"""Синхронизация firewall-профиля с привязанными нодами."""

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
from app.models import Server, FirewallProfile, FirewallSyncLog
from app.services.haproxy_profile_sync import is_server_online
from app.services.http_client import get_node_apply_client, node_auth_headers
from app.services.node_sync_queue import KIND_FIREWALL_PROFILE, enqueue

logger = logging.getLogger(__name__)

MAX_CONCURRENT_SYNCS = 10
APPLY_TIMEOUT_SECONDS = 120.0
QUEUED_MESSAGE = "Сервер офлайн — синхронизация отложена до восстановления"


@dataclass
class SyncResult:
    server_id: int
    server_name: str
    success: bool
    message: str
    rolled_back: bool = False
    queued: bool = False


def _normalize_rule(rule: dict) -> dict:
    """Каноничный вид правила для хэша. Comment в хэш не входит — UFW
    не сохраняет комментарии через `ufw allow ...`, поэтому после apply
    оно всегда пустое, и сравнение с панелью даст ложный drift.
    Формула обязана совпадать с FirewallManager._normalize_rule на ноде.
    """
    return {
        "port": int(rule.get("port", 0)),
        "protocol": (rule.get("protocol") or "tcp").lower(),
        "action": (rule.get("action") or "allow").lower(),
        "from_ip": rule.get("from_ip") or None,
        "direction": (rule.get("direction") or "in").lower(),
    }


def compute_rules_hash(rules_json: str, default_in: str, default_out: str) -> str:
    """SHA256 от каноничного представления правил + дефолт-политик."""
    try:
        rules = json.loads(rules_json) if rules_json else []
    except (json.JSONDecodeError, TypeError):
        rules = []

    canonical = {
        "rules": sorted(
            (_normalize_rule(r) for r in rules),
            key=lambda r: (r["direction"], r["action"], r["port"], r["protocol"], r["from_ip"] or ""),
        ),
        "default_incoming": (default_in or "deny").lower(),
        "default_outgoing": (default_out or "allow").lower(),
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


async def _log_failure(
    db: AsyncSession, server: Server, profile_id: int, message: str, rules_hash: str,
    retryable: bool = False,
) -> SyncResult:
    await db.execute(
        update(Server).where(Server.id == server.id).values(firewall_sync_status="failed")
    )
    db.add(FirewallSyncLog(
        server_id=server.id,
        profile_id=profile_id,
        status="failed",
        message=message,
        rules_hash=rules_hash,
    ))
    await db.commit()
    # Нода отвалилась посреди раскатки — правила до неё не доехали. Отказ самой ноды
    # (битое правило, старый агент) повторять смысла нет: он повторится с тем же итогом.
    if retryable:
        await enqueue([server.id], KIND_FIREWALL_PROFILE, message)
    return SyncResult(server.id, server.name, False, message)


async def _queue_offline_servers(
    servers: list[Server], profile_id: int, rules_hash: str,
) -> list[SyncResult]:
    """Отложить раскатку офлайн-нодам — без попытки достучаться до них.

    Иначе каждая лежащая нода стоила бы полного APPLY_TIMEOUT_SECONDS ожидания
    и оседала бы в статусе failed без единого повтора.
    """
    if not servers:
        return []

    async with async_session_maker() as db:
        db.add_all([
            FirewallSyncLog(
                server_id=server.id,
                profile_id=profile_id,
                status="skipped",
                message=QUEUED_MESSAGE,
                rules_hash=rules_hash,
            )
            for server in servers
        ])
        await db.commit()

    await enqueue([s.id for s in servers], KIND_FIREWALL_PROFILE, QUEUED_MESSAGE)
    return [
        SyncResult(s.id, s.name, False, QUEUED_MESSAGE, queued=True) for s in servers
    ]


async def _sync_single_server(
    server: Server,
    rules: list[dict],
    default_in: str,
    default_out: str,
    rules_hash: str,
    profile_id: int,
    force: bool,
    queue_failures: bool = True,
) -> SyncResult:
    """Применяет профиль на одну ноду. Собственная короткая сессия БД с немедленным
    коммитом результата — статус виден сразу, и один коннект пула не держится за весь
    fan-out (AsyncSession не поддерживает конкурентную запись из gather)."""
    url = f"{server.url.rstrip('/')}/api/firewall/profile/apply"
    payload = {
        "rules": rules,
        "default_incoming": default_in,
        "default_outgoing": default_out,
        "force": force,
    }

    async with async_session_maker() as db:
        try:
            client = get_node_apply_client(server)
            response = await client.post(
                url,
                headers=node_auth_headers(server),
                json=payload,
                timeout=APPLY_TIMEOUT_SECONDS,
            )

            if response.status_code == 404:
                msg = "На ноде нет роутера firewall_profile — обновите образ ноды (docker compose pull && up -d)"
                return await _log_failure(db, server, profile_id, msg, rules_hash)

            if response.status_code != 200:
                error_detail = f"HTTP {response.status_code}"
                try:
                    error_detail = response.json().get("detail", error_detail)
                except Exception:
                    pass
                return await _log_failure(db, server, profile_id, error_detail, rules_hash)

            data = response.json() if response.content else {}
            ok = bool(data.get("success", False))
            msg = data.get("message", "Profile applied")
            rolled_back = bool(data.get("rolled_back", False))
            node_hash = data.get("rules_hash") or rules_hash

            if ok:
                now = datetime.now(timezone.utc)
                await db.execute(
                    update(Server)
                    .where(Server.id == server.id)
                    .values(
                        firewall_rules_hash=node_hash,
                        firewall_last_sync_at=now,
                        firewall_sync_status="synced",
                    )
                )
                db.add(FirewallSyncLog(
                    server_id=server.id,
                    profile_id=profile_id,
                    status="success",
                    message=msg,
                    rules_hash=node_hash,
                ))
                await db.commit()
                return SyncResult(server.id, server.name, True, msg, rolled_back=False)

            status = "rolled_back" if rolled_back else "failed"
            await db.execute(
                update(Server).where(Server.id == server.id).values(firewall_sync_status=status)
            )
            db.add(FirewallSyncLog(
                server_id=server.id,
                profile_id=profile_id,
                status=status,
                message=msg,
                rules_hash=rules_hash,
            ))
            await db.commit()
            return SyncResult(server.id, server.name, False, msg, rolled_back=rolled_back)

        except httpx.TimeoutException:
            return await _log_failure(
                db, server, profile_id, "Connection timeout", rules_hash, retryable=queue_failures
            )
        except httpx.RequestError as e:
            return await _log_failure(
                db, server, profile_id, f"Connection error: {e}", rules_hash,
                retryable=queue_failures,
            )
        except Exception as e:
            logger.exception("Unexpected error syncing firewall profile to server %s", server.name)
            return await _log_failure(db, server, profile_id, str(e), rules_hash)


async def sync_profile_to_servers(
    profile: FirewallProfile,
    db: AsyncSession,
    server_ids: list[int] | None = None,
    force: bool = False,
    queue_failures: bool = True,
) -> list[SyncResult]:
    """Раскатать профиль на все привязанные серверы (или подмножество).

    `queue_failures=False` — вызов из самой очереди отложенных синков: она разбирается
    с повторами сама, а собственная постановка долга обнулила бы её выдержку и
    превратила бы повторы в непрерывное долбление ноды.
    """
    try:
        rules = json.loads(profile.rules_json) if profile.rules_json else []
    except (json.JSONDecodeError, TypeError):
        rules = []

    rules_hash = compute_rules_hash(profile.rules_json, profile.default_incoming, profile.default_outgoing)

    query = select(Server).where(
        Server.active_firewall_profile_id == profile.id,
        Server.is_active.is_(True),
    )
    if server_ids:
        query = query.where(Server.id.in_(server_ids))

    result = await db.execute(query)
    servers = list(result.scalars().all())
    if not servers:
        return []

    ids = [s.id for s in servers]
    await db.execute(
        update(Server).where(Server.id.in_(ids)).values(firewall_sync_status="pending")
    )
    await db.commit()

    online = [s for s in servers if is_server_online(s)]
    offline = [s for s in servers if not is_server_online(s)]

    # Статус pending уже проставлен выше всем — офлайн-ноды так и остаются в нём
    # до момента, когда очередь их догонит.
    results: list[SyncResult] = await _queue_offline_servers(offline, profile.id, rules_hash)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SYNCS)

    async def _guarded(server: Server) -> SyncResult:
        async with semaphore:
            return await _sync_single_server(
                server, rules, profile.default_incoming, profile.default_outgoing,
                rules_hash, profile.id, force, queue_failures,
            )

    results.extend(await asyncio.gather(*[_guarded(s) for s in online]))
    return results


