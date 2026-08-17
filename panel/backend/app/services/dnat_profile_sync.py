"""Синхронизация DNAT-профиля (проброс портов через iptables nat) с привязанными нодами."""

import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session, async_session_maker
from app.models import DnatProfile, DnatSyncLog, Server
from app.services.haproxy_profile_sync import is_server_online
from app.services.http_client import get_node_apply_client, node_auth_headers
from app.services.node_capabilities import Capability, denied_message, server_allows
from app.services.node_sync_queue import KIND_DNAT_PROFILE, enqueue

logger = logging.getLogger(__name__)

MAX_CONCURRENT_SYNCS = 10
APPLY_TIMEOUT_SECONDS = 60.0
QUEUED_MESSAGE = "Сервер офлайн — синхронизация отложена до восстановления"
OLD_NODE_MESSAGE = "На ноде нет модуля DNAT — обновите агент ноды"


@dataclass
class SyncResult:
    server_id: int
    server_name: str
    success: bool
    message: str
    queued: bool = False


def normalize_rule(rule: dict) -> dict:
    """Каноничный вид правила для хэша. Комментарий не участвует.
    Формула обязана совпадать с normalize_rule в node/app/services/dnat_manager.py."""
    listen_port = int(rule.get("listen_port", 0))
    end = rule.get("listen_port_end")
    end = int(end) if end not in (None, "", 0) else None
    if end == listen_port:
        end = None
    return {
        "name": str(rule.get("name", "")),
        "protocol": (rule.get("protocol") or "tcp").lower(),
        "listen_port": listen_port,
        "listen_port_end": end,
        "target_ip": str(rule.get("target_ip", "")).strip(),
        "target_port": int(rule.get("target_port") or 0),
        "masquerade": bool(rule.get("masquerade", True)),
        "enabled": bool(rule.get("enabled", True)),
    }


