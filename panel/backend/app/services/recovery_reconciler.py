"""Авто-восстановление состояния ноды после наблюдаемого перехода offline → online.

Когда сервер надолго пропадал и снова ожил, его привязанное состояние могло разойтись с
желаемым: HAProxy не поднялся после reboot или потерял конфиг, UFW сбросился, временные
ipset-блокировки исчезли. Здесь мы сверяем фактическое состояние ноды с ожидаемым по
контрольной сумме и тихо переприменяем только то, что реально сбилось — чтобы не сбросить
рабочий firewall зря.
"""

import asyncio
import json
import logging
from dataclasses import dataclass

from app.database import async_session
from app.models import FirewallProfile, HAProxyConfigProfile, Server, ServerCache
from app.services.blocklist_manager import get_blocklist_manager
from app.services.firewall_profile_sync import (
    compute_rules_hash,
    sync_profile_to_servers as sync_firewall_profile,
)
from app.services.haproxy_profile_sync import (
    compute_config_hash,
    sync_profile_to_servers as sync_haproxy_profile,
)
from app.services.http_client import get_node_client, node_auth_headers

logger = logging.getLogger(__name__)

NODE_FETCH_TIMEOUT = 15.0
HAPROXY_START_TIMEOUT = 30.0


@dataclass
class RecoveryReport:
    """Итог восстановления по каждой подсистеме (для одной строки лога)."""
    server_id: int
    firewall: str = "skipped"
    haproxy_cfg: str = "skipped"
    haproxy_run: str = "skipped"
    blocklist: str = "skipped"


def _normalize_config(text: str) -> str:
    """Канонизирует текст конфига перед хэшем, чтобы CRLF/трейлинг-перевод не давали ложный drift."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _cached_haproxy_running(cache_row: ServerCache | None) -> bool | None:
    """Возвращает running-статус HAProxy из кэша (значение ДО падения) или None если неизвестно."""
    if not cache_row or not cache_row.last_haproxy_data:
        return None
    try:
        data = json.loads(cache_row.last_haproxy_data)
    except (json.JSONDecodeError, TypeError):
        return None
    status = data.get("status")
    if not isinstance(status, dict):
        return None
    running = status.get("running")
    return None if running is None else bool(running)


async def _node_get(server: Server, path: str) -> dict | None:
    """GET к ноде; None при любой ошибке или не-200 (нода могла снова отвалиться посреди reconcile)."""
    try:
        client = get_node_client(server)
        resp = await client.get(
            f"{server.url}{path}",
            headers=node_auth_headers(server),
            timeout=NODE_FETCH_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        return None
    return None


async def _post_haproxy_start(server: Server) -> bool:
    try:
        client = get_node_client(server)
        resp = await client.post(
            f"{server.url}/api/haproxy/start",
            headers=node_auth_headers(server),
            timeout=HAPROXY_START_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            return bool(data.get("success", True)) if isinstance(data, dict) else True
    except Exception:
        return False
    return False


async def _reconcile_firewall(server_id: int) -> str:
    async with async_session() as db:
        server = await db.get(Server, server_id)
        if not server or not server.active_firewall_profile_id:
            return "no_profile"
        profile = await db.get(FirewallProfile, server.active_firewall_profile_id)
        if not profile:
            return "no_profile"

        expected = compute_rules_hash(
            profile.rules_json, profile.default_incoming, profile.default_outgoing
        )
        state = await _node_get(server, "/api/firewall/profile/state")
        if state is None:
            return "node_unreachable"

        # ufw reset→apply→enable рискован — переприменяем строго при подтверждённом расхождении.
        drift = state.get("rules_hash") != expected or not state.get("active", False)
        if not drift:
            return "in_sync"

        results = await sync_firewall_profile(profile, db, server_ids=[server_id], force=False)
        ok = bool(results) and results[0].success
        return "reapplied" if ok else "reapply_failed"


async def _reconcile_haproxy_config(server_id: int) -> str:
    async with async_session() as db:
        server = await db.get(Server, server_id)
        if not server or not server.active_haproxy_profile_id:
            return "no_profile"
        profile = await db.get(HAProxyConfigProfile, server.active_haproxy_profile_id)
        if not profile:
            return "no_profile"

        expected = compute_config_hash(_normalize_config(profile.config_content))
        data = await _node_get(server, "/api/haproxy/config")
        if data is None or data.get("content") is None:
            return "node_unreachable"

        if compute_config_hash(_normalize_config(data["content"])) == expected:
            return "in_sync"

        results = await sync_haproxy_profile(profile, db, server_ids=[server_id])
        ok = bool(results) and results[0].success
        return "reapplied" if ok else "reapply_failed"


async def _reconcile_haproxy_running(server_id: int, pre_death_running: bool | None) -> str:
    if not pre_death_running:
        return "was_not_running"  # выключенное до падения не запускаем

    async with async_session() as db:
        server = await db.get(Server, server_id)
        if not server:
            return "skipped"

    status = await _node_get(server, "/api/haproxy/status")
    if status is None:
        return "node_unreachable"
    if status.get("running"):
        return "already_running"  # после reboot обычно поднялся сам (systemctl enable)
    if not status.get("installed"):
        return "not_installed"
    if not status.get("config_valid"):
        return "config_invalid"  # битый конфиг не стартуем (шаг конфига уже отработал)

    return "started" if await _post_haproxy_start(server) else "start_failed"


async def _reconcile_blocklist(server_id: int) -> str:
    result = await get_blocklist_manager().sync_single_node_by_id(server_id)
    return "synced" if result and result.get("success") else "sync_failed"


async def reconcile_recovered_server(server_id: int, semaphore: asyncio.Semaphore) -> RecoveryReport:
    """Сверяет и восстанавливает состояние ноды, которая только что перешла offline → online."""
    report = RecoveryReport(server_id=server_id)
    server_name = str(server_id)

    async with semaphore:
        async with async_session() as db:
            server = await db.get(Server, server_id)
            if not server or not server.is_active:
                return report
            server_name = server.name
            # pre-death running читаем первым делом, до обращений к ноде — пока кэш-лупы
            # не перезаписали ServerCache свежим (уже online) значением.
            pre_death_running = _cached_haproxy_running(await db.get(ServerCache, server_id))

        report.firewall = await _reconcile_firewall(server_id)
        report.haproxy_cfg = await _reconcile_haproxy_config(server_id)
        report.haproxy_run = await _reconcile_haproxy_running(server_id, pre_death_running)
        report.blocklist = await _reconcile_blocklist(server_id)

    logger.info(
        "recovery_reconcile_done server=%s(%s) firewall=%s haproxy_cfg=%s haproxy_run=%s blocklist=%s",
        server_id, server_name, report.firewall, report.haproxy_cfg, report.haproxy_run, report.blocklist,
    )
    return report
