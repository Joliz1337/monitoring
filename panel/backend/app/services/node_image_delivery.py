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

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 30
LOAD_TIMEOUT = 3600
UPDATE_TIMEOUT = 7200
REMOTE_TAR = "/tmp/mon-node-img.tar.gz"
FINISHED_TTL_SECONDS = 600
LOG_BUFFER_LIMIT = 5000


@dataclass
class SSHTarget:
    host: str
    port: int = 22
    user: str = "root"
    password: Optional[str] = None
    private_key: Optional[str] = None
    passphrase: Optional[str] = None


def _connect_kwargs(t: SSHTarget) -> dict:
    kwargs: dict = {
        "host": t.host,
        "port": t.port,
        "username": t.user,
        "known_hosts": None,
        "connect_timeout": CONNECT_TIMEOUT,
        # Долгая заливка образа по медленному/throttled-каналу: keepalive держит
        # SSH-сессию живой в паузах между шагами.
        "keepalive_interval": 15,
        "keepalive_count_max": 8,
    }
    if t.private_key:
        kwargs["client_keys"] = [asyncssh.import_private_key(t.private_key, t.passphrase or None)]
    elif t.password:
        kwargs["password"] = t.password
    return kwargs


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


async def deliver_image(target: SSHTarget, tag: str, ref: str) -> AsyncIterator[dict]:
    """Доставить образ ноды и обновить её. Стримит события {type: log|error|done}."""
    yield {"type": "log", "line": f"[panel] Готовлю образ ноды ({tag})…"}
    try:
        tar = await ensure_image(tag)
    except Exception as exc:  # noqa: BLE001 — граница: реестр/докер недоступны панели
        yield {"type": "error", "message": f"Панель не смогла получить образ с реестра: {exc}"}
        return

    size_mb = tar.stat().st_size // (1024 * 1024)
    try:
        async with await asyncssh.connect(**_connect_kwargs(target)) as conn:
            yield {"type": "log", "line": f"[panel] SSH к {target.host}:{target.port} установлен"}
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
                    yield {"type": "log", "line": f"[panel] Заливка: {pct}% ({done_mb}/{size_mb} МБ)"}
                await put_task  # пробросить исключение, если заливка упала

            yield {"type": "log", "line": "[panel] Загружаю образ в docker…"}
            load_cmd = f"gunzip -c {shlex.quote(REMOTE_TAR)} | docker load && rm -f {shlex.quote(REMOTE_TAR)}"
            async for ev in _run_streamed(conn, load_cmd, LOAD_TIMEOUT):
                if ev["type"] == "exit":
                    if ev["code"] != 0:
                        yield {"type": "error", "message": f"docker load не удался (код {ev['code']})"}
                        return
                else:
                    yield ev

            yield {"type": "log", "line": "[panel] Обновляю ноду на локальном образе…"}
            update_cmd = f"bash /opt/monitoring-node/update.sh {shlex.quote(ref)}"
            async for ev in _run_streamed(conn, update_cmd, UPDATE_TIMEOUT):
                if ev["type"] == "exit":
                    if ev["code"] != 0:
                        yield {"type": "error", "message": f"Обновление ноды завершилось с кодом {ev['code']}"}
                        return
                else:
                    yield ev

            yield {"type": "done", "message": "Образ доставлен, нода обновлена"}
    except asyncssh.PermissionDenied:
        yield {"type": "error", "message": "SSH: неверный логин, пароль или ключ"}
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
        yield {"type": "error", "message": f"Ошибка SSH: {exc}"}


@dataclass
class DeliveryJob:
    id: str
    name: str
    host: str
    status: str = "running"  # running | success | error
    log: list[str] = field(default_factory=list)
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    subscribers: set = field(default_factory=set)
    task: Optional[asyncio.Task] = None


class ImageDeliveryJobManager:
    """In-memory реестр фоновых задач доставки образа с pub/sub лога."""

    def __init__(self) -> None:
        self._jobs: dict[str, DeliveryJob] = {}

    def _cleanup_finished(self) -> None:
        now = time.time()
        for jid in [
            j for j, job in self._jobs.items()
            if job.finished_at is not None and now - job.finished_at > FINISHED_TTL_SECONDS
        ]:
            self._jobs.pop(jid, None)

    def get(self, job_id: str) -> Optional[DeliveryJob]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        self._cleanup_finished()
        return [
            {"job_id": j.id, "name": j.name, "host": j.host, "status": j.status, "error": j.error}
            for j in sorted(self._jobs.values(), key=lambda x: x.started_at)
        ]

    def start(self, name: str, target: SSHTarget, tag: str, ref: str) -> str:
        self._cleanup_finished()
        job_id = uuid.uuid4().hex
        job = DeliveryJob(id=job_id, name=name, host=target.host)
        self._jobs[job_id] = job
        job.task = asyncio.create_task(self._run(job, target, tag, ref))
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

    async def _run(self, job: DeliveryJob, target: SSHTarget, tag: str, ref: str) -> None:
        try:
            self._emit(job, {"type": "start", "host": job.host})
            async for event in deliver_image(target, tag, ref):
                etype = event.get("type")
                if etype == "error":
                    job.error = event.get("message")
                    self._emit(job, event)
                    self._emit(job, {"type": "done", "status": "error"})
                    job.status = "error"
                    job.finished_at = time.time()
                    return
                if etype == "done":
                    self._emit(job, event)
                    self._emit(job, {"type": "done", "status": "success"})
                    job.status = "success"
                    job.finished_at = time.time()
                    return
                self._emit(job, event)
            self._emit(job, {"type": "error", "message": "Доставка прервалась без результата"})
            self._emit(job, {"type": "done", "status": "error"})
            job.status = "error"
            job.error = "Доставка прервалась без результата"
            job.finished_at = time.time()
        except asyncio.CancelledError:
            job.status = "error"
            job.error = "Доставка отменена"
            job.finished_at = time.time()
            raise
        except Exception as exc:  # noqa: BLE001 — верхняя граница фоновой задачи
            logger.error("Delivery job %s failed: %s", job.id, exc)
            self._emit(job, {"type": "error", "message": str(exc)})
            self._emit(job, {"type": "done", "status": "error"})
            job.status = "error"
            job.error = str(exc)
            job.finished_at = time.time()

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