def compute_rules_hash(rules_json: str) -> str:
    try:
        rules = json.loads(rules_json) if rules_json else []
    except (json.JSONDecodeError, TypeError):
        rules = []
    canonical = sorted((normalize_rule(r) for r in rules), key=lambda r: r["name"])
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def load_rules(profile: DnatProfile) -> list[dict]:
    try:
        rules = json.loads(profile.rules_json) if profile.rules_json else []
        return rules if isinstance(rules, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


async def _log_failure(
    db: AsyncSession, server: Server, profile_id: int, message: str, rules_hash: str,
    retryable: bool = False,
) -> SyncResult:
    await db.execute(
        update(Server).where(Server.id == server.id).values(dnat_sync_status="failed")
    )
    db.add(DnatSyncLog(
        server_id=server.id, profile_id=profile_id, status="failed",
        message=message, rules_hash=rules_hash,
    ))
    await db.commit()
    # Обрыв связи — повторим, когда нода вернётся; отказ самой ноды повторится с тем же итогом
    if retryable:
        await enqueue([server.id], KIND_DNAT_PROFILE, message)
    return SyncResult(server.id, server.name, False, message)


async def _queue_offline_servers(
    servers: list[Server], profile_id: int, rules_hash: str,
) -> list[SyncResult]:
    if not servers:
        return []
    async with async_session_maker() as db:
        db.add_all([
            DnatSyncLog(
                server_id=server.id, profile_id=profile_id, status="skipped",
                message=QUEUED_MESSAGE, rules_hash=rules_hash,
            )
            for server in servers
        ])
        await db.commit()
    await enqueue([s.id for s in servers], KIND_DNAT_PROFILE, QUEUED_MESSAGE)
    return [SyncResult(s.id, s.name, False, QUEUED_MESSAGE, queued=True) for s in servers]


async def _sync_single_server(
    server: Server, rules: list[dict], rules_hash: str, profile_id: int, queue_failures: bool,
) -> SyncResult:
    """Своя короткая сессия БД на каждую ноду: статус виден сразу, а коннект пула
    не держится на весь fan-out."""
    url = f"{server.url.rstrip('/')}/api/dnat/apply"

    async with async_session_maker() as db:
        try:
            client = get_node_apply_client(server)
            response = await client.post(
                url, headers=node_auth_headers(server), json={"rules": rules},
                timeout=APPLY_TIMEOUT_SECONDS,
            )

            if response.status_code == 404:
                return await _log_failure(db, server, profile_id, OLD_NODE_MESSAGE, rules_hash)

            if response.status_code != 200:
                error_detail = f"HTTP {response.status_code}"
                try:
                    detail = response.json().get("detail", error_detail)
                    error_detail = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
                except Exception:
                    pass
                return await _log_failure(db, server, profile_id, error_detail, rules_hash)

            data = response.json() if response.content else {}
            ok = bool(data.get("success", False))
            msg = data.get("message", "Rules applied")
            node_hash = data.get("rules_hash") or rules_hash

            if not ok:
                if data.get("error_log"):
                    msg = f"{msg}: {data['error_log']}"
                return await _log_failure(db, server, profile_id, msg, rules_hash)

            now = datetime.now(timezone.utc)
            await db.execute(
                update(Server).where(Server.id == server.id).values(
                    dnat_rules_hash=node_hash, dnat_last_sync_at=now, dnat_sync_status="synced",
                )
            )
            db.add(DnatSyncLog(
                server_id=server.id, profile_id=profile_id, status="success",
                message=msg, rules_hash=node_hash,
            ))
            await db.commit()
            return SyncResult(server.id, server.name, True, msg)

        except httpx.TimeoutException:
            return await _log_failure(
                db, server, profile_id, "Connection timeout", rules_hash, retryable=queue_failures,
            )
        except httpx.RequestError as e:
            return await _log_failure(
                db, server, profile_id, f"Connection error: {e}", rules_hash, retryable=queue_failures,
            )
        except Exception as e:
            logger.exception("Unexpected error syncing DNAT profile to server %s", server.name)
            return await _log_failure(db, server, profile_id, str(e), rules_hash)


async def sync_profile_to_servers(
    profile: DnatProfile,
    db: AsyncSession,
    server_ids: list[int] | None = None,
    queue_failures: bool = True,
) -> list[SyncResult]:
    """Раскатать профиль на привязанные серверы (или подмножество).

    `queue_failures=False` — вызов из очереди отложенных синков: она сама ведёт
    повторы, а собственная постановка долга обнулила бы её выдержку.
    """
    rules = load_rules(profile)
    rules_hash = compute_rules_hash(profile.rules_json)

    query = select(Server).where(
        Server.active_dnat_profile_id == profile.id,
        Server.is_active.is_(True),
    )
    if server_ids:
        query = query.where(Server.id.in_(server_ids))
    servers = list((await db.execute(query)).scalars().all())
    if not servers:
        return []

    denied = [s for s in servers if not server_allows(s, Capability.DNAT, write=True)]
    servers = [s for s in servers if s not in denied]

    if servers:
        await db.execute(
            update(Server).where(Server.id.in_([s.id for s in servers])).values(dnat_sync_status="pending")
        )
    if denied:
        await db.execute(
            update(Server).where(Server.id.in_([s.id for s in denied])).values(dnat_sync_status="denied")
        )
    await db.commit()

    results: list[SyncResult] = [
        SyncResult(s.id, s.name, False, denied_message(Capability.DNAT, True)) for s in denied
    ]

    online = [s for s in servers if is_server_online(s)]
    offline = [s for s in servers if not is_server_online(s)]
    results.extend(await _queue_offline_servers(offline, profile.id, rules_hash))

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SYNCS)

    async def _guarded(server: Server) -> SyncResult:
        async with semaphore:
            return await _sync_single_server(server, rules, rules_hash, profile.id, queue_failures)

    results.extend(await asyncio.gather(*[_guarded(s) for s in online]))
    return results


async def sync_dnat_to_servers(server_ids: list[int]) -> dict[int, Optional[str]]:
    """Досинхронизировать DNAT-профиль перечисленным нодам (исполнитель очереди)."""
    async with async_session() as db:
        servers = list((await db.execute(
            select(Server).where(
                Server.id.in_(server_ids),
                Server.is_active.is_(True),
                Server.active_dnat_profile_id.isnot(None),
            )
        )).scalars().all())

    found = {s.id for s in servers}
    results: dict[int, Optional[str]] = {sid: None for sid in server_ids if sid not in found}

    by_profile: dict[int, list[int]] = defaultdict(list)
    for server in servers:
        by_profile[server.active_dnat_profile_id].append(server.id)

    for profile_id, sids in by_profile.items():
        async with async_session() as db:
            profile = await db.get(DnatProfile, profile_id)
            if not profile:
                results.update({sid: None for sid in sids})
                continue
            synced = await sync_profile_to_servers(profile, db, server_ids=sids, queue_failures=False)
            for sync in synced:
                denied_here = sync.message == denied_message(Capability.DNAT, True)
                results[sync.server_id] = None if (sync.success or denied_here) else sync.message

    return results
