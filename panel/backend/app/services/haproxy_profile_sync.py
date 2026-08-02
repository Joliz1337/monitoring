import asyncio
import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_maker
from app.models import Server, HAProxyConfigProfile, HAProxySyncLog
from app.services.http_client import get_node_client, node_auth_headers

logger = logging.getLogger(__name__)

MAX_CONCURRENT_SYNCS = 10

# Сервер считается живым, если метрики обновлялись не дольше этого порога назад.
# Чуть шире окна сбора метрик (10-15с × 3 + запас), чтобы не считать мёртвым из-за одного пропуска.
ONLINE_THRESHOLD_SECONDS = 90


@dataclass
class SyncResult:
    server_id: int
    server_name: str
    success: bool
    message: str
    status: str = "failed"  # success | failed | queued


def compute_config_hash(config_content: str) -> str:
    return hashlib.sha256(config_content.encode()).hexdigest()


def is_server_online(server: Server, threshold: int = ONLINE_THRESHOLD_SECONDS) -> bool:
    """Жив ли сервер — по свежести last_seen."""
    if not server.last_seen:
        return False
    age = (datetime.now(timezone.utc) - server.last_seen).total_seconds()
    return age <= threshold


async def _sync_single_server(
    server: Server,
    config_content: str,
    config_hash: str,
    profile_id: int,
    ensure_started: bool = False,
) -> SyncResult:
    """Применяет конфиг на одну (живую) ноду. Использует собственную сессию БД и коммитит
    результат сразу — статус сервера обновляется в реальном времени, не дожидаясь остальных.

    ensure_started=True заодно поднимает HAProxy, если он остановлен (используется при привязке)."""
    url = f"{server.url}/api/haproxy/config/apply"

    async with async_session_maker() as db:
        try:
            client = get_node_client(server)
            response = await client.post(
                url,
                headers=node_auth_headers(server),
                json={"config_content": config_content, "reload_after": True, "ensure_started": ensure_started},
                timeout=30.0,
            )

            if response.status_code == 200:
                data = response.json()
                ok = data.get("success", True) if isinstance(data, dict) else True
                msg = data.get("message", "Config applied") if isinstance(data, dict) else "Config applied"

                if ok:
                    await db.execute(
                        update(Server)
                        .where(Server.id == server.id)
                        .values(
                            haproxy_config_hash=config_hash,
                            haproxy_last_sync_at=datetime.now(timezone.utc),
                            haproxy_sync_status="synced",
                        )
                    )
                    db.add(HAProxySyncLog(
                        server_id=server.id, profile_id=profile_id,
                        status="success", message=msg, config_hash=config_hash,
                    ))
                    await db.commit()
                    return SyncResult(server.id, server.name, True, msg, status="success")

                return await _record_failure(db, server, profile_id, config_hash, msg)

            error_detail = f"HTTP {response.status_code}"
            try:
                error_detail = response.json().get("detail", error_detail)
            except Exception:
                pass
            return await _record_failure(db, server, profile_id, config_hash, error_detail)

        except httpx.TimeoutException:
            return await _record_failure(db, server, profile_id, config_hash, "Connection timeout")
        except httpx.RequestError as e:
            return await _record_failure(db, server, profile_id, config_hash, f"Connection error: {e}")
        except Exception as e:
            logger.exception("Unexpected error syncing to server %s", server.name)
            return await _record_failure(db, server, profile_id, config_hash, str(e))


async def _record_failure(
    db: AsyncSession, server: Server, profile_id: int, config_hash: str, message: str,
) -> SyncResult:
    await db.execute(
        update(Server).where(Server.id == server.id).values(haproxy_sync_status="failed")
    )
    db.add(HAProxySyncLog(
        server_id=server.id, profile_id=profile_id,
        status="failed", message=message, config_hash=config_hash,
    ))
    await db.commit()
    return SyncResult(server.id, server.name, False, message, status="failed")


async def _queue_offline_server(
    server: Server, profile_id: int, config_hash: str,
) -> SyncResult:
    """Помечает офлайн-сервер как ожидающий синхронизации — без попытки достучаться до ноды."""
    async with async_session_maker() as db:
        await db.execute(
            update(Server).where(Server.id == server.id).values(haproxy_sync_status="pending")
        )
        db.add(HAProxySyncLog(
            server_id=server.id, profile_id=profile_id,
            status="skipped", message="Сервер офлайн — синхронизация отложена до восстановления",
            config_hash=config_hash,
        ))
        await db.commit()
    return SyncResult(server.id, server.name, False, "Сервер офлайн — отложено", status="queued")


