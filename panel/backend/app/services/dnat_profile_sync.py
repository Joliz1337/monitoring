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
from sqlalchemy import func, select, update
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
        "target_ip": ",".join(p.strip() for p in str(rule.get("target_ip", "")).split(",") if p.strip()),
        "distribution": (rule.get("distribution") or "per_server").lower(),
        "target_port": int(rule.get("target_port") or 0),
        "masquerade": bool(rule.get("masquerade", True)),
        "mask_ttl": bool(rule.get("mask_ttl", False)),
        "enabled": bool(rule.get("enabled", True)),
    }


def compute_rules_hash(rules: list[dict]) -> str:
    """Хэш набора, как его видит нода — то есть уже отрендеренного под конкретный сервер."""
    canonical = sorted((normalize_rule(r) for r in rules), key=lambda r: r["name"])
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def load_rules(profile: DnatProfile) -> list[dict]:
    try:
        rules = json.loads(profile.rules_json) if profile.rules_json else []
        return rules if isinstance(rules, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def split_targets(target_ip: str) -> list[str]:
    """«10.0.0.2, 10.0.0.3» → ['10.0.0.2', '10.0.0.3']; один адрес — список из одного."""
    return [part.strip() for part in (target_ip or "").split(",") if part.strip()]


def _per_server(rule: dict) -> bool:
    return (rule.get("distribution") or "per_server") == "per_server"


def render_rules_for_server(rules: list[dict], server_index: int) -> list[dict]:
    """Правила профиля → правила для одной ноды. В режиме per_server из списка IP
    берётся адрес по порядку привязки сервера, по кругу — нода получает один IP.
    В остальных режимах список уходит на ноду целиком, распределяет она сама."""
    rendered: list[dict] = []
    for rule in rules:
        targets = split_targets(rule.get("target_ip", ""))
        item = dict(rule)
        if targets and _per_server(rule):
            item["target_ip"] = targets[server_index % len(targets)]
        rendered.append(item)
    return rendered


def assigned_targets(rules: list[dict], server_index: int) -> dict[str, str]:
    """Что достанется серверу по per_server-правилам с несколькими IP — для отображения в панели."""
    return {
        rule["name"]: targets[server_index % len(targets)]
        for rule in rules
        if _per_server(rule) and len(targets := split_targets(rule.get("target_ip", ""))) > 1
    }


async def link_server_to_profile(server: Server, profile_id: int, db: AsyncSession) -> None:
    """Привязать сервер к профилю (без commit). Новый сервер встаёт в конец очереди
    привязки: у уже привязанных IP назначения не меняются, их правила не переприменяются."""
    last_position = (await db.execute(
        select(func.max(Server.dnat_link_position)).where(Server.active_dnat_profile_id == profile_id)
    )).scalar() or 0
    server.active_dnat_profile_id = profile_id
    server.dnat_sync_status = "pending"
    server.dnat_link_position = last_position + 1


async def ordered_linked_servers(profile_id: int, db: AsyncSession) -> list[Server]:
    """Привязанные серверы в порядке привязки (`dnat_link_position`, затем id).
    Серверам, привязанным до появления позиции, она дописывается один раз."""
    servers = list((await db.execute(
        select(Server)
        .where(Server.active_dnat_profile_id == profile_id)
        .order_by(Server.dnat_link_position.asc().nulls_last(), Server.id)
    )).scalars().all())
    unnumbered = [s for s in servers if s.dnat_link_position is None]
    if unnumbered:
        next_position = max((s.dnat_link_position for s in servers if s.dnat_link_position is not None), default=0) + 1
        for server in unnumbered:
            server.dnat_link_position = next_position
            next_position += 1
        await db.commit()
    return servers


def server_index(servers: list[Server], server_id: int) -> int:
    for index, server in enumerate(servers):
        if server.id == server_id:
            return index
    return 0


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
    servers: list[Server], profile_id: int, hashes: dict[int, str],
) -> list[SyncResult]:
    if not servers:
        return []
    async with async_session_maker() as db:
        db.add_all([
            DnatSyncLog(
                server_id=server.id, profile_id=profile_id, status="skipped",
                message=QUEUED_MESSAGE, rules_hash=hashes[server.id],
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
    # Порядок привязки считается по всем привязанным серверам, а не по подмножеству —
    # иначе синк одного сервера выдал бы ему чужой IP из списка назначения
    linked = await ordered_linked_servers(profile.id, db)
    rendered = {s.id: render_rules_for_server(rules, index) for index, s in enumerate(linked)}
    hashes = {sid: compute_rules_hash(items) for sid, items in rendered.items()}

    servers = [s for s in linked if s.is_active and (not server_ids or s.id in server_ids)]
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
    results.extend(await _queue_offline_servers(offline, profile.id, hashes))

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SYNCS)

    async def _guarded(server: Server) -> SyncResult:
        async with semaphore:
            return await _sync_single_server(
                server, rendered[server.id], hashes[server.id], profile.id, queue_failures,
            )

    results.extend(await asyncio.gather(*[_guarded(s) for s in online]))
    return results


CLEAR_QUEUED_MESSAGE = "Сервер офлайн — снятие правил отложено до восстановления"


async def _clear_single_server(server: Server, queue_failures: bool, profile_id: Optional[int]) -> Optional[str]:
    """Снять DNAT-правила с ноды, у которой больше нет профиля. None — успех или
    повторять бессмысленно; строка — ошибка, с которой долг остаётся в очереди."""
    url = f"{server.url.rstrip('/')}/api/dnat/clear"
    async with async_session_maker() as db:
        try:
            client = get_node_apply_client(server)
            response = await client.post(
                url, headers=node_auth_headers(server), timeout=APPLY_TIMEOUT_SECONDS,
            )
            # Старый агент правил не имеет — снимать нечего
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                message = f"HTTP {response.status_code}"
                db.add(DnatSyncLog(server_id=server.id, profile_id=profile_id, status="failed", message=f"clear: {message}"))
                await db.commit()
                return message
            data = response.json() if response.content else {}
            if not data.get("success", False):
                message = data.get("message", "clear failed")
                db.add(DnatSyncLog(server_id=server.id, profile_id=profile_id, status="failed", message=f"clear: {message}"))
                await db.commit()
                return message
            await db.execute(
                update(Server).where(Server.id == server.id).values(dnat_rules_hash=None, dnat_last_sync_at=None)
            )
            db.add(DnatSyncLog(server_id=server.id, profile_id=profile_id, status="cleared", message="DNAT rules removed from node"))
            await db.commit()
            return None
        except (httpx.TimeoutException, httpx.RequestError) as e:
            message = f"Connection error: {e}"
            db.add(DnatSyncLog(server_id=server.id, profile_id=profile_id, status="failed", message=f"clear: {message}"))
            await db.commit()
            if queue_failures:
                await enqueue([server.id], KIND_DNAT_PROFILE, message)
            return message


async def clear_dnat_on_servers(
    server_ids: list[int], queue_failures: bool = True, profile_id: Optional[int] = None,
) -> dict[int, Optional[str]]:
    """Снять правила с нод, отвязанных от профиля: онлайн — сразу, офлайн — через
    очередь отложенной синхронизации (её исполнитель видит, что профиля больше
    нет, и делает то же самое). Иначе отвязанная нода пробрасывала бы трафик
    дальше и после перезагрузки — свои правила она хранит и восстанавливает сама.
    `profile_id` — бывший профиль, только чтобы запись попала в его историю.
    """
    async with async_session() as db:
        servers = list((await db.execute(
            select(Server).where(Server.id.in_(server_ids), Server.is_active.is_(True))
        )).scalars().all())

    results: dict[int, Optional[str]] = {sid: None for sid in server_ids}
    todo = [s for s in servers if server_allows(s, Capability.DNAT, write=True)]
    offline = [s for s in todo if not is_server_online(s)]
    online = [s for s in todo if is_server_online(s)]

    if offline and queue_failures:
        async with async_session_maker() as db:
            db.add_all([
                DnatSyncLog(server_id=s.id, profile_id=profile_id, status="skipped", message=CLEAR_QUEUED_MESSAGE)
                for s in offline
            ])
            await db.commit()
        await enqueue([s.id for s in offline], KIND_DNAT_PROFILE, CLEAR_QUEUED_MESSAGE)
    for s in offline:
        results[s.id] = CLEAR_QUEUED_MESSAGE

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SYNCS)

    async def _guarded(server: Server) -> tuple[int, Optional[str]]:
        async with semaphore:
            return server.id, await _clear_single_server(server, queue_failures, profile_id)

    for server_id, error in await asyncio.gather(*[_guarded(s) for s in online]):
        results[server_id] = error
    return results


async def sync_dnat_to_servers(server_ids: list[int]) -> dict[int, Optional[str]]:
    """Исполнитель очереди: нодам с профилем — досинхронизировать его, нодам без
    профиля (отвязаны, пока лежали) — снять правила."""
    async with async_session() as db:
        servers = list((await db.execute(
            select(Server).where(Server.id.in_(server_ids), Server.is_active.is_(True))
        )).scalars().all())

    found = {s.id for s in servers}
    results: dict[int, Optional[str]] = {sid: None for sid in server_ids if sid not in found}

    orphans = [s.id for s in servers if s.active_dnat_profile_id is None]
    if orphans:
        results.update(await clear_dnat_on_servers(orphans, queue_failures=False))

    by_profile: dict[int, list[int]] = defaultdict(list)
    for server in servers:
        if server.active_dnat_profile_id is not None:
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
