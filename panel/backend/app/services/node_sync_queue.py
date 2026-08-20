"""Очередь отложенной синхронизации нод: доставка изменений тем, кто был офлайн.

Панель применяет блок-листы, whitelist анти-DDoS и firewall-профили рассылкой на весь
парк. Нода, лежавшая в этот момент, изменение не получала — и не получила бы уже никогда:
рассылка одноразовая, а следующий полный проход по расписанию бывает раз в сутки. Здесь
за каждой такой нодой записывается долг, а фоновый воркер закрывает его, как только она
снова начинает отвечать. Долг лежит в PostgreSQL, поэтому перезапуск панели его не теряет.

Виды долгов независимы: сбой блок-листа не задерживает whitelist и наоборот.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import and_, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.services.node_capabilities import Capability, allows, server_allows

from app.database import async_session
from app.models import NodePendingSync, Server
from app.services.haproxy_profile_sync import is_server_online

logger = logging.getLogger(__name__)

KIND_BLOCKLIST = "blocklist"
KIND_ANTIDDOS_WHITELIST = "antiddos_whitelist"
KIND_FIREWALL_PROFILE = "firewall_profile"
KIND_DNAT_PROFILE = "dnat_profile"
KIND_RESERVED_PORTS = "reserved_ports"

# Право, без которого долг неисполним. Все виды — запись на ноду; долг,
# который нода не примет никогда, здесь не задерживается: потолка попыток нет,
# и он остался бы в таблице навсегда, дёргая ноду каждые полчаса.
KIND_CAPABILITIES: dict[str, Capability] = {
    KIND_BLOCKLIST: Capability.IPSET,
    KIND_ANTIDDOS_WHITELIST: Capability.ANTIDDOS,
    KIND_FIREWALL_PROFILE: Capability.FIREWALL,
    KIND_DNAT_PROFILE: Capability.DNAT,
    KIND_RESERVED_PORTS: Capability.SYSTEM,
}

RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 1800


def retry_delay(attempts: int) -> int:
    """Отсрочка перед следующей попыткой: нода может отвечать, но стабильно отказывать
    (битое правило, старый агент) — долбить её каждые полминуты бессмысленно."""
    return min(RETRY_BASE_SECONDS * (2 ** max(attempts - 1, 0)), RETRY_MAX_SECONDS)


# Исполнитель одного вида долга: получает ноды, возвращает по каждой None при успехе
# или текст ошибки. Работа берётся пачкой — сборка блок-листа на миллионы адресов
# стоит слишком дорого, чтобы повторять её для каждой ноды отдельно.
SyncHandler = Callable[[list[int]], Awaitable[dict[int, Optional[str]]]]


async def enqueue(server_ids: list[int], kind: str, reason: str = "") -> None:
    """Записать долг перед нодами. Повторная запись не плодит строк и сбрасывает
    отсрочку: появилось новое изменение, ждать конца прошлого backoff незачем."""
    unique_ids = list(dict.fromkeys(server_ids))
    if not unique_ids:
        return

    unique_ids = await _drop_ids_without_capability(unique_ids, kind)
    if not unique_ids:
        return

    now = datetime.now(timezone.utc)
    message = reason[:500] if reason else None
    try:
        async with async_session() as db:
            stmt = pg_insert(NodePendingSync).values([
                {
                    "server_id": sid,
                    "kind": kind,
                    "requested_at": now,
                    "next_attempt_at": now,
                    "attempts": 0,
                    "last_error": message,
                }
                for sid in unique_ids
            ])
            # requested_at обновляется и при конфликте: он же метка версии долга,
            # по которой исполнитель отличает «я это закрыл» от «пока я работал,
            # прилетело новое изменение» — иначе оно стёрлось бы вместе со старым.
            await db.execute(stmt.on_conflict_do_update(
                index_elements=["server_id", "kind"],
                set_={
                    "requested_at": now,
                    "next_attempt_at": now,
                    "attempts": 0,
                    "last_error": message,
                },
            ))
            await db.commit()
    except Exception as e:
        logger.error(f"Failed to enqueue {kind} for servers {unique_ids}: {e}")


async def _drop_ids_without_capability(server_ids: list[int], kind: str) -> list[int]:
    """Отсечь ноды, которые этот вид долга принять не могут."""
    capability = KIND_CAPABILITIES.get(kind)
    if capability is None:
        return server_ids
    try:
        async with async_session() as db:
            rows = (await db.execute(
                select(Server.id, Server.node_capabilities).where(Server.id.in_(server_ids))
            )).all()
    except Exception as e:
        # Не знаем — пропускаем: потерянный долг хуже лишней попытки
        logger.error(f"Capability check before enqueue failed: {e}")
        return server_ids

    forbidden = {
        sid for sid, caps in rows
        if not allows(caps, capability, write=True)
    }
    if not forbidden:
        return server_ids
    logger.debug("Skipping %s for nodes with the section closed: %s", kind, sorted(forbidden))
    return [sid for sid in server_ids if sid not in forbidden]


async def drop_forbidden(server_ids: list[int]) -> None:
    """Снять долги, ставшие неисполнимыми после смены прав на ноде."""
    if not server_ids:
        return
    try:
        async with async_session() as db:
            rows = (await db.execute(
                select(NodePendingSync.server_id, NodePendingSync.kind, Server.node_capabilities)
                .join(Server, Server.id == NodePendingSync.server_id)
                .where(NodePendingSync.server_id.in_(server_ids))
            )).all()

            doomed = [
                (sid, kind) for sid, kind, caps in rows
                if kind in KIND_CAPABILITIES
                and not allows(caps, KIND_CAPABILITIES[kind], write=True)
            ]
            if not doomed:
                return

            for sid, kind in doomed:
                await db.execute(
                    delete(NodePendingSync).where(
                        NodePendingSync.server_id == sid,
                        NodePendingSync.kind == kind,
                    )
                )
            await db.commit()
        logger.info("Dropped %d pending sync(s) closed by node permissions", len(doomed))
    except Exception as e:
        logger.error(f"Failed to drop forbidden pending syncs: {e}")


async def clear(server_id: int, kinds: list[str], before: datetime) -> None:
    """Снять долги, закрытые в обход очереди — например авто-восстановлением ноды,
    которое при её оживании применяет то же самое напрямую.

    `before` — момент, на состояние которого работал вызывающий: долг, поставленный
    позже, относится к изменению, которого он ещё не видел, и остаётся в очереди.
    """
    if not kinds:
        return
    try:
        async with async_session() as db:
            await db.execute(
                delete(NodePendingSync).where(
                    NodePendingSync.server_id == server_id,
                    NodePendingSync.kind.in_(kinds),
                    NodePendingSync.requested_at <= before,
                )
            )
            await db.commit()
    except Exception as e:
        logger.error(f"Failed to clear pending sync {kinds} for server {server_id}: {e}")


class NodeSyncQueue:
    POLL_INTERVAL = 30
    # Стартовая пауза: сразу после подъёма панели last_seen у всех нод протух, и без
    # неё первый проход счёл бы офлайн весь парк, отложив долги на минуту впустую.
    START_DELAY = 45

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Долги, которые прямо сейчас закрываются: цикл короче времени применения
        # блок-листа, и без этого следующий проход запустил бы ту же работу повторно.
        self._inflight: set[tuple[int, str]] = set()

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("NodeSyncQueue started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("NodeSyncQueue stopped")

    def _handlers(self) -> dict[str, SyncHandler]:
        """Импорт внутри вызова: сервисы-исполнители сами ставят долги в очередь,
        а импорт на уровне модуля замкнул бы это в цикл."""
        from app.services.antiddos_manager import get_antiddos_manager
        from app.services.blocklist_manager import get_blocklist_manager
        from app.services.dnat_profile_sync import sync_dnat_to_servers
        from app.services.firewall_profile_sync import sync_firewall_to_servers
        from app.services.reserved_ports_sync import push_reserved_ports_to_servers

        return {
            KIND_BLOCKLIST: get_blocklist_manager().sync_nodes_by_ids,
            KIND_ANTIDDOS_WHITELIST: get_antiddos_manager().push_whitelist_to_servers,
            KIND_FIREWALL_PROFILE: sync_firewall_to_servers,
            KIND_DNAT_PROFILE: sync_dnat_to_servers,
            KIND_RESERVED_PORTS: push_reserved_ports_to_servers,
        }

    async def _loop(self):
        await asyncio.sleep(self.START_DELAY)
        while self._running:
            try:
                await self._process_due()
            except Exception as e:
                logger.error(f"Node sync queue pass failed: {e}")
            await asyncio.sleep(self.POLL_INTERVAL)

    async def _process_due(self):
        now = datetime.now(timezone.utc)
        async with async_session() as db:
            rows = (await db.execute(
                select(NodePendingSync.kind, Server)
                .join(Server, Server.id == NodePendingSync.server_id)
                .where(
                    NodePendingSync.next_attempt_at <= now,
                    Server.is_active.is_(True),
                )
            )).all()

        due: dict[str, list[int]] = defaultdict(list)
        stale: list[tuple[int, str]] = []
        for kind, server in rows:
            capability = KIND_CAPABILITIES.get(kind)
            # Подметальщик: сюда попадают долги, записанные до того, как нода
            # закрыла раздел, — в том числе пережившие перезапуск панели
            if capability is not None and not server_allows(server, capability, write=True):
                stale.append((server.id, kind))
                continue
            if (server.id, kind) in self._inflight or not is_server_online(server):
                continue
            due[kind].append(server.id)

        if stale:
            for kind in {kind for _, kind in stale}:
                await self._drop(kind, [sid for sid, k in stale if k == kind])
            logger.info("Dropped %d pending sync(s) closed by node permissions", len(stale))

        if not due:
            return

        await asyncio.gather(*[self._run_kind(kind, ids) for kind, ids in due.items()])

    async def _run_kind(self, kind: str, server_ids: list[int]):
        handler = self._handlers().get(kind)
        if handler is None:
            # Долг вида, которого в этой версии панели уже нет — иначе висел бы вечно.
            logger.warning("Dropping %d pending sync(s) of unknown kind %s", len(server_ids), kind)
            await self._drop(kind, server_ids)
            return

        # Состояние, с которым работает исполнитель, зафиксировано этим моментом:
        # долг, поставленный позже, он не видел, и закрывать его нельзя.
        started_at = datetime.now(timezone.utc)
        self._inflight.update((sid, kind) for sid in server_ids)
        try:
            results = await handler(server_ids)
        except Exception as e:
            logger.error(f"Pending {kind} sync failed for {server_ids}: {e}")
            await self._defer(kind, {sid: str(e) for sid in server_ids}, started_at)
            return
        finally:
            self._inflight.difference_update((sid, kind) for sid in server_ids)

        done = [sid for sid, error in results.items() if error is None]
        failed = {sid: error for sid, error in results.items() if error is not None}

        await self._drop(kind, done, started_at)
        await self._defer(kind, failed, started_at)

        if done:
            logger.info("Pending %s applied to %d recovered node(s)", kind, len(done))
        if failed:
            logger.warning("Pending %s still failing on %d node(s)", kind, len(failed))

    async def _drop(self, kind: str, server_ids: list[int], started_at: Optional[datetime] = None):
        """Закрыть долги. Записанные уже после начала работы не трогаем — это новые
        изменения, до ноды они ещё не доехали."""
        if not server_ids:
            return
        conditions = [
            NodePendingSync.kind == kind,
            NodePendingSync.server_id.in_(server_ids),
        ]
        if started_at is not None:
            conditions.append(NodePendingSync.requested_at <= started_at)
        async with async_session() as db:
            await db.execute(delete(NodePendingSync).where(*conditions))
            await db.commit()

    async def _defer(self, kind: str, errors: dict[int, str], started_at: datetime):
        """Отодвинуть неудавшиеся долги по экспоненте."""
        if not errors:
            return
        now = datetime.now(timezone.utc)
        async with async_session() as db:
            for server_id, error in errors.items():
                row = await db.get(NodePendingSync, (server_id, kind))
                # Долг, перезаписанный во время работы, несёт свежее изменение —
                # отодвигать его чужой неудачей нельзя.
                if row is None or row.requested_at > started_at:
                    continue
                attempts = (row.attempts or 0) + 1
                delay = retry_delay(attempts)
                await db.execute(
                    update(NodePendingSync)
                    .where(and_(
                        NodePendingSync.server_id == server_id,
                        NodePendingSync.kind == kind,
                        NodePendingSync.requested_at <= started_at,
                    ))
                    .values(
                        attempts=attempts,
                        next_attempt_at=now + timedelta(seconds=delay),
                        last_error=(error or "")[:500],
                    )
                )
            await db.commit()


_queue: Optional[NodeSyncQueue] = None


def get_node_sync_queue() -> NodeSyncQueue:
    global _queue
    if _queue is None:
        _queue = NodeSyncQueue()
    return _queue


async def start_node_sync_queue():
    await get_node_sync_queue().start()


async def stop_node_sync_queue():
    await get_node_sync_queue().stop()
