"""Фоновые задачи проверки с переподключаемым потоком результатов.

Прогон подписки на две сотни ключей идёт минутами: держать его на времени
жизни HTTP-запроса нельзя — закрытая вкладка обрывала бы работу. Задача живёт в
памяти процесса, подписчик получает накопленные результаты и продолжение
потока, поэтому перезагрузка страницы ничего не теряет.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, Optional

from app.services.xray_test.errors import LimitExceededError
from app.services.xray_test.models import CellResult, FailReason, TestCell, Verdict
from app.services.xray_test.probes import ProbeOptions
from app.services.xray_test.runner import BATCH_SIZE, CoreRunner
from app.services.xray_test.sanitize import sanitize_output

logger = logging.getLogger(__name__)

FINISHED_TTL_SECONDS = 900
LOG_BUFFER_LIMIT = 2000
MAX_ACTIVE_JOBS = 5
# Через сколько результатов отмечать прогресс в журнале задачи
FinishHook = Callable[["XrayTestJob"], Awaitable[None]]

PROGRESS_STEP = 25
# Тик в поток, когда нечего сказать: прокси рвут молчащие соединения
HEARTBEAT_INTERVAL = 15.0
# Потолок он же значение по умолчанию: держать проверки медленнее, чем позволяют
# зарезервированные порты, смысла нет — это просто дольше при том же результате
# Рабочих на точку запуска. Каждый берёт из очереди пачку и прогоняет её одним
# процессом ядра, поэтому число процессов теперь равно числу рабочих, а не числу
# проверок: восьми хватает, чтобы не простаивать, и нода не задыхается.
MAX_CONCURRENCY = 8


@dataclass
class XrayTestJob:
    id: str
    total: int
    location: str
    status: str = "running"  # running | success | error | cancelled
    results: list[dict] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    subscribers: set = field(default_factory=set)
    task: Optional[asyncio.Task] = None

    @property
    def summary(self) -> dict:
        counts = {verdict.value: 0 for verdict in Verdict}
        for item in self.results:
            counts[item.get("verdict", Verdict.FAIL.value)] += 1
        return {"total": self.total, "done": len(self.results), **counts}


class XrayTestJobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, XrayTestJob] = {}
        # Сохранение истории отменённого прогона живёт дольше самой задачи
        self._detached: set[asyncio.Task] = set()

    def get(self, job_id: str) -> Optional[XrayTestJob]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        self._cleanup_finished()
        return [
            {
                "job_id": job.id,
                "status": job.status,
                "location": job.location,
                "error": job.error,
                "started_at": job.started_at,
                **job.summary,
            }
            for job in sorted(self._jobs.values(), key=lambda item: item.started_at)
        ]

    def start(
        self,
        cells: list[TestCell],
        options: ProbeOptions,
        runners: dict[str, CoreRunner],
        *,
        location: str,
        concurrency: int = MAX_CONCURRENCY,
        on_finish: Optional[FinishHook] = None,
    ) -> str:
        self._cleanup_finished()
        active = sum(1 for job in self._jobs.values() if job.finished_at is None)
        if active >= MAX_ACTIVE_JOBS:
            raise LimitExceededError(
                f"Уже выполняется {active} проверок — дождитесь завершения или отмените лишние"
            )

        job = XrayTestJob(id=uuid.uuid4().hex, total=len(cells), location=location)
        self._jobs[job.id] = job
        job.task = asyncio.create_task(self._run(
            job, cells, options, runners,
            max(1, min(concurrency, MAX_CONCURRENCY)), on_finish,
        ))
        return job.id

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.finished_at is not None or job.task is None:
            return False
        job.task.cancel()
        return True

    async def _run(
        self,
        job: XrayTestJob,
        cells: list[TestCell],
        options: ProbeOptions,
        runners: dict[str, CoreRunner],
        concurrency: int,
        on_finish: Optional[FinishHook],
    ) -> None:
        self._emit(job, {"type": "start", "total": job.total, "location": job.location})
        self._log(job, f"Проверок: {job.total}, параллельно на точку: {concurrency}")
        # Нода тянет меньше панели и делит процессор с боевым трафиком, поэтому
        # её потолок считается по числу ядер — и его видно, а не приходится гадать
        for code, runner in runners.items():
            capacity = getattr(runner, "capacity", None)
            if capacity is not None:
                self._log(job, f"Точка {code}: одновременных проверок не больше {capacity}")

        # Очередь на каждое место запуска и постоянное число рабочих над ней.
        # Размер прогона ничем не ограничен, поэтому создавать задачу на каждую
        # ячейку нельзя — рабочие разбирают очередь порциями по мере готовности.
        # Отдельные очереди нужны потому, что нода тянет проверки медленнее
        # панели: с общей очередью быстрая точка простаивала бы за медленной.
        queues: dict[str, asyncio.Queue[TestCell]] = {}
        for cell in cells:
            queues.setdefault(cell.location, asyncio.Queue()).put_nowait(cell)

        try:
            workers = [
                asyncio.create_task(self._worker(job, queue, runners.get(code), options))
                for code, queue in queues.items()
                for _ in range(concurrency)
            ]
            await asyncio.gather(*workers)
            self._finish(job, "success")
        except asyncio.CancelledError:
            self._finish(job, "cancelled", "Проверка отменена")
            # Внутри отменённой задачи любой await тут же получит отмену снова,
            # поэтому сохранение уходит отдельной задачей: остановленный вручную
            # прогон — обычно самый интересный, терять его результаты нельзя
            self._persist_detached(job, on_finish)
            raise
        except Exception as exc:  # noqa: BLE001 — верхняя граница фоновой задачи
            logger.error("xray-test job %s failed: %s", job.id, exc)
            self._finish(job, "error", str(exc))

        await self._persist(job, on_finish)

    async def _persist(self, job: XrayTestJob, on_finish: Optional[FinishHook]) -> None:
        if on_finish is None or not job.results:
            return
        try:
            await on_finish(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — история не должна валить прогон
            # Молча потерянная история выглядит как «истории вообще нет»:
            # причина должна дойти и до журнала прогона, а не только в лог
            logger.error("xray-test history not saved for %s: %s", job.id, exc, exc_info=True)
            self._log(job, f"История прогона не сохранена: {exc}")

    def _persist_detached(self, job: XrayTestJob, on_finish: Optional[FinishHook]) -> None:
        if on_finish is None or not job.results:
            return
        task = asyncio.create_task(self._persist(job, on_finish))
        # Держим ссылку: задача без неё может быть собрана сборщиком мусора
        self._detached.add(task)
        task.add_done_callback(self._detached.discard)

    async def _worker(
        self,
        job: XrayTestJob,
        queue: "asyncio.Queue[TestCell]",
        runner: Optional[CoreRunner],
        options: ProbeOptions,
    ) -> None:
        while True:
            batch = _take(queue, BATCH_SIZE)
            if not batch:
                return

            if runner is None:
                for cell in batch:
                    self._emit_result(job, _internal_error(
                        cell, RuntimeError(f"нет исполнителя для {cell.location}")
                    ))
                continue

            # Результат уходит в поток сразу, как только готов: пачка на ноде
            # идёт минуты, и ждать её целиком значит держать таблицу пустой
            reported: set[int] = set()

            def report(result: CellResult) -> None:
                if result.index in reported:
                    return
                reported.add(result.index)
                self._emit_result(job, result)

            try:
                results = await runner.probe_batch(batch, options, report)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — одна пачка не валит задачу
                logger.warning("xray-test batch of %d failed: %s", len(batch), exc)
                results = [_internal_error(cell, exc) for cell in batch]

            # Добираем то, что раннер не успел отдать сам
            for result in results:
                report(result)

    def _emit_result(self, job: XrayTestJob, result: CellResult) -> None:
        payload = result.as_event()
        job.results.append(payload)
        done = len(job.results)
        self._emit(job, {"type": "cell", **payload, "done": done})

        # На длинном прогоне отметки в журнале показывают, что работа идёт
        if job.total > PROGRESS_STEP and done % PROGRESS_STEP == 0:
            counts = job.summary
            self._log(job, (
                f"Проверено {done} из {job.total} за {_elapsed(job)} "
                f"({_rate(job, done)}) — "
                f"работают {counts['ok']}, с оговорками {counts['degraded']}, "
                f"не работают {counts['fail']}"
            ))

    def _log(self, job: XrayTestJob, line: str) -> None:
        self._emit(job, {"type": "log", "line": sanitize_output(line)})

    def _emit(self, job: XrayTestJob, event: dict) -> None:
        if event.get("type") == "log":
            job.log.append(event.get("line", ""))
            if len(job.log) > LOG_BUFFER_LIMIT:
                del job.log[: len(job.log) - LOG_BUFFER_LIMIT]
        for queue in list(job.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _finish(self, job: XrayTestJob, status: str, error: Optional[str] = None) -> None:
        job.status = status
        job.error = error
        job.finished_at = time.time()
        self._emit(job, {"type": "done", "status": status, "error": error, **job.summary})

    def _cleanup_finished(self) -> None:
        now = time.time()
        for job_id in [
            key for key, job in self._jobs.items()
            if job.finished_at is not None and now - job.finished_at > FINISHED_TTL_SECONDS
        ]:
            self._jobs.pop(job_id, None)

    async def subscribe(self, job_id: str) -> AsyncIterator[dict]:
        job = self._jobs.get(job_id)
        if job is None:
            return

        queue: asyncio.Queue = asyncio.Queue(maxsize=4000)
        live = job.finished_at is None
        if live:
            job.subscribers.add(queue)

        backlog = list(job.results)
        seen = len(backlog)
        try:
            yield {"type": "start", "total": job.total, "location": job.location}
            for item in backlog:
                yield {"type": "cell", **item, "done": item.get("index", 0) + 1}

            if not live:
                yield {
                    "type": "done", "status": job.status, "error": job.error, **job.summary,
                }
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    # Пока идут долгие проверки, событий нет минутами, и молчащий
                    # поток закрывает прокси между панелью и браузером. Пустой
                    # тик держит соединение и заодно показывает, что мы живы.
                    yield {"type": "ping", "done": len(job.results), "total": job.total}
                    continue
                kind = event.get("type")
                if kind == "start":
                    continue
                # Реплей уже отданных ячеек: подписка могла успеть на середину
                if kind == "cell" and event.get("done", 0) <= seen:
                    continue
                yield event
                if kind == "done":
                    return
        finally:
            job.subscribers.discard(queue)


def _elapsed(job: XrayTestJob) -> str:
    seconds = max(1, int(time.time() - job.started_at))
    return f"{seconds // 60} мин {seconds % 60} с" if seconds >= 60 else f"{seconds} с"


def _rate(job: XrayTestJob, done: int) -> str:
    """Скорость и остаток: без них «долго» остаётся ощущением, а не числом."""
    seconds = max(1.0, time.time() - job.started_at)
    per_minute = done / seconds * 60
    if per_minute < 1:
        return "меньше 1/мин"
    left = job.total - done
    eta = int(left / per_minute) if per_minute else 0
    return f"{per_minute:.0f}/мин, осталось ~{eta} мин" if left else f"{per_minute:.0f}/мин"


def _take(queue: "asyncio.Queue[TestCell]", limit: int) -> list[TestCell]:
    """Снять из очереди до `limit` ячеек, не дожидаясь остальных."""
    batch: list[TestCell] = []
    while len(batch) < limit:
        try:
            batch.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return batch


def _internal_error(cell: TestCell, exc: Exception) -> CellResult:
    endpoint = cell.endpoint
    return CellResult(
        index=cell.index,
        remark=endpoint.remark,
        protocol=endpoint.protocol.value,
        address=endpoint.address,
        port=endpoint.port,
        sni=cell.sni_label or endpoint.tls.sni,
        sni_from_config=cell.sni_label is None,
        transport=endpoint.transport.kind.value,
        security=endpoint.tls.security.value,
        verdict=Verdict.FAIL,
        reason=FailReason.INTERNAL,
        detail=sanitize_output(str(exc))[:400],
        link=cell.link,
        location=cell.location,
        location_name=cell.location_name,
    )


_manager = XrayTestJobManager()


def get_xray_test_manager() -> XrayTestJobManager:
    return _manager
