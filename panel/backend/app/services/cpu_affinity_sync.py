"""Рассылка настройки «Развод по ядрам» на ноды.

Настройка глобальная, а полное применение системных оптимизаций на ноде занимает
минуты и перезаписывает весь sysctl — гонять его ради одного флага нельзя. Поэтому
у переключателя своя лёгкая ручка на ноде, а панель только разносит новое значение.
"""

import asyncio
import logging

from sqlalchemy import select

from app.database import async_session
from app.models import Server
from app.services.http_client import get_node_client, node_auth_headers
from app.services.node_capabilities import Capability, server_allows

logger = logging.getLogger(__name__)

# Тот же потолок, что у синхронизации времени: на 500 нодах разом это 500 POST,
# конкурирующих с потоком метрик за пул соединений.
CONCURRENCY = 50
NODE_TIMEOUT_SEC = 20.0


async def _push_to_node(server: Server, enabled: bool) -> bool:
    if not server_allows(server, Capability.SYSTEM, write=True):
        return False
    try:
        client = get_node_client(server)
        response = await client.post(
            f"{server.url}/api/system/cpu-affinity",
            json={"enabled": enabled},
            headers=node_auth_headers(server),
            timeout=NODE_TIMEOUT_SEC,
        )
        if response.status_code == 200:
            return True
        logger.warning(
            "cpu-affinity push to %s failed: HTTP %s", server.name, response.status_code
        )
    except Exception as e:
        # Офлайн-нода — обычное дело; она подхватит значение при ближайшем
        # применении системных оптимизаций, которое всегда несёт флаг с собой.
        logger.info("cpu-affinity push to %s skipped: %s", server.name, e)
    return False


async def push_to_all_nodes(enabled: bool) -> dict:
    """Разносит новое значение по активным нодам. Ошибки не фатальны."""
    async with async_session() as db:
        result = await db.execute(
            select(Server).where(Server.is_active == True)  # noqa: E712
        )
        servers = list(result.scalars().all())

    if not servers:
        return {"total": 0, "updated": 0}

    sem = asyncio.Semaphore(CONCURRENCY)

    async def _guarded(srv: Server) -> bool:
        async with sem:
            return await _push_to_node(srv, enabled)

    results = await asyncio.gather(
        *(_guarded(s) for s in servers), return_exceptions=True
    )
    updated = sum(1 for r in results if r is True)
    logger.info("cpu-affinity=%s pushed to %d/%d nodes", enabled, updated, len(servers))
    return {"total": len(servers), "updated": updated}
