"""Фоновый цикл GRE-туннелей: доставка списка туннелей на ноды, сбор состояния и
наблюдение за входящими пакетами.

Туннели держит сама нода (служба на хосте), панель присылает список по хэшу,
когда он изменился, раз в минуту забирает состояние и по счётчику входящих
пакетов замечает, что туннель замолчал. Нода, лежавшая в момент изменения,
получает список через очередь долгов (`node_sync_queue`, вид `gre`).
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update

from app.database import async_session
from app.models import GreNode, Server
from app.services.exit_proxy.settings import load_json
from app.services.gre import alerts, node_client
from app.services.gre.activity import update_activity
from app.services.gre.node_client import GreNodeDenied, GreNodeError, GreNodeRejected, GreNodeUnsupported
from app.services.gre.render import build_node_config, config_hash, load_tunnels
from app.services.haproxy_profile_sync import is_server_online

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
START_DELAY_SECONDS = 45
NODE_CONCURRENCY = 10

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


def needs_cycle(row: GreNode) -> bool:
    """Туннели заданы — следить; список опустел — только довезти снятие."""
    return bool(load_tunnels(row)) or row.sync_status == SYNC_PENDING


class GreService:
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._cycle_lock = asyncio.Lock()
        self.last_tick_at: Optional[datetime] = None
        self.last_error: Optional[str] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("GRE service started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("GRE service stopped")

    async def _loop(self) -> None:
        await asyncio.sleep(START_DELAY_SECONDS)
        while self._running:
            try:
                await self.run_cycle()
            except Exception as exc:  # noqa: BLE001 — цикл обязан пережить любой сбой
                logger.error("GRE cycle failed: %s", exc, exc_info=True)
                self.last_error = str(exc)
            await asyncio.sleep(TICK_SECONDS)

    # ── цикл ──

    async def run_cycle(self) -> None:
        if self._cycle_lock.locked():
            return
        async with self._cycle_lock:
            async with async_session() as db:
                rows = (await db.execute(
                    select(GreNode, Server)
                    .join(Server, Server.id == GreNode.server_id)
                    .where(Server.is_active == True)  # noqa: E712
                )).all()

            semaphore = asyncio.Semaphore(NODE_CONCURRENCY)

            async def guarded(node: GreNode, server: Server) -> Optional[str]:
                async with semaphore:
                    return await self._sync_and_queue(node, server)

            outcomes = await asyncio.gather(
                *(guarded(node, server) for node, server in rows if needs_cycle(node)), return_exceptions=True,
            )
            errors = [str(item) for item in outcomes if isinstance(item, Exception)]
            self.last_tick_at = _now()
            self.last_error = "; ".join(errors) or None

    async def _sync_and_queue(self, node: GreNode, server: Server) -> Optional[str]:
        outcome = await self.sync_node(node, server)
        if outcome.retry:
            from app.services import node_sync_queue
            await node_sync_queue.enqueue([server.id], node_sync_queue.KIND_GRE, outcome.error or "")
        return outcome.error

    async def sync_node(self, node: GreNode, server: Server) -> SyncOutcome:
        """Довезти список (если изменился) и забрать состояние одной ноды."""
        if not is_server_online(server):
            return SyncOutcome(OFFLINE_MESSAGE, retry=True)
        try:
            node_client.ensure_node_ready(server)
        except GreNodeUnsupported as exc:
            await self._set_sync(server.id, SYNC_UNSUPPORTED, str(exc))
            return SyncOutcome(str(exc))
        except GreNodeDenied as exc:
            await self._set_sync(server.id, SYNC_DENIED, str(exc))
            return SyncOutcome(str(exc))

        desired = build_node_config(node)
        digest = config_hash(desired)
        state: Optional[dict] = None
        if digest != node.config_hash or node.sync_status != SYNC_SYNCED:
            try:
                state = await node_client.push_config(server, desired)
            except GreNodeUnsupported as exc:
                await self._set_sync(server.id, SYNC_UNSUPPORTED, str(exc))
                return SyncOutcome(str(exc))
            except GreNodeDenied as exc:
                await self._set_sync(server.id, SYNC_DENIED, str(exc))
                return SyncOutcome(str(exc))
            except GreNodeRejected as exc:
                # Отказ по существу: без правки туннеля повтор кончится тем же — без очереди долгов
                await self._set_sync(server.id, SYNC_FAILED, str(exc))
                return SyncOutcome(str(exc))
            except GreNodeError as exc:
                await self._set_sync(server.id, SYNC_FAILED, str(exc))
                return SyncOutcome(str(exc), retry=True)
            await self._set_sync(server.id, SYNC_SYNCED, None, digest)

        if state is None:
            try:
                state = await node_client.fetch_state(server)
            except GreNodeError as exc:
                await self._set_sync(server.id, SYNC_SYNCED, f"состояние недоступно: {exc}")
                return SyncOutcome(str(exc))
        await self._store_state(server, state)
        return SyncOutcome()

    async def _set_sync(self, server_id: int, status: str, error: Optional[str], digest: Optional[str] = None) -> None:
        values: dict = {"sync_status": status, "sync_error": error}
        if digest is not None:
            values.update(config_hash=digest, last_sync_at=_now())
        async with async_session() as db:
            await db.execute(update(GreNode).where(GreNode.server_id == server_id).values(**values))
            await db.commit()

    async def _store_state(self, server: Server, state: dict) -> None:
        async with async_session() as db:
            node = (await db.execute(select(GreNode).where(GreNode.server_id == server.id))).scalar_one_or_none()
            if node is None:
                return
            activity, changes = update_activity(load_json(node.activity, {}), state.get("tunnels") or [], _now())
            node.node_state = json.dumps(state, ensure_ascii=False)
            node.activity = json.dumps(activity)
            node.last_state_at = _now()
            notify = bool(node.notify)
            await db.commit()
        for change in changes:
            await alerts.notify(server.id, server.name, change, notify)

    # ── операции по одной ноде ──

    async def _load(self, server_id: int) -> tuple[GreNode, Server]:
        async with async_session() as db:
            row = (await db.execute(
                select(GreNode, Server)
                .join(Server, Server.id == GreNode.server_id)
                .where(GreNode.server_id == server_id)
            )).first()
        if row is None:
            raise LookupError("GRE-туннели на этом сервере не настраивались")
        return row[0], row[1]

    async def sync_one(self, server_id: int) -> Optional[str]:
        node, server = await self._load(server_id)
        return await self._sync_and_queue(node, server)

    async def refresh(self, server_id: int) -> None:
        """Забрать состояние с ноды сейчас, без доставки списка."""
        _, server = await self._load(server_id)
        if not is_server_online(server):
            raise GreNodeError(OFFLINE_MESSAGE)
        await self._store_state(server, await node_client.fetch_state(server))

    async def push_to_servers(self, server_ids: list[int]) -> dict[int, Optional[str]]:
        """Исполнитель долга `gre`: None — довезли, текст — повторить позже."""
        async with async_session() as db:
            rows = (await db.execute(
                select(GreNode, Server)
                .join(Server, Server.id == GreNode.server_id)
                .where(GreNode.server_id.in_(server_ids))
            )).all()
        results: dict[int, Optional[str]] = {server_id: None for server_id in server_ids}
        for node, server in rows:
            outcome = await self.sync_node(node, server)
            results[server.id] = outcome.error if outcome.retry else None
        return results


_service: Optional[GreService] = None


def get_gre_service() -> GreService:
    global _service
    if _service is None:
        _service = GreService()
    return _service


async def start_gre() -> None:
    await get_gre_service().start()


async def stop_gre() -> None:
    await get_gre_service().stop()


async def push_gre_to_servers(server_ids: list[int]) -> dict[int, Optional[str]]:
    return await get_gre_service().push_to_servers(server_ids)
