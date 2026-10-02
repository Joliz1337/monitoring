"""Доставка образа ноды с панели на заблокированную ноду по SSH.

Один SSH-сеанс: SFTP-заливка gzip-tar образа → docker load → запуск update.sh
(код едет git-clone'ом с зеркалом, pull промахивается мимо GHCR, а
compose_images_present поднимает уже загруженный локальный образ). Не зависит от
mTLS — работает даже когда агент ноды лежит.

Доставка идёт фоновой задачей, не привязанной к HTTP: заливка ~200 МБ по 6 Мбит
длится минуты, обрыв вкладки её не прерывает; лог стримится подписчикам и
переигрывается при переподключении (как у авторазвёртывания).
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import asyncssh

from app.services.node_image_cache import ensure_image
from app.services.ssh_target import SSHTarget, ssh_connect_kwargs

logger = logging.getLogger(__name__)

LOAD_TIMEOUT = 3600
UPDATE_TIMEOUT = 7200
REMOTE_TAR = "/tmp/mon-node-img.tar.gz"
FINISHED_TTL_SECONDS = 600
DELIVERY_CONCURRENCY = 5
LOG_BUFFER_LIMIT = 5000


async def _run_streamed(conn, command: str, timeout: int) -> AsyncIterator[dict]:
    """Выполнить команду по SSH, стримя вывод построчно. Последнее событие —
    {"type": "exit", "code": N}."""
    process = await conn.create_process(command, stderr=asyncssh.STDOUT)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            process.terminate()
            yield {"type": "exit", "code": 124}
            return
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            process.terminate()
            yield {"type": "exit", "code": 124}
            return
        if not line:
            break
        yield {"type": "log", "line": line.rstrip("\n")}
    await process.wait()
    yield {"type": "exit", "code": process.returncode if process.returncode is not None else 1}


async def deliver_image(target: SSHTarget, tag: str) -> AsyncIterator[dict]:
    """Доставить образ ноды и поднять её на нём. Стримит события {type: log|step|error|done}."""
    yield {"type": "step", "step": "prepare"}
    yield {"type": "log", "line": f"[panel] Готовлю образ ноды ({tag})…"}
    try:
        tar = await ensure_image(tag)
    except Exception as exc:  # noqa: BLE001 — граница: реестр/докер недоступны панели
        yield {"type": "error", "message": f"Панель не смогла получить образ с реестра: {exc}"}
        return

    size_mb = tar.stat().st_size // (1024 * 1024)
    try:
        async with await asyncssh.connect(**ssh_connect_kwargs(target)) as conn:
            yield {"type": "log", "line": f"[panel] SSH к {target.host}:{target.port} установлен"}
            yield {"type": "step", "step": "upload", "percent": 0}
            yield {"type": "log", "line": f"[panel] Заливаю образ ({size_mb} МБ) — на медленном канале это несколько минут…"}
            async with conn.start_sftp_client() as sftp:
                total = tar.stat().st_size
                progress = {"copied": 0}

                def _on_progress(_src, _dst, copied, _total):
                    progress["copied"] = copied

                put_task = asyncio.create_task(
                    sftp.put(str(tar), REMOTE_TAR, progress_handler=_on_progress)
                )
                # Пока идёт заливка — раз в 8с шлём прогресс: стрим лога не молчит
                # (иначе прокси/браузер рвут соединение по idle-таймауту) и виден процент.
                while not put_task.done():
                    await asyncio.sleep(8)
                    done_mb = progress["copied"] // (1024 * 1024)
                    pct = int(progress["copied"] * 100 / total) if total else 0
                    yield {"type": "step", "step": "upload", "percent": pct}
                    yield {"type": "log", "line": f"[panel] Заливка: {pct}% ({done_mb}/{size_mb} МБ)"}
                await put_task  # пробросить исключение, если заливка упала

            yield {"type": "step", "step": "load"}
            yield {"type": "log", "line": "[panel] Загружаю образ в docker…"}
            load_cmd = f"gunzip -c {shlex.quote(REMOTE_TAR)} | docker load && rm -f {shlex.quote(REMOTE_TAR)}"
            async for ev in _run_streamed(conn, load_cmd, LOAD_TIMEOUT):
                if ev["type"] == "exit":
                    if ev["code"] != 0:
                        yield {"type": "error", "message": f"docker load не удался (код {ev['code']})"}
                        return
                else:
                    yield ev

            # Никаких скачиваний с ноды: образ уже загружен, просто поднимаем её на
            # нём. Тег в .env приводим к доставленному, иначе compose полез бы в реестр.
            yield {"type": "step", "step": "start"}
            yield {"type": "log", "line": "[panel] Поднимаю ноду на доставленном образе (без скачивания)…"}
            apply_cmd = (
                "cd /opt/monitoring-node && "
                f"{{ grep -q '^MON_IMAGE_TAG=' .env && sed -i 's|^MON_IMAGE_TAG=.*|MON_IMAGE_TAG={tag}|' .env "
                f"|| echo 'MON_IMAGE_TAG={tag}' >> .env; }} && "
                "docker compose up -d && "
                "for i in $(seq 1 30); do curl -sf http://localhost:7500/health >/dev/null 2>&1 && break; sleep 2; done; "
                "echo '[node] нода поднята на доставленном образе'"
            )
            async for ev in _run_streamed(conn, apply_cmd, UPDATE_TIMEOUT):
                if ev["type"] == "exit":
                    if ev["code"] != 0:
                        yield {"type": "error", "message": f"Не удалось поднять ноду на образе (код {ev['code']})"}
                        return
                else:
                    yield ev

            yield {"type": "done", "message": "Образ доставлен, нода поднята"}
    except asyncssh.PermissionDenied:
        yield {"type": "error", "message": "SSH: неверный логин, пароль или ключ"}
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
        yield {"type": "error", "message": f"Ошибка SSH: {exc}"}


@dataclass
class DeliveryJob:
    id: str
    server_id: int
    name: str
    host: str
    status: str = "queued"  # queued | running | success | error
    # Этап по порядку: prepare → upload → load → start — статус на карточке ноды
    # «заливка образа 45% (2/4)»; пока задача идёт
    step: Optional[str] = None
    percent: Optional[int] = None  # прогресс заливки образа
    log: list[str] = field(default_factory=list)
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    subscribers: set = field(default_factory=set)
    task: Optional[asyncio.Task] = None


class ImageDeliveryJobManager:
    """In-memory реестр фоновых задач доставки образа с pub/sub лога."""

    def __init__(self, concurrency: int = DELIVERY_CONCURRENCY) -> None:
        self._jobs: dict[str, DeliveryJob] = {}
        self._concurrency = concurrency
        # Все заливки идут с одного аплинка панели: без потолка массовый запуск
        # поделил бы канал на всех и не довёз бы образ ни до одной ноды за разумное время
        self._slots = asyncio.Semaphore(concurrency)

    def _cleanup_finished(self) -> None:
        now = time.time()
        for jid in [
            j for j, job in self._jobs.items()
            if job.finished_at is not None and now - job.finished_at > FINISHED_TTL_SECONDS
        ]:
            self._jobs.pop(jid, None)

    def get(self, job_id: str) -> Optional[DeliveryJob]:
        return self._jobs.get(job_id)

    def _active_job_for(self, server_id: int) -> Optional[DeliveryJob]:
        return next(
            (j for j in self._jobs.values() if j.server_id == server_id and j.finished_at is None),
            None,
        )

    def list_jobs(self) -> list[dict]:
        self._cleanup_finished()
        return [
            {
                "job_id": j.id,
                "server_id": j.server_id,
                "name": j.name,
                "host": j.host,
                "status": j.status,
                "error": j.error,
                "started_at": j.started_at,
                "finished_at": j.finished_at,
                "step": j.step,
                "percent": j.percent,
            }
            for j in sorted(self._jobs.values(), key=lambda x: x.started_at)
        ]

    def start(self, server_id: int, name: str, target: SSHTarget, tag: str) -> str:
        """Запустить доставку. Если по серверу уже идёт задача — вернуть её, вторую не плодить."""
        self._cleanup_finished()
        active = self._active_job_for(server_id)
        if active is not None:
            return active.id
        job_id = uuid.uuid4().hex
        job = DeliveryJob(id=job_id, server_id=server_id, name=name, host=target.host)
        self._jobs[job_id] = job
        job.task = asyncio.create_task(self._run(job, target, tag))
        return job_id

    def _emit(self, job: DeliveryJob, event: dict) -> None:
        if event.get("type") == "log":
            job.log.append(event.get("line", ""))
            if len(job.log) > LOG_BUFFER_LIMIT:
                del job.log[: len(job.log) - LOG_BUFFER_LIMIT]
            event = {**event, "_idx": len(job.log) - 1}
        for queue in list(job.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _finish(self, job: DeliveryJob, status: str, error: Optional[str] = None) -> None:
        if error:
            self._emit(job, {"type": "error", "message": error})
        job.status = status
        job.error = error
        job.step = None
        job.percent = None
        job.finished_at = time.time()
        self._emit(job, {"type": "done", "status": status})

    async def _run(self, job: DeliveryJob, target: SSHTarget, tag: str) -> None:
        try:
            self._emit(job, {"type": "start", "host": job.host})
            if self._slots.locked():
                self._emit(job, {
                    "type": "log",
                    "line": f"[panel] В очереди: одновременно обновляется не больше {self._concurrency} нод",
                })
            async with self._slots:
                job.status = "running"
                async for event in deliver_image(target, tag):
                    etype = event.get("type")
                    if etype == "error":
                        self._finish(job, "error", event.get("message"))
                        return
                    if etype == "done":
                        self._emit(job, {"type": "log", "line": f"[panel] {event.get('message')}"})
                        self._finish(job, "success")
                        return
                    if etype == "step":
                        # Этап — для статуса на карточке, в лог окна он не идёт
                        job.step = event.get("step")
                        job.percent = event.get("percent")
                        continue
                    self._emit(job, event)
            self._finish(job, "error", "Доставка прервалась без результата")
        except asyncio.CancelledError:
            self._finish(job, "error", "Доставка отменена")
            raise
        except Exception as exc:  # noqa: BLE001 — верхняя граница фоновой задачи
            logger.error("Delivery job %s failed: %s", job.id, exc)
            self._finish(job, "error", str(exc))

    async def subscribe(self, job_id: str) -> AsyncIterator[dict]:
        job = self._jobs.get(job_id)
        if job is None:
            return
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        live = job.finished_at is None
        if live:
            job.subscribers.add(queue)
        backlog = list(job.log)
        last_idx = len(backlog) - 1
        try:
            yield {"type": "start", "host": job.host}
            for line in backlog:
                yield {"type": "log", "line": line}
            if not live:
                if job.error:
                    yield {"type": "error", "message": job.error}
                yield {"type": "done", "status": job.status}
                return
            while True:
                event = await queue.get()
                etype = event.get("type")
                if etype == "start":
                    continue
                if etype == "log":
                    if event.get("_idx", -1) <= last_idx:
                        continue
                    yield {"type": "log", "line": event.get("line", "")}
                    continue
                yield event
                if etype == "done":
                    return
        finally:
            job.subscribers.discard(queue)


_manager = ImageDeliveryJobManager()


def get_image_delivery_manager() -> ImageDeliveryJobManager:
    return _manager
