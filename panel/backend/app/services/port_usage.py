"""Какие порты на сервере держат HAProxy и DNAT.

Правило DNAT стоит первым в nat/PREROUTING и ловит TCP-порт на любом адресе
сервера, поэтому HAProxy на том же порту молча перестаёт получать соединения:
запущен, конфиг валиден, а трафика ноль. Пересечения считает интерфейс — ему
нужно сравнивать порты ещё не сохранённого правила; здесь только кто что занял."""

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DnatProfile, HAProxyConfigProfile, Server, ServerCache
from app.services.dnat_profile_sync import load_rules
from app.services.haproxy_config import get_config_generator

PortRange = tuple[int, int]


@dataclass
class ServerPortUsage:
    haproxy_ports: list[int] = field(default_factory=list)
    dnat_ranges: list[PortRange] = field(default_factory=list)
    dnat_profile_id: int | None = None


def dnat_tcp_ranges(rules: list[dict]) -> list[PortRange]:
    """Диапазоны включённых DNAT-правил, ловящих TCP: HAProxy слушает только его."""
    ranges: list[PortRange] = []
    for rule in rules:
        if not rule.get("enabled", True) or (rule.get("protocol") or "tcp") == "udp":
            continue
        low = int(rule.get("listen_port") or 0)
        if low > 0:
            ranges.append((low, int(rule.get("listen_port_end") or low)))
    return sorted(ranges)


def running_haproxy_ports(cache_json: str | None) -> list[int]:
    """Порты правил HAProxy из последнего опроса ноды — только если HAProxy запущен:
    конфиг остановленного HAProxy, который заменили на DNAT, конфликтом не считается."""
    if not cache_json:
        return []
    try:
        data = json.loads(cache_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict) or not (data.get("status") or {}).get("running"):
        return []
    rules = (data.get("rules") or {}).get("rules") or []
    return [int(r["listen_port"]) for r in rules if isinstance(r, dict) and int(r.get("listen_port") or 0) > 0]


def profile_haproxy_ports(config_content: str | None) -> list[int]:
    rules = get_config_generator().parse_rules_from_config(config_content or "")
    return [r.listen_port for r in rules if r.listen_port > 0]


async def load_port_usage(db: AsyncSession, server_id: int | None = None) -> dict[int, ServerPortUsage]:
    """Порты HAProxy и диапазоны DNAT по серверам; серверов без того и другого нет.

    Порт HAProxy занят, если правило стоит на ноде при запущенном HAProxy или есть
    в профиле, к которому привязан сервер, — HAProxy профиля займёт порт, как только
    его запустят. DNAT берётся из профиля: правила на ноду ставит только панель."""
    servers_query = select(Server.id, Server.active_haproxy_profile_id, Server.active_dnat_profile_id)
    cache_query = select(ServerCache.server_id, ServerCache.last_haproxy_data).where(
        ServerCache.last_haproxy_data.is_not(None)
    )
    if server_id is not None:
        servers_query = servers_query.where(Server.id == server_id)
        cache_query = cache_query.where(ServerCache.server_id == server_id)

    servers = (await db.execute(servers_query)).all()
    if not servers:
        return {}

    haproxy_profile_ids = {row[1] for row in servers if row[1]}
    dnat_profile_ids = {row[2] for row in servers if row[2]}

    profile_ports: dict[int, list[int]] = {}
    if haproxy_profile_ids:
        rows = await db.execute(
            select(HAProxyConfigProfile.id, HAProxyConfigProfile.config_content)
            .where(HAProxyConfigProfile.id.in_(haproxy_profile_ids))
        )
        profile_ports = {pid: profile_haproxy_ports(content) for pid, content in rows.all()}

    profile_ranges: dict[int, list[PortRange]] = {}
    if dnat_profile_ids:
        profiles = (await db.execute(select(DnatProfile).where(DnatProfile.id.in_(dnat_profile_ids)))).scalars().all()
        profile_ranges = {p.id: dnat_tcp_ranges(load_rules(p)) for p in profiles}

    node_ports = {sid: running_haproxy_ports(data) for sid, data in (await db.execute(cache_query)).all()}

    usage: dict[int, ServerPortUsage] = {}
    for sid, haproxy_profile_id, dnat_profile_id in servers:
        haproxy_ports = sorted(set(node_ports.get(sid, [])) | set(profile_ports.get(haproxy_profile_id, [])))
        dnat_ranges = profile_ranges.get(dnat_profile_id, [])
        if haproxy_ports or dnat_ranges:
            usage[sid] = ServerPortUsage(haproxy_ports, dnat_ranges, dnat_profile_id)
    return usage