async def sync_profile_to_servers(
    profile: HAProxyConfigProfile,
    db: AsyncSession,
    server_ids: list[int] | None = None,
    ensure_started: bool = False,
) -> list[SyncResult]:
    config_hash = compute_config_hash(profile.config_content)

    query = select(Server).where(
        Server.active_haproxy_profile_id == profile.id,
        Server.is_active.is_(True),
    )
    if server_ids:
        query = query.where(Server.id.in_(server_ids))

    result = await db.execute(query)
    servers = list(result.scalars().all())
    if not servers:
        return []

    online = [s for s in servers if is_server_online(s)]
    offline = [s for s in servers if not is_server_online(s)]

    # Живой офлайн уже синхронизированный сервер не трогаем — он не «ждёт».
    offline_pending = [s for s in offline if s.haproxy_config_hash != config_hash]
    offline_synced = [s for s in offline if s.haproxy_config_hash == config_hash]

    # Все, кого реально будем менять, помечаем pending в общей сессии (видно сразу при опросе).
    pending_ids = [s.id for s in online] + [s.id for s in offline_pending]
    if pending_ids:
        await db.execute(
            update(Server).where(Server.id.in_(pending_ids)).values(haproxy_sync_status="pending")
        )
        await db.commit()

    results: list[SyncResult] = [
        SyncResult(s.id, s.name, True, "Уже синхронизирован", status="success")
        for s in offline_synced
    ]

    offline_results = await asyncio.gather(
        *[_queue_offline_server(s, profile.id, config_hash) for s in offline_pending]
    )
    results.extend(offline_results)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SYNCS)

    async def _guarded(server: Server) -> SyncResult:
        async with semaphore:
            return await _sync_single_server(server, profile.config_content, config_hash, profile.id, ensure_started)

    online_results = await asyncio.gather(*[_guarded(s) for s in online])
    results.extend(online_results)
    return results


async def retry_pending_haproxy_syncs(db: AsyncSession) -> int:
    """Досинхронизирует ожившие серверы, у которых раскатка была отложена (offline → online).

    Возвращает число успешно синхронизированных серверов.
    """
    result = await db.execute(
        select(Server).where(
            Server.active_haproxy_profile_id.isnot(None),
            Server.is_active.is_(True),
            Server.haproxy_sync_status == "pending",
        )
    )
    revived = [s for s in result.scalars().all() if is_server_online(s)]
    if not revived:
        return 0

    by_profile: dict[int, list[int]] = defaultdict(list)
    for s in revived:
        by_profile[s.active_haproxy_profile_id].append(s.id)

    synced = 0
    for profile_id, sids in by_profile.items():
        profile = await db.get(HAProxyConfigProfile, profile_id)
        if not profile:
            continue
        # Ожившая привязанная нода должна прийти к состоянию «HAProxy запущен»,
        # как и при онлайн-привязке (вручную остановленные остаются synced, не pending — их не трогаем)
        res = await sync_profile_to_servers(profile, db, server_ids=sids, ensure_started=True)
        synced += sum(1 for r in res if r.status == "success")
    return synced


async def stop_haproxy_on_server(server: Server) -> bool:
    """Останавливает HAProxy на ноде после отвязки от профиля (stop + disable autostart).

    Офлайн-сервер пропускаем — достучаться нельзя. Ошибки связи логируем, но не пробрасываем:
    отвязка в БД уже выполнена, остановка — best-effort.
    """
    if not is_server_online(server):
        logger.info("HAProxy stop пропущен — сервер %s офлайн", server.name)
        return False

    url = f"{server.url}/api/haproxy/stop"
    try:
        client = get_node_client(server)
        response = await client.post(url, headers=node_auth_headers(server), timeout=30.0)
        if response.status_code == 200:
            logger.info("HAProxy остановлен на сервере %s после отвязки", server.name)
            return True
        logger.warning("HAProxy stop на %s не удался: HTTP %s", server.name, response.status_code)
    except httpx.TimeoutException:
        logger.warning("HAProxy stop на %s не удался: таймаут", server.name)
    except httpx.RequestError as e:
        logger.warning("HAProxy stop на %s не удался: %s", server.name, e)
    return False
