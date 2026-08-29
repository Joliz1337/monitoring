"""Установка ноды Remnawave на уже добавленный сервер через агента ноды.

Панель запускает install.sh (MON_INSTALL_REMNAWAVE=1) на хосте через SSE-канал
агента POST /api/system/execute-stream (nsenter) — SSH-креды не нужны. Установка
идёт фоновой задачей, не привязанной к HTTP: обрыв вкладки её не прерывает; лог
стримится подписчикам и переигрывается при переподключении (как у деплоя).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import httpx
from sqlalchemy import update

from app.database import async_session_maker
from app.models import Server
from app.services.http_client import get_node_client, node_auth_headers
from app.services.node_capabilities import learn_from_denial

logger = logging.getLogger(__name__)

# Потолок исполнителя ноды (host_executor.MAX_TIMEOUT) — дольше команда не живёт
EXEC_TIMEOUT = 600
HTTP_READ_TIMEOUT = 620
FINISHED_TTL_SECONDS = 600
LOG_BUFFER_LIMIT = 5000


def parse_sse_event(event_name: str, data: str) -> Optional[dict]:
    """SSE-событие исполнителя ноды → внутреннее событие job'а.

    stdout/stderr → log, error → error, done → служебный _exit с кодом.
    """
    try:
        payload = json.loads(data) if data else {}
    except json.JSONDecodeError:
        payload = {}

    if event_name in ("stdout", "stderr"):
        return {"type": "log", "line": payload.get("line", "")}
    if event_name == "error":
        return {"type": "error", "message": payload.get("message", "Unknown node error")}
    if event_name == "done":
        exit_code = payload.get("exit_code", -1)
        return {"type": "_exit", "code": exit_code, "success": bool(payload.get("success", exit_code == 0))}
    return None


async def run_install_on_node(server: Server, command: str) -> AsyncIterator[dict]:
    """Выполнить команду установки на хосте ноды, стримя события {type: log|error|done}."""
    url = f"{server.url}/api/system/execute-stream"
    body = {"command": command, "timeout": EXEC_TIMEOUT, "shell": "bash"}
    # per-request таймаут: у клиента ноды дефолтный read слишком короткий для
    # долгой установки, а SSE-исполнитель шлёт done не позже своего потолка
    timeout = httpx.Timeout(connect=10.0, read=HTTP_READ_TIMEOUT, write=10.0, pool=10.0)

    try:
        client = get_node_client(server)
        async with client.stream(
            "POST", url, headers=node_auth_headers(server), json=body, timeout=timeout
        ) as response:
            if response.status_code != 200:
                raw = (await response.aread()).decode("utf-8", errors="replace")
                try:
                    detail = json.loads(raw)
                except json.JSONDecodeError:
                    detail = raw
                await learn_from_denial(server.id, response.status_code, detail)
                yield {"type": "error", "message": f"Нода ответила HTTP {response.status_code}: {raw[:300]}"}
                return

            event_name = ""
            exit_code: Optional[int] = None
            async for line in response.aiter_lines():
                line = line.rstrip("\r")
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                    continue
                if line.startswith("data:"):
                    event = parse_sse_event(event_name, line[len("data:"):].strip())
                    if event is None:
                        continue
                    if event["type"] == "_exit":
                        exit_code = event["code"]
                        continue
                    yield event
                    if event["type"] == "error":
                        return

            if exit_code is None:
                yield {"type": "error", "message": "Стрим ноды оборвался без результата"}
                return
            yield {"type": "done", "exit_code": exit_code}
    except httpx.TimeoutException:
        yield {"type": "error", "message": "Таймаут соединения с нодой"}
    except httpx.RequestError as exc:
        yield {"type": "error", "message": f"Ошибка соединения с нодой: {exc}"}


@dataclass
class RemnawaveInstallJob:
    id: str
    server_id: int
    server_name: str
    status: str = "running"  # running | success | error
    log: list[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    subscribers: set = field(default_factory=set)
    task: Optional[asyncio.Task] = None


class RemnawaveInstallJobManager:
    """In-memory реестр фоновых установок Remnawave с pub/sub лога."""

    def __init__(self) -> None:
        self._jobs: dict[str, RemnawaveInstallJob] = {}

    def _cleanup_finished(self) -> None:
        now = time.time()
        for jid in [
            j for j, job in self._jobs.items()
            if job.finished_at is not None and now - job.finished_at > FINISHED_TTL_SECONDS
        ]:
            self._jobs.pop(jid, None)

    def get(self, job_id: str) -> Optional[RemnawaveInstallJob]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        self._cleanup_finished()
        return [
            {
                "job_id": j.id,
                "server_id": j.server_id,
                "name": j.server_name,
                "status": j.status,
                "exit_code": j.exit_code,
                "error": j.error,
            }
            for j in sorted(self._jobs.values(), key=lambda x: x.started_at)
        ]

    def start(self, server: Server, command: str) -> str:
        self._cleanup_finished()
        job_id = uuid.uuid4().hex
        job = RemnawaveInstallJob(id=job_id, server_id=server.id, server_name=server.name)
        self._jobs[job_id] = job
        job.task = asyncio.create_task(self._run(job, server, command))
        return job_id

    def _emit(self, job: RemnawaveInstallJob, event: dict) -> None:
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

    def _finish(self, job: RemnawaveInstallJob, status: str, error: Optional[str] = None) -> None:
        job.status = status
        job.error = error
        job.finished_at = time.time()
        self._emit(job, {"type": "done", "status": status, "exit_code": job.exit_code})

    async def _run(self, job: RemnawaveInstallJob, server: Server, command: str) -> None:
        try:
            self._emit(job, {"type": "start", "name": job.server_name})
            self._emit(job, {"type": "log", "line": f"[panel] Устанавливаю ноду Remnawave на «{job.server_name}» через агента…"})
            async for event in run_install_on_node(server, command):
                etype = event.get("type")
                if etype == "error":
                    self._emit(job, event)
                    self._finish(job, "error", event.get("message"))
                    return
                if etype == "done":
                    job.exit_code = event.get("exit_code")
                    if job.exit_code == 0:
                        await self._mark_xray_installed(job.server_id)
                        self._emit(job, {"type": "log", "line": "[panel] Установка завершена"})
                        self._finish(job, "success")
                    else:
                        self._finish(job, "error", f"install.sh завершился с кодом {job.exit_code}")
                    return
                self._emit(job, event)
            self._finish(job, "error", "Установка прервалась без результата")
        except asyncio.CancelledError:
            self._finish(job, "error", "Установка отменена")
            raise
        except Exception as exc:  # noqa: BLE001 — верхняя граница фоновой задачи
            logger.error("Remnawave install job %s failed: %s", job.id, exc)
            self._emit(job, {"type": "error", "message": str(exc)})
            self._finish(job, "error", str(exc))

    async def _mark_xray_installed(self, server_id: int) -> None:
        """Бейдж «xray» сразу, не дожидаясь 2-минутного цикла коллектора."""
        try:
            async with async_session_maker() as db:
                await db.execute(
                    update(Server).where(Server.id == server_id).values(has_xray_node=True)
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — best-effort, коллектор поправит сам
            logger.warning("Failed to mark has_xray_node for server %s: %s", server_id, exc)

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
            yield {"type": "start", "name": job.server_name}
            for line in backlog:
                yield {"type": "log", "line": line}
            if not live:
                if job.error:
                    yield {"type": "error", "message": job.error}
                yield {"type": "done", "status": job.status, "exit_code": job.exit_code}
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


_manager = RemnawaveInstallJobManager()


def get_remnawave_install_manager() -> RemnawaveInstallJobManager:
    return _manager