async def sync_firewall_to_servers(server_ids: list[int]) -> dict[int, Optional[str]]:
    """Досинхронизировать firewall-профиль перечисленным нодам (исполнитель очереди).

    Ноды группируются по своему активному профилю: у каждой он свой, а раскатка
    работает от профиля, а не от сервера.
    """
    async with async_session() as db:
        servers = list((await db.execute(
            select(Server).where(
                Server.id.in_(server_ids),
                Server.is_active.is_(True),
                Server.active_firewall_profile_id.isnot(None),
            )
        )).scalars().all())

    # Сервер удалён, выключен или отвязан от профиля — применять нечего, долг закрыт.
    found = {s.id for s in servers}
    results: dict[int, Optional[str]] = {sid: None for sid in server_ids if sid not in found}

    by_profile: dict[int, list[int]] = defaultdict(list)
    for server in servers:
        by_profile[server.active_firewall_profile_id].append(server.id)

    # Своя сессия на профиль: раскатка внутри ждёт ответа нод, и одна общая держала бы
    # коннект пула всё это время.
    for profile_id, sids in by_profile.items():
        async with async_session() as db:
            profile = await db.get(FirewallProfile, profile_id)
            if not profile:
                results.update({sid: None for sid in sids})
                continue
            synced = await sync_profile_to_servers(
                profile, db, server_ids=sids, queue_failures=False
            )
            for sync in synced:
                results[sync.server_id] = None if sync.success else sync.message

    return results
