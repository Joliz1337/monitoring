"""Фоновые задачи авторазвёртывания нод.

Установка ноды по SSH выполняется в фоновой asyncio-задаче, не привязанной к
HTTP-соединению клиента: закрытие вкладки браузера установку не прерывает.
Лог буферизуется в памяти и стримится подписчикам построчно; при переоткрытии
страницы можно переподключиться к идущей или недавно завершённой задаче.

Ограничение: задача живёт в процессе backend — перезапуск контейнера прервёт
установку (SSH-сессию держит сам backend).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from sqlalchemy import select

from app.database import async_session_maker
from app.models import Server
from app.services.blocklist_manager import get_blocklist_manager
from app.services.deploy_service import DeployParams, deploy_node
from app.services.ssh_manager import MAXIMUM_PRESET, RECOMMENDED_PRESET, proxy_to_node
from app.services.time_sync import get_time_sync_service

logger = logging.getLogger(__name__)

# Завершённые задачи держим для переподключения и просмотра результата
FINISHED_TTL_SECONDS = 600
# Защита от разрастания памяти на очень длинных логах установки
LOG_BUFFER_LIMIT = 5000
# Сколько ждать появления ноды после ребута из Rescue System (firstboot ставит
# ОС → нода → docker pull может занять ~30 мин на медленном канале)
RESCUE_ONLINE_TIMEOUT = 2400
RESCUE_POLL_INTERVAL = 10


@dataclass
class PostDeployOptions:
    """Постустановочные шаги, выполняемые после успешного развёртывания."""
    ssh_preset: Optional[str] = None
    new_root_password: Optional[str] = None
    haproxy_profile_id: Optional[int] = None
    firewall_profile_id: Optional[int] = None


@dataclass
class DeployJob:
    id: str
    name: str
    host: str
    server_url: str
    status: str = "running"  # running | success | error
    log: list[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    server_id: Optional[int] = None
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task: Optional[asyncio.Task] = None


class DeployJobManager:
    """In-memory реестр фоновых задач развёртывания с pub/sub лога."""

    def __init__(self) -> None:
        self._jobs: dict[str, DeployJob] = {}

    def _cleanup_finished(self) -> None:
        now = time.time()
        expired = [
            jid for jid, job in self._jobs.items()
            if job.finished_at is not None and now - job.finished_at > FINISHED_TTL_SECONDS
        ]
        for jid in expired:
            self._jobs.pop(jid, None)

    def get(self, job_id: str) -> Optional[DeployJob]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        """Активные и недавно завершённые задачи — для восстановления на фронте."""
        self._cleanup_finished()
        return [
            {
                "job_id": job.id,
                "name": job.name,
                "host": job.host,
                "status": job.status,
                "exit_code": job.exit_code,
                "server_id": job.server_id,
                "error": job.error,
            }
            for job in sorted(self._jobs.values(), key=lambda j: j.started_at)
        ]

    def start(
        self,
        params: DeployParams,
        name: str,
        server_url: str,
        post_opts: PostDeployOptions,
    ) -> str:
        self._cleanup_finished()
        job_id = uuid.uuid4().hex
        job = DeployJob(id=job_id, name=name, host=params.host, server_url=server_url)
        self._jobs[job_id] = job
        job.task = asyncio.create_task(self._run(job, params, post_opts))
        return job_id

    def _emit(self, job: DeployJob, event: dict) -> None:
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

    def _finish(self, job: DeployJob, status: str, error: Optional[str] = None) -> None:
        job.status = status
        if error and not job.error:
            job.error = error
        job.finished_at = time.time()

    async def _run(
        self,
        job: DeployJob,
        params: DeployParams,
        post_opts: PostDeployOptions,
    ) -> None:
        try:
            self._emit(job, {"type": "start", "host": params.host})
            async for event in deploy_node(params):
                etype = event.get("type")
                if etype == "done":
                    if event.get("rescue"):
                        await self._on_rescue_reboot(job, params, post_opts)
                    else:
                        await self._on_install_done(job, event.get("exit_code", 1), post_opts)
                    return
                if etype == "error":
                    job.error = event.get("message")
                    self._emit(job, event)
                    self._emit(job, {"type": "done", "exit_code": 1, "server_id": None})
                    self._finish(job, "error")
                    return
                self._emit(job, event)
            # Поток закончился без терминального события — трактуем как сбой
            self._emit(job, {"type": "error", "message": "Установка прервалась без кода завершения"})
            self._emit(job, {"type": "done", "exit_code": 1, "server_id": None})
            self._finish(job, "error", "Установка прервалась без кода завершения")
        except asyncio.CancelledError:
            self._finish(job, "error", "Установка отменена")
            raise
        except Exception as exc:  # noqa: BLE001 — верхняя граница фоновой задачи
            logger.error("Deploy job %s failed: %s", job.id, exc)
            self._emit(job, {"type": "error", "message": str(exc)})
            self._emit(job, {"type": "done", "exit_code": 1, "server_id": None})
            self._finish(job, "error", str(exc))

    async def _on_install_done(
        self,
        job: DeployJob,
        exit_code: int,
        post_opts: PostDeployOptions,
    ) -> None:
        job.exit_code = exit_code
        if exit_code != 0:
            self._emit(job, {"type": "done", "exit_code": exit_code, "server_id": None})
            self._finish(job, "error")
            return

        try:
            server_id = await self._create_server(job.name, job.server_url)
        except Exception as exc:  # noqa: BLE001 — финальная граница создания сервера
            logger.error("Deploy job %s: create server failed: %s", job.id, exc)
            message = f"Установка прошла, но не удалось добавить сервер: {exc}"
            self._emit(job, {"type": "error", "message": message})
            self._emit(job, {"type": "done", "exit_code": exit_code, "server_id": None})
            self._finish(job, "error", message)
            return

        job.server_id = server_id
        await self._post_install(job, server_id, post_opts)
        await self._bind_profiles(job, server_id, post_opts)
        self._emit(job, {"type": "done", "exit_code": 0, "server_id": server_id})
        self._finish(job, "success")

    async def _on_rescue_reboot(
        self,
        job: DeployJob,
        params: DeployParams,
        post_opts: PostDeployOptions,
    ) -> None:
        """Rescue-сценарий: ОС поставлена, сервер ушёл в ребут. Нода доустановится
        сама (firstboot) с тем же NODE_SECRET. Ждём её появления, затем выполняем
        обычные постустановочные шаги (создание записи, SSH-пресет, профили)."""
        self._emit(job, {
            "type": "log",
            "line": "[panel] ОС установлена, сервер перезагружается. Нода доустановится "
                    "автоматически — ожидаю её появления (до 40 мин)...",
        })

        # Транзиентный объект (без записи в БД) — только чтобы поллить ноду по mTLS.
        # Реальную запись создаём через _create_server, когда нода ответит.
        probe = Server(
            name=job.name,
            url=job.server_url.rstrip("/"),
            api_key=None,
            pki_enabled=True,
            uses_shared_cert=True,
        )

        if not await self._wait_node_online(job, probe):
            message = ("Нода не появилась за отведённое время (~40 мин). "
                       "ОС установлена — проверьте сервер вручную.")
            self._emit(job, {"type": "error", "message": message})
            self._emit(job, {"type": "done", "exit_code": 1, "server_id": None})
            self._finish(job, "error", message)
            return

        # Нода онлайн — дальше тот же путь, что и при обычном успешном деплое
        await self._on_install_done(job, 0, post_opts)

    async def _wait_node_online(self, job: DeployJob, server: Server) -> bool:
        """Поллит /api/version ноды до ответа или таймаута. True — нода поднялась.
        Периодический лог служит и keepalive для NDJSON-стрима."""
        deadline = time.time() + RESCUE_ONLINE_TIMEOUT
        attempt = 0
        while time.time() < deadline:
            try:
                await proxy_to_node(server, "GET", "/api/version", timeout=10.0)
                self._emit(job, {"type": "log", "line": "[panel] Нода онлайн — продолжаю настройку"})
                return True
            except Exception:  # noqa: BLE001 — нода ещё грузится, любой сбой = ещё не готова
                pass
            attempt += 1
            if attempt % 3 == 0:  # ~раз в 30 c
                remaining = max(0, int(deadline - time.time()))
                self._emit(job, {"type": "log", "line": f"[panel] Жду ноду... осталось ~{remaining // 60} мин"})
            await asyncio.sleep(RESCUE_POLL_INTERVAL)
        return False

    async def _create_server(self, name: str, url: str) -> int:
        """Создаёт запись ноды после успешной установки (mTLS, shared cert)."""
        async with async_session_maker() as db:
            result = await db.execute(select(Server).order_by(Server.position.desc()))
            last = result.scalars().first()
            server = Server(
                name=name,
                url=url.rstrip("/"),
                api_key=None,
                pki_enabled=True,
                uses_shared_cert=True,
                position=(last.position + 1) if last else 0,
            )
            db.add(server)
            await db.commit()
            await db.refresh(server)
            server_id = server.id

        asyncio.ensure_future(get_blocklist_manager().sync_single_node_by_id(server_id))
        asyncio.ensure_future(get_time_sync_service().sync_single_server(server_id))
        return server_id

    async def _post_install(
        self,
        job: DeployJob,
        server_id: int,
        post_opts: PostDeployOptions,
    ) -> None:
        """SSH-пресет и смена пароля root через API ноды. Best-effort."""
        if not post_opts.ssh_preset and not post_opts.new_root_password:
            return

        async with async_session_maker() as db:
            result = await db.execute(select(Server).where(Server.id == server_id))
            server = result.scalar_one_or_none()
        if not server:
            return

        # Дать ноде подняться и принять mTLS-подключение панели
        await asyncio.sleep(8)
        self._emit(job, {"type": "log", "line": "[panel] Применение SSH-настроек через API ноды..."})

        if post_opts.ssh_preset:
            preset = RECOMMENDED_PRESET if post_opts.ssh_preset == "recommended" else MAXIMUM_PRESET
            label = "рекомендуемый" if post_opts.ssh_preset == "recommended" else "максимальный"
            try:
                await proxy_to_node(server, "POST", "/api/ssh/config", preset["ssh"], timeout=30.0)
                self._emit(job, {"type": "log", "line": f"[panel] SSH-конфиг применён (пресет: {label})"})
            except Exception as exc:  # noqa: BLE001 — best-effort постшаг
                self._emit(job, {"type": "log", "line": f"[panel] SSH-конфиг не применён: {exc}"})
            try:
                await proxy_to_node(
                    server, "POST", "/api/ssh/fail2ban/config", preset["fail2ban"],
                    timeout=120.0, use_apply_client=True,
                )
                self._emit(job, {"type": "log", "line": "[panel] fail2ban настроен"})
            except Exception as exc:  # noqa: BLE001 — best-effort постшаг
                self._emit(job, {"type": "log", "line": f"[panel] fail2ban не настроен: {exc}"})

        if post_opts.new_root_password:
            try:
                await proxy_to_node(
                    server, "POST", "/api/ssh/password",
                    {"user": "root", "password": post_opts.new_root_password},
                    timeout=120.0, use_apply_client=True,
                )
                self._emit(job, {"type": "log", "line": "[panel] Пароль root изменён"})
            except Exception as exc:  # noqa: BLE001 — best-effort постшаг
                self._emit(job, {"type": "log", "line": f"[panel] Не удалось сменить пароль root: {exc}"})

    async def _bind_profiles(
        self,
        job: DeployJob,
        server_id: int,
        post_opts: PostDeployOptions,
    ) -> None:
        """Привязка к HAProxy/Firewall-профилям. Best-effort, лог в стрим."""
        if post_opts.haproxy_profile_id is None and post_opts.firewall_profile_id is None:
            return

        # Ленивый импорт — роутеры профилей зависят от своих сервисов,
        # импорт на уровне модуля создал бы цикл
        from app.routers.firewall_profiles import _bg_sync_profile as firewall_sync
        from app.routers.haproxy_profiles import _bg_sync_profile as haproxy_sync

        if post_opts.haproxy_profile_id is not None:
            try:
                async with async_session_maker() as db:
                    result = await db.execute(select(Server).where(Server.id == server_id))
                    server = result.scalar_one_or_none()
                    if server:
                        server.active_haproxy_profile_id = post_opts.haproxy_profile_id
                        server.haproxy_sync_status = "pending"
                        await db.commit()
                asyncio.ensure_future(haproxy_sync(post_opts.haproxy_profile_id))
                self._emit(job, {"type": "log", "line": "[panel] Привязан к HAProxy-профилю"})
            except Exception as exc:  # noqa: BLE001 — best-effort постшаг
                self._emit(job, {"type": "log", "line": f"[panel] HAProxy-профиль не привязан: {exc}"})

        if post_opts.firewall_profile_id is not None:
            try:
                async with async_session_maker() as db:
                    result = await db.execute(select(Server).where(Server.id == server_id))
                    server = result.scalar_one_or_none()
                    if server:
                        server.active_firewall_profile_id = post_opts.firewall_profile_id
                        server.firewall_sync_status = "pending"
                        await db.commit()
                asyncio.ensure_future(firewall_sync(post_opts.firewall_profile_id))
                self._emit(job, {"type": "log", "line": "[panel] Привязан к Firewall-профилю"})
            except Exception as exc:  # noqa: BLE001 — best-effort постшаг
                self._emit(job, {"type": "log", "line": f"[panel] Firewall-профиль не привязан: {exc}"})

    async def subscribe(self, job_id: str) -> AsyncIterator[dict]:
        """Поток событий задачи: реплей накопленного лога + live до завершения."""
        job = self._jobs.get(job_id)
        if job is None:
            return

        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        # Подписку добавляем ДО снимка лога — иначе строка между снимком и
        # подпиской потеряется. Дубли отсекаются по _idx.
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
                async for event in self._drain_terminal(job):
                    yield event
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

    async def _drain_terminal(self, job: DeployJob) -> AsyncIterator[dict]:
        """Финальные события для уже завершённой задачи (реплей результата)."""
        if job.status == "success":
            yield {"type": "done", "exit_code": job.exit_code or 0, "server_id": job.server_id}
            return
        if job.error:
            yield {"type": "error", "message": job.error}
        yield {
            "type": "done",
            "exit_code": job.exit_code if job.exit_code is not None else 1,
            "server_id": job.server_id,
        }


_manager = DeployJobManager()


def get_deploy_job_manager() -> DeployJobManager:
    return _manager
