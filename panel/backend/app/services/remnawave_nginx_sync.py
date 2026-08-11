"""Раскатка профилей nginx Remnawave на ноды.

Зеркало haproxy_profile_sync с одним отличием: профиль — шаблон с {{DOMAIN}},
перед отправкой рендерится per-node доменом сервера, и хэш считается от
ОТРЕНДЕРЕННОГО контента (иначе drift-детекция сравнивала бы шаблон с
реальным файлом ноды и всегда видела расхождение).
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_maker
from app.models import PanelSettings, RemnawaveNginxProfile, RemnawaveNginxSyncLog, Server
from app.services.haproxy_profile_sync import compute_config_hash, is_server_online
from app.services.http_client import get_node_client, node_auth_headers
from app.services.node_capabilities import Capability, server_allows
from app.services.remnawave_nginx_config import DOMAIN_PLACEHOLDER, render_for_server

logger = logging.getLogger(__name__)

MAX_CONCURRENT_SYNCS = 10
APPLY_TIMEOUT_SECONDS = 60.0

DEFAULT_REMNAWAVE_NGINX_PATH = "/opt/remnawave"
REMNAWAVE_NGINX_PATH_KEY = "remnawave_nginx_path"

MIN_NODE_VERSION_MESSAGE = (
    "Нода не поддерживает управление nginx Remnawave — обновите агент ноды (нужна версия ≥ 10.7.0)"
)


@dataclass
class SyncResult:
    server_id: int
    server_name: str
    success: bool
    message: str
    status: str = "failed"  # success | failed | queued


async def get_remnawave_nginx_path(db: AsyncSession) -> str:
    result = await db.execute(
        select(PanelSettings.value).where(PanelSettings.key == REMNAWAVE_NGINX_PATH_KEY)
    )
    value = result.scalar_one_or_none()
    return (value or "").strip() or DEFAULT_REMNAWAVE_NGINX_PATH


def render_profile_for_server(template: str, server: Server) -> tuple[Optional[str], Optional[str]]:
    """(rendered, error): рендер шаблона доменом сервера либо причина отказа."""
    if DOMAIN_PLACEHOLDER not in template:
        return template, None
    domain = (server.remnawave_nginx_domain or "").strip()
    if not domain:
        return None, "У сервера не задан домен — укажите его в привязке к профилю"
    return render_for_server(template, domain), None


async def _record_failure(
    db: AsyncSession, server: Server, profile_id: int, config_hash: Optional[str], message: str,
) -> SyncResult:
    await db.execute(
        update(Server).where(Server.id == server.id).values(remnawave_nginx_sync_status="failed")
    )
    db.add(RemnawaveNginxSyncLog(
        server_id=server.id, profile_id=profile_id,
        status="failed", message=message, config_hash=config_hash,
    ))
    await db.commit()
    return SyncResult(server.id, server.name, False, message, status="failed")


async def _sync_single_server(
    server: Server,
    template: str,
    profile_id: int,
    install_path: str,
    ensure_started: bool = False,
) -> SyncResult:
    """Применяет отрендеренный конфиг на одну (живую) ноду. Собственная сессия БД,
    коммит сразу — статус сервера обновляется в реальном времени."""
    async with async_session_maker() as db:
        rendered, render_error = render_profile_for_server(template, server)
        if render_error:
            return await _record_failure(db, server, profile_id, None, render_error)

        config_hash = compute_config_hash(rendered)
        url = f"{server.url}/api/remnawave/nginx/config/apply"
        try:
            client = get_node_client(server)
            response = await client.post(
                url,
                headers=node_auth_headers(server),
                json={
                    "path": install_path,
                    "content": rendered,
                    "reload_after": True,
                    "ensure_started": ensure_started,
                },
                timeout=APPLY_TIMEOUT_SECONDS,
            )

            if response.status_code == 200:
                data = response.json()
                if not isinstance(data, dict):
                    data = {}
                ok = data.get("success", True)
                msg = data.get("message", "Конфиг применён")

                if ok:
                    if data.get("remounted"):
                        logger.info(
                            "Remnawave nginx: нода %s переведена на полный nginx.conf, "
                            "контейнер пересоздан", server.name,
                        )
                    await db.execute(
                        update(Server)
                        .where(Server.id == server.id)
                        .values(
                            remnawave_nginx_config_hash=config_hash,
                            # Нода подставляет свои лимиты, поэтому итоговый файл
                            # отличается от отправленного — эталон для drift берём
                            # из её ответа
                            remnawave_nginx_node_hash=data.get("hash"),
                            remnawave_nginx_last_sync_at=datetime.now(timezone.utc),
                            remnawave_nginx_sync_status="synced",
                        )
                    )
                    db.add(RemnawaveNginxSyncLog(
                        server_id=server.id, profile_id=profile_id,
                        status="success", message=msg, config_hash=config_hash,
                    ))
                    await db.commit()
                    return SyncResult(server.id, server.name, True, msg, status="success")

                return await _record_failure(db, server, profile_id, config_hash, msg)

            if response.status_code == 404:
                return await _record_failure(
                    db, server, profile_id, config_hash, MIN_NODE_VERSION_MESSAGE
                )

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
            logger.exception("Unexpected error syncing remnawave nginx to server %s", server.name)
            return await _record_failure(db, server, profile_id, config_hash, str(e))


async def _queue_offline_server(server: Server, profile_id: int) -> SyncResult:
    async with async_session_maker() as db:
        await db.execute(
            update(Server).where(Server.id == server.id).values(remnawave_nginx_sync_status="pending")
        )
        db.add(RemnawaveNginxSyncLog(
            server_id=server.id, profile_id=profile_id,
            status="skipped", message="Сервер офлайн — синхронизация отложена до восстановления",
        ))
        await db.commit()
    return SyncResult(server.id, server.name, False, "Сервер офлайн — отложено", status="queued")


def _rendered_hash_or_none(template: str, server: Server) -> Optional[str]:
    rendered, error = render_profile_for_server(template, server)
    return None if error else compute_config_hash(rendered)


async def sync_profile_to_servers(
    profile: RemnawaveNginxProfile,
    db: AsyncSession,
    server_ids: list[int] | None = None,
    ensure_started: bool = False,
) -> list[SyncResult]:
    install_path = await get_remnawave_nginx_path(db)

    query = select(Server).where(
        Server.active_remnawave_nginx_profile_id == profile.id,
        Server.is_active.is_(True),
    )
    if server_ids:
        query = query.where(Server.id.in_(server_ids))

    result = await db.execute(query)
    servers = list(result.scalars().all())
    if not servers:
        return []

    # Закрытый раздел: статус denied, не pending — pending заставил бы
    # retry_pending_remnawave_nginx_syncs дёргать ноду каждые полминуты
    denied = [s for s in servers if not server_allows(s, Capability.REMNAWAVE, write=True)]
    if denied:
        await db.execute(
            update(Server).where(Server.id.in_([s.id for s in denied]))
            .values(remnawave_nginx_sync_status="denied")
        )
        await db.commit()
        servers = [s for s in servers if s not in denied]

    online = [s for s in servers if is_server_online(s)]
    offline = [s for s in servers if not is_server_online(s)]

    # Хэш per-server (рендер доменом): офлайн уже синхронизированный не трогаем
    offline_pending = [
        s for s in offline
        if s.remnawave_nginx_config_hash != _rendered_hash_or_none(profile.config_content, s)
        or s.remnawave_nginx_config_hash is None
    ]
    offline_synced = [s for s in offline if s not in offline_pending]

    pending_ids = [s.id for s in online] + [s.id for s in offline_pending]
    if pending_ids:
        await db.execute(
            update(Server).where(Server.id.in_(pending_ids)).values(remnawave_nginx_sync_status="pending")
        )
        await db.commit()

    results: list[SyncResult] = [
        SyncResult(s.id, s.name, True, "Уже синхронизирован", status="success")
        for s in offline_synced
    ]

    offline_results = await asyncio.gather(
        *[_queue_offline_server(s, profile.id) for s in offline_pending]
    )
    results.extend(offline_results)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SYNCS)

    async def _guarded(server: Server) -> SyncResult:
        async with semaphore:
            return await _sync_single_server(
                server, profile.config_content, profile.id, install_path, ensure_started
            )

    online_results = await asyncio.gather(*[_guarded(s) for s in online])
    results.extend(online_results)
    return results


async def retry_pending_remnawave_nginx_syncs(db: AsyncSession) -> int:
    """Досинхронизирует ожившие серверы с отложенной раскаткой (offline → online)."""
    result = await db.execute(
        select(Server).where(
            Server.active_remnawave_nginx_profile_id.isnot(None),
            Server.is_active.is_(True),
            Server.remnawave_nginx_sync_status == "pending",
        )
    )
    revived = [s for s in result.scalars().all() if is_server_online(s)]
    if not revived:
        return 0

    by_profile: dict[int, list[int]] = defaultdict(list)
    for s in revived:
        by_profile[s.active_remnawave_nginx_profile_id].append(s.id)

    synced = 0
    for profile_id, sids in by_profile.items():
        profile = await db.get(RemnawaveNginxProfile, profile_id)
        if not profile:
            continue
        res = await sync_profile_to_servers(profile, db, server_ids=sids, ensure_started=True)
        synced += sum(1 for r in res if r.status == "success")
    return synced
