import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Server, HAProxyConfigProfile, HAProxySyncLog
from app.services.http_client import get_node_client, node_auth_headers

logger = logging.getLogger(__name__)

MAX_CONCURRENT_SYNCS = 10


@dataclass
class SyncResult:
    server_id: int
    server_name: str
    success: bool
    message: str


def compute_config_hash(config_content: str) -> str:
    return hashlib.sha256(config_content.encode()).hexdigest()


async def _sync_single_server(
    server: Server,
    config_content: str,
    config_hash: str,
    profile_id: int,
    db: AsyncSession,
) -> SyncResult:
    url = f"{server.url}/api/haproxy/config/apply"

    try:
        client = get_node_client(server)
        response = await client.post(
            url,
            headers=node_auth_headers(server),
            json={"config_content": config_content, "reload_after": True},
            timeout=30.0,
        )

        if response.status_code == 200:
            data = response.json()
            ok = data.get("success", True) if isinstance(data, dict) else True
            msg = data.get("message", "Config applied") if isinstance(data, dict) else "Config applied"

            if ok:
                now = datetime.now(timezone.utc)
                await db.execute(
                    update(Server)
                    .where(Server.id == server.id)
                    .values(
                        haproxy_config_hash=config_hash,
                        haproxy_last_sync_at=now,
                        haproxy_sync_status="synced",
                    )
                )
                db.add(HAProxySyncLog(
                    server_id=server.id,
                    profile_id=profile_id,
                    status="success",
                    message=msg,
                    config_hash=config_hash,
                ))
                return SyncResult(server.id, server.name, True, msg)

            await db.execute(
                update(Server).where(Server.id == server.id).values(haproxy_sync_status="failed")
            )
            db.add(HAProxySyncLog(
                server_id=server.id,
                profile_id=profile_id,
                status="failed",
                message=msg,
                config_hash=config_hash,
            ))
            return SyncResult(server.id, server.name, False, msg)

        error_detail = "Unknown error"
        try:
            error_detail = response.json().get("detail", f"HTTP {response.status_code}")
        except Exception:
            error_detail = f"HTTP {response.status_code}"

        await db.execute(
            update(Server).where(Server.id == server.id).values(haproxy_sync_status="failed")
        )
        db.add(HAProxySyncLog(
            server_id=server.id,
            profile_id=profile_id,
            status="failed",
            message=error_detail,
            config_hash=config_hash,
        ))
        return SyncResult(server.id, server.name, False, error_detail)

    except httpx.TimeoutException:
        await db.execute(
            update(Server).where(Server.id == server.id).values(haproxy_sync_status="failed")
        )
        db.add(HAProxySyncLog(
            server_id=server.id, profile_id=profile_id,
            status="failed", message="Connection timeout", config_hash=config_hash,
        ))
        return SyncResult(server.id, server.name, False, "Connection timeout")
    except httpx.RequestError as e:
        msg = f"Connection error: {e}"
        await db.execute(
            update(Server).where(Server.id == server.id).values(haproxy_sync_status="failed")
        )
        db.add(HAProxySyncLog(
            server_id=server.id, profile_id=profile_id,
            status="failed", message=msg, config_hash=config_hash,
        ))
        return SyncResult(server.id, server.name, False, msg)
    except Exception as e:
        msg = str(e)
        logger.exception("Unexpected error syncing to server %s", server.name)
        await db.execute(
            update(Server).where(Server.id == server.id).values(haproxy_sync_status="failed")
        )
        db.add(HAProxySyncLog(
            server_id=server.id, profile_id=profile_id,
            status="failed", message=msg, config_hash=config_hash,
        ))
        return SyncResult(server.id, server.name, False, msg)


async def sync_profile_to_servers(
    profile: HAProxyConfigProfile,
    db: AsyncSession,
    server_ids: list[int] | None = None,
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

    # Пометить все как pending перед началом
    ids = [s.id for s in servers]
    await db.execute(
        update(Server).where(Server.id.in_(ids)).values(haproxy_sync_status="pending")
    )
    await db.flush()

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SYNCS)

    async def _guarded(server: Server) -> SyncResult:
        async with semaphore:
            return await _sync_single_server(server, profile.config_content, config_hash, profile.id, db)

    results = await asyncio.gather(*[_guarded(s) for s in servers])
    await db.commit()
    return list(results)
