"""Фоновый цикл exit-прокси: доставка конфига на ноды, сбор статуса, журнал и алерты.

Вся логика выбора выхода живёт на ноде — панель лишь присылает желаемый конфиг
(по хэшу, когда он изменился), забирает статус, переносит события ноды в свой
журнал и шлёт Telegram. Нода, лежавшая в момент изменения, получает конфиг
через очередь долгов (`node_sync_queue`, вид `exit_proxy`).
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_, select, update

from app.database import async_session
from app.models import ExitProxyEvent, ExitProxyNode, Server
from app.services.exit_proxy import node_client
from app.services.exit_proxy.alerts import (
    KIND_SELF_TEST_FAILED,
    KIND_SELF_TEST_RECOVERED,
    KIND_SWITCHED,
    ExitProxyAlerter,
)
from app.services.exit_proxy.node_client import ExitProxyNodeDenied, ExitProxyNodeError, ExitProxyNodeUnsupported
from app.services.exit_proxy.render import NodePrefs, build_node_config, config_hash
from app.services.exit_proxy.settings import SettingsSnapshot, get_or_create_settings
from app.services.exit_proxy.views import new_node_events
from app.services.haproxy_profile_sync import is_server_online

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
START_DELAY_SECONDS = 45
NODE_CONCURRENCY = 10
CHECK_NOW_TIMEOUT_SEC = 180
CHECK_NOW_POLL_SEC = 3

SYNC_SYNCED = "synced"
SYNC_PENDING = "pending"
SYNC_FAILED = "failed"
SYNC_DENIED = "denied"
SYNC_UNSUPPORTED = "unsupported"
OFFLINE_MESSAGE = "нода офлайн"


@dataclass(frozen=True)
class SyncOutcome:
    error: Optional[str] = None
    # Долг в очередь: нода офлайн или запрос сорвался — повторить, когда ответит
    retry: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExitProxyService:
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._cycle_lock = asyncio.Lock()
        self.alerter = ExitProxyAlerter()
        self.last_tick_at: Optional[datetime] = None
        self.last_error: Optional[str] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Exit proxy service started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Exit proxy service stopped")

    def trigger(self) -> None:
        """Прогнать цикл, не дожидаясь тика: настройки или список нод изменились."""
        self._wake.set()

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "last_error": self.last_error,
        }

    async def _loop(self) -> None:
        delay: float = START_DELAY_SECONDS
        while self._running:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.run_cycle()
            except Exception as exc:  # noqa: BLE001 — цикл обязан пережить любой сбой
                logger.error("Exit proxy cycle failed: %s", exc, exc_info=True)
                self.last_error = str(exc)
            delay = TICK_SECONDS

    # ── цикл ──

    async def run_cycle(self) -> None:
        if self._cycle_lock.locked():
            return
        async with self._cycle_lock:
            async with async_session() as db:
                settings = SettingsSnapshot.from_row(await get_or_create_settings(db))
                if not settings.enabled:
                    return
                rows = (await db.execute(
                    select(ExitProxyNode, Server)
                    .join(Server, Server.id == ExitProxyNode.server_id)
                    .where(
                        Server.is_active == True,  # noqa: E712
                        # Выключенная нода с долгом pending — ей ещё надо отвезти enabled=false
                        or_(ExitProxyNode.enabled == True, ExitProxyNode.sync_status == SYNC_PENDING),  # noqa: E712
                    )
                )).all()

            semaphore = asyncio.Semaphore(NODE_CONCURRENCY)

            async def guarded(node: ExitProxyNode, server: Server) -> Optional[str]:
                async with semaphore:
                    return await self._sync_and_queue(settings, node, server)

            outcomes = await asyncio.gather(*(guarded(node, server) for node, server in rows), return_exceptions=True)
            errors = [str(item) for item in outcomes if isinstance(item, Exception)]
            self.last_tick_at = _now()
            self.last_error = "; ".join(errors) or None
            async with async_session() as db:
                row = await get_or_create_settings(db)
                row.last_cycle_at = self.last_tick_at
                row.last_cycle_error = self.last_error
                await db.commit()

    async def _sync_and_queue(self, settings: SettingsSnapshot, node: ExitProxyNode, server: Server) -> Optional[str]:
        outcome = await self.sync_node(settings, node, server)
        if outcome.retry:
            from app.services import node_sync_queue
            await node_sync_queue.enqueue([server.id], node_sync_queue.KIND_EXIT_PROXY, outcome.error or "")
        return outcome.error

    async def sync_node(self, settings: SettingsSnapshot, node: ExitProxyNode, server: Server) -> SyncOutcome:
        """Довезти конфиг (если изменился) и забрать статус одной ноды."""
        if not is_server_online(server):
            return SyncOutcome(OFFLINE_MESSAGE, retry=True)
        try:
            node_client.ensure_node_ready(server)
        except ExitProxyNodeUnsupported as exc:
            await self._set_sync(server.id, SYNC_UNSUPPORTED, str(exc))
            return SyncOutcome(str(exc))
        except ExitProxyNodeDenied as exc:
            await self._set_sync(server.id, SYNC_DENIED, str(exc))
            return SyncOutcome(str(exc))

        desired = build_node_config(settings, NodePrefs.from_row(node))
        digest = config_hash(desired)
        status: Optional[dict] = None
        if digest != node.config_hash or node.sync_status != SYNC_SYNCED:
            try:
                status = await node_client.push_config(server, desired)
            except ExitProxyNodeUnsupported as exc:
                await self._set_sync(server.id, SYNC_UNSUPPORTED, str(exc))
                return SyncOutcome(str(exc))
            except ExitProxyNodeDenied as exc:
                await self._set_sync(server.id, SYNC_DENIED, str(exc))
                return SyncOutcome(str(exc))
            except ExitProxyNodeError as exc:
                await self._set_sync(server.id, SYNC_FAILED, str(exc))
                return SyncOutcome(str(exc), retry=True)
            await self._set_sync(server.id, SYNC_SYNCED, None, digest)

        if status is None:
            try:
                status = await node_client.fetch_status(server)
            except ExitProxyNodeError as exc:
                await self._set_sync(server.id, SYNC_SYNCED, f"статус недоступен: {exc}")
                return SyncOutcome(str(exc))
        await self.absorb_status(settings, node, server, status)
        return SyncOutcome()

    async def _set_sync(self, server_id: int, status: str, error: Optional[str], digest: Optional[str] = None) -> None:
        values: dict = {"sync_status": status, "sync_error": error}
        if digest is not None:
            values.update(config_hash=digest, last_sync_at=_now())
        async with async_session() as db:
            await db.execute(update(ExitProxyNode).where(ExitProxyNode.server_id == server_id).values(**values))
            await db.commit()

    async def absorb_status(self, settings: SettingsSnapshot, node: ExitProxyNode, server: Server, status: dict) -> None:
        """Сохранить статус ноды, перенести её новые события в журнал, уведомить."""
        events = new_node_events(status.get("events") or [], node.last_event_at)
        self_test = status.get("self_test") or None
        self_test_ok = bool(self_test.get("ok")) if self_test else None

        journal: list[tuple[str, Optional[str], Optional[str], str]] = [
            (event.get("kind", ""), event.get("from_candidate"), event.get("to_candidate"), event.get("reason") or "")
            for event in events
        ]
        if node.self_test_ok is not None and self_test_ok is not None and self_test_ok != node.self_test_ok:
            kind = KIND_SELF_TEST_RECOVERED if self_test_ok else KIND_SELF_TEST_FAILED
            journal.append((kind, None, status.get("current"), (self_test or {}).get("error") or ""))

        last_event_at = events[-1]["at"] if events else node.last_event_at
        async with async_session() as db:
            await db.execute(
                update(ExitProxyNode)
                .where(ExitProxyNode.server_id == server.id)
                .values(
                    node_status=json.dumps(status, ensure_ascii=False),
                    current_candidate=status.get("current"),
                    self_test_ok=self_test_ok,
                    last_event_at=last_event_at,
                    last_status_at=_now(),
                )
            )
            for kind, from_value, to_value, reason in journal:
                db.add(ExitProxyEvent(
                    server_id=server.id, kind=kind, from_value=from_value, to_value=to_value, reason=reason[:500],
                ))
            await db.commit()

        # Первый сбор статуса переигрывает хвост старых событий ноды — только в журнал
        if node.last_event_at is None:
            return
        for kind, from_value, to_value, reason in journal:
            if kind == KIND_SWITCHED and from_value is None:
                continue
            await self.alerter.notify(
                kind, server.id, server.name, from_value=from_value, to_value=to_value, reason=reason,
                enabled=settings.telegram_enabled, cooldown_seconds=settings.alert_cooldown_seconds,
            )

    # ── операции по одной ноде ──

    async def _load(self, server_id: int) -> tuple[SettingsSnapshot, ExitProxyNode, Server]:
        async with async_session() as db:
            settings = SettingsSnapshot.from_row(await get_or_create_settings(db))
            row = (await db.execute(
                select(ExitProxyNode, Server)
                .join(Server, Server.id == ExitProxyNode.server_id)
                .where(ExitProxyNode.server_id == server_id)
            )).first()
        if row is None:
            raise LookupError("нода не включена в exit-прокси")
        return settings, row[0], row[1]

    async def sync_one(self, server_id: int) -> Optional[str]:
        settings, node, server = await self._load(server_id)
        return await self._sync_and_queue(settings, node, server)

    async def check_now(self, server_id: int) -> dict:
        settings, node, server = await self._load(server_id)
        if not is_server_online(server):
            raise ExitProxyNodeError(OFFLINE_MESSAGE)
        await node_client.start_check(server)
        deadline = time.monotonic() + CHECK_NOW_TIMEOUT_SEC
        while True:
            await asyncio.sleep(CHECK_NOW_POLL_SEC)
            status = await node_client.fetch_status(server)
            if not status.get("check_in_progress") or time.monotonic() > deadline:
                break
        await self.absorb_status(settings, node, server, status)
        return status

    async def switch(self, server_id: int, candidate: str) -> dict:
        settings, node, server = await self._load(server_id)
        status = await node_client.switch_exit(server, candidate)
        await self.absorb_status(settings, node, server, status)
        return status

    async def push_to_servers(self, server_ids: list[int]) -> dict[int, Optional[str]]:
        """Исполнитель долга `exit_proxy`: None — довезли, текст — повторить позже."""
        async with async_session() as db:
            settings = SettingsSnapshot.from_row(await get_or_create_settings(db))
            rows = (await db.execute(
                select(ExitProxyNode, Server)
                .join(Server, Server.id == ExitProxyNode.server_id)
                .where(ExitProxyNode.server_id.in_(server_ids))
            )).all()
        results: dict[int, Optional[str]] = {server_id: None for server_id in server_ids}
        for node, server in rows:
            outcome = await self.sync_node(settings, node, server)
            results[server.id] = outcome.error if outcome.retry else None
        return results


_service: Optional[ExitProxyService] = None


def get_exit_proxy_service() -> ExitProxyService:
    global _service
    if _service is None:
        _service = ExitProxyService()
    return _service


async def start_exit_proxy() -> None:
    await get_exit_proxy_service().start()


async def stop_exit_proxy() -> None:
    await get_exit_proxy_service().stop()


async def push_exit_proxy_to_servers(server_ids: list[int]) -> dict[int, Optional[str]]:
    return await get_exit_proxy_service().push_to_servers(server_ids)
