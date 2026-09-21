"""Фоновый цикл пула исходящих адресов: доставка конфига на ноды и сбор состояния раскладки.

Раскладку меток по адресам делает нода — панель присылает лишь `enabled` и
список исключений (по хэшу, когда он изменился) и забирает состояние. Нода,
лежавшая в момент изменения, получает конфиг через очередь долгов
(`node_sync_queue`, вид `source_pool`).
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_, select, update

from app.database import async_session
from app.models import Server, SourcePoolNode
from app.services.haproxy_profile_sync import is_server_online
from app.services.source_pool import node_client
from app.services.source_pool.node_client import (
    SourcePoolNodeConflict,
    SourcePoolNodeDenied,
    SourcePoolNodeError,
    SourcePoolNodeUnsupported,
)
from app.services.source_pool.render import build_node_config, config_hash

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


class SourcePoolService:
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._cycle_lock = asyncio.Lock()
        self.last_tick_at: Optional[datetime] = None
        self.last_error: Optional[str] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Source pool service started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Source pool service stopped")

    def trigger(self) -> None:
        self._wake.set()

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
                logger.error("Source pool cycle failed: %s", exc, exc_info=True)
                self.last_error = str(exc)
            delay = TICK_SECONDS

    # ── цикл ──

    async def run_cycle(self) -> None:
        if self._cycle_lock.locked():
            return
        async with self._cycle_lock:
            async with async_session() as db:
                rows = (await db.execute(
                    select(SourcePoolNode, Server)
                    .join(Server, Server.id == SourcePoolNode.server_id)
                    .where(
                        Server.is_active == True,  # noqa: E712
                        # Выключенная нода с долгом pending — ей ещё надо отвезти enabled=false
                        or_(SourcePoolNode.enabled == True, SourcePoolNode.sync_status == SYNC_PENDING),  # noqa: E712
                    )
                )).all()

            semaphore = asyncio.Semaphore(NODE_CONCURRENCY)

            async def guarded(node: SourcePoolNode, server: Server) -> Optional[str]:
                async with semaphore:
                    return await self._sync_and_queue(node, server)

            outcomes = await asyncio.gather(*(guarded(node, server) for node, server in rows), return_exceptions=True)
            errors = [str(item) for item in outcomes if isinstance(item, Exception)]
            self.last_tick_at = _now()
            self.last_error = "; ".join(errors) or None

    async def _sync_and_queue(self, node: SourcePoolNode, server: Server) -> Optional[str]:
        outcome = await self.sync_node(node, server)
        if outcome.retry:
            from app.services import node_sync_queue
            await node_sync_queue.enqueue([server.id], node_sync_queue.KIND_SOURCE_POOL, outcome.error or "")
        return outcome.error

    async def sync_node(self, node: SourcePoolNode, server: Server) -> SyncOutcome:
        """Довезти конфиг (если изменился) и забрать состояние одной ноды."""
        if not is_server_online(server):
            return SyncOutcome(OFFLINE_MESSAGE, retry=True)
        try:
            node_client.ensure_node_ready(server)
        except SourcePoolNodeUnsupported as exc:
            await self._set_sync(server.id, SYNC_UNSUPPORTED, str(exc))
            return SyncOutcome(str(exc))
        except SourcePoolNodeDenied as exc:
            await self._set_sync(server.id, SYNC_DENIED, str(exc))
            return SyncOutcome(str(exc))

        desired = build_node_config(node)
        digest = config_hash(desired)
        state: Optional[dict] = None
        if digest != node.config_hash or node.sync_status != SYNC_SYNCED:
            try:
                state = await node_client.push_config(server, desired)
            except SourcePoolNodeUnsupported as exc:
                await self._set_sync(server.id, SYNC_UNSUPPORTED, str(exc))
                return SyncOutcome(str(exc))
            except SourcePoolNodeDenied as exc:
                await self._set_sync(server.id, SYNC_DENIED, str(exc))
                return SyncOutcome(str(exc))
            except SourcePoolNodeConflict as exc:
                # Детерминированный отказ: повторять смысла нет, пока на ноде включён exit-прокси.
                # Статус остаётся failed, следующий тик попробует снова — без очереди долгов.
                await self._set_sync(server.id, SYNC_FAILED, str(exc))
                return SyncOutcome(str(exc))
            except SourcePoolNodeError as exc:
                await self._set_sync(server.id, SYNC_FAILED, str(exc))
                return SyncOutcome(str(exc), retry=True)
            await self._set_sync(server.id, SYNC_SYNCED, None, digest)

        if state is None:
            try:
                state = await node_client.fetch_state(server)
            except SourcePoolNodeError as exc:
                await self._set_sync(server.id, SYNC_SYNCED, f"состояние недоступно: {exc}")
                return SyncOutcome(str(exc))
        await self._store_state(server.id, state)
        return SyncOutcome()

    async def _set_sync(self, server_id: int, status: str, error: Optional[str], digest: Optional[str] = None) -> None:
        values: dict = {"sync_status": status, "sync_error": error}
        if digest is not None:
            values.update(config_hash=digest, last_sync_at=_now())
        async with async_session() as db:
            await db.execute(update(SourcePoolNode).where(SourcePoolNode.server_id == server_id).values(**values))
            await db.commit()

    async def _store_state(self, server_id: int, state: dict) -> None:
        async with async_session() as db:
            await db.execute(
                update(SourcePoolNode)
                .where(SourcePoolNode.server_id == server_id)
                .values(node_state=json.dumps(state, ensure_ascii=False), last_state_at=_now())
            )
            await db.commit()

    # ── операции по одной ноде ──

    async def _load(self, server_id: int) -> tuple[SourcePoolNode, Server]:
        async with async_session() as db:
            row = (await db.execute(
                select(SourcePoolNode, Server)
                .join(Server, Server.id == SourcePoolNode.server_id)
                .where(SourcePoolNode.server_id == server_id)
            )).first()
        if row is None:
            raise LookupError("пул исходящих адресов на этой ноде не настраивался")
        return row[0], row[1]

    async def sync_one(self, server_id: int) -> Optional[str]:
        node, server = await self._load(server_id)
        return await self._sync_and_queue(node, server)

    async def refresh(self, server_id: int) -> dict:
        """Забрать состояние с ноды сейчас, без доставки конфига."""
        _, server = await self._load(server_id)
        if not is_server_online(server):
            raise SourcePoolNodeError(OFFLINE_MESSAGE)
        state = await node_client.fetch_state(server)
        await self._store_state(server_id, state)
        return state

    async def push_to_servers(self, server_ids: list[int]) -> dict[int, Optional[str]]:
        """Исполнитель долга `source_pool`: None — довезли, текст — повторить позже."""
        async with async_session() as db:
            rows = (await db.execute(
                select(SourcePoolNode, Server)
                .join(Server, Server.id == SourcePoolNode.server_id)
                .where(SourcePoolNode.server_id.in_(server_ids))
            )).all()
        results: dict[int, Optional[str]] = {server_id: None for server_id in server_ids}
        for node, server in rows:
            outcome = await self.sync_node(node, server)
            results[server.id] = outcome.error if outcome.retry else None
        return results


_service: Optional[SourcePoolService] = None


def get_source_pool_service() -> SourcePoolService:
    global _service
    if _service is None:
        _service = SourcePoolService()
    return _service


async def start_source_pool() -> None:
    await get_source_pool_service().start()


async def stop_source_pool() -> None:
    await get_source_pool_service().stop()


async def push_source_pool_to_servers(server_ids: list[int]) -> dict[int, Optional[str]]:
    return await get_source_pool_service().push_to_servers(server_ids)
