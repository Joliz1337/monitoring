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
from app.services.xray_test.runner import CoreRunner
from app.services.xray_test.sanitize import sanitize_output

logger = logging.getLogger(__name__)

FINISHED_TTL_SECONDS = 900
LOG_BUFFER_LIMIT = 2000
MAX_ACTIVE_JOBS = 5
DEFAULT_CONCURRENCY = 4
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
        runner: CoreRunner,
        *,
        location: str,
        concurrency: int = DEFAULT_CONCURRENCY,
        on_finish: Optional[Callable[[XrayTestJob], Awaitable[None]]] = None,
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
            job, cells, options, runner,
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
        runner: CoreRunner,
        concurrency: int,
        on_finish: Optional[Callable[[XrayTestJob], Awaitable[None]]],
    ) -> None:
        semaphore = asyncio.Semaphore(concurrency)
        self._emit(job, {"type": "start", "total": job.total, "location": job.location})
        self._log(job, f"Проверок: {job.total}, параллельно: {concurrency}")

        async def run_cell(cell: TestCell) -> None:
            async with semaphore:
                try:
                    result = await runner.probe(cell, options)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — одна ячейка не валит задачу
                    logger.warning("xray-test cell %s failed: %s", cell.index, exc)
                    result = _internal_error(cell, exc)
                self._emit_result(job, result)

        try:
            await asyncio.gather(*(run_cell(cell) for cell in cells))
            self._finish(job, "success")
        except asyncio.CancelledError:
            # История при отмене не пишется: await в отменённой задаче тут же
            # получит отмену снова, а частичный прогон сравнивать не с чем
            self._finish(job, "cancelled", "Проверка отменена")
            raise
        except Exception as exc:  # noqa: BLE001 — верхняя граница фоновой задачи
            logger.error("xray-test job %s failed: %s", job.id, exc)
            self._finish(job, "error", str(exc))

        if on_finish is not None and job.results:
            try:
                await on_finish(job)
            except Exception as exc:  # noqa: BLE001 — история не должна валить прогон
                logger.warning("xray-test history not saved for %s: %s", job.id, exc)

    def _emit_result(self, job: XrayTestJob, result: CellResult) -> None:
        payload = result.as_event()
        job.results.append(payload)
        self._emit(job, {"type": "cell", **payload, "done": len(job.results)})

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
                event = await queue.get()
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


def _internal_error(cell: TestCell, exc: Exception) -> CellResult:
    endpoint = cell.endpoint
    return CellResult(
        index=cell.index,
        remark=endpoint.remark,
        protocol=endpoint.protocol.value,
        address=endpoint.address,
        port=endpoint.port,
        sni=cell.sni_label or endpoint.tls.sni,
        transport=endpoint.transport.kind.value,
        security=endpoint.tls.security.value,
        verdict=Verdict.FAIL,
        reason=FailReason.INTERNAL,
        detail=sanitize_output(str(exc))[:400],
        link=cell.link,
    )


_manager = XrayTestJobManager()


def get_xray_test_manager() -> XrayTestJobManager:
    return _manager
