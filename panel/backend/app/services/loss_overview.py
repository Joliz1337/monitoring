"""Страница «Потери»: сводка по адресам назначения со всех релеев и ручная проверка.

Сводка строится из реестра (loss_registry) — без запросов к нодам. Ручная
проверка просит выбранные ноды прямо сейчас сделать серию попыток до адреса
(`POST /api/loss-probe/check` на ноде).
"""

import asyncio
import ipaddress
import logging
from enum import Enum
from typing import Optional

import httpx

from app.models import Server
from app.services.http_client import get_node_client, node_auth_headers
from app.services.loss_alerts import Episode
from app.services.loss_registry import RelaySnapshot
from app.services.node_capabilities import learn_from_denial, server_allows_path

logger = logging.getLogger(__name__)

CHECK_PATH = "/api/loss-probe/check"
CHECK_CONCURRENCY = 20
# Серия на ноде — пара секунд; запас на медленный канал до ноды
CHECK_TIMEOUT_SEC = 15.0
DEFAULT_PORT = 443


class CheckStatus(str, Enum):
    OK = "ok"
    UNSUPPORTED = "unsupported"
    DENIED = "denied"
    UNREACHABLE = "unreachable"


class TargetParseError(ValueError):
    pass


def build_overview(snapshots: list[RelaySnapshot], owners: dict[str, str],
                   episodes: dict[str, Episode], hidden_ips: frozenset[str] = frozenset()) -> list[dict]:
    """Адрес → что видит каждый релей; худшие потери сверху. Адреса
    исключённых серверов (hidden_ips) не показываются."""
    targets: dict[str, dict] = {}
    for snapshot in snapshots:
        for reading in snapshot.readings:
            if reading.ip in hidden_ips:
                continue
            entry = targets.setdefault(reading.key, {
                "target": reading.key,
                "ip": reading.ip,
                "port": reading.port,
                "owner": owners.get(reading.ip),
                "relays": [],
            })
            entry["relays"].append({
                "server_id": snapshot.server_id,
                "name": snapshot.name,
                "loss_pct": reading.loss_pct,
                "rtt_ms": reading.rtt_ms,
                "samples": reading.samples,
            })
    for key, entry in targets.items():
        entry["relays"].sort(key=lambda relay: (-relay["loss_pct"], relay["name"]))
        entry["worst_loss"] = entry["relays"][0]["loss_pct"]
        episode = episodes.get(key)
        entry["episode"] = {"level": episode.level, "opened_at": episode.opened_at} if episode else None
    return sorted(targets.values(), key=lambda entry: (-entry["worst_loss"], entry["target"]))


def parse_target(text: str) -> tuple[str, int]:
    """`1.2.3.4`, `1.2.3.4:8443`, `[2001:db8::1]:443` или голый IPv6 → (ip, порт)."""
    raw = (text or "").strip()
    host, port_text = raw, ""
    if raw.startswith("["):
        host, _, rest = raw[1:].partition("]")
        port_text = rest.removeprefix(":")
    elif raw.count(":") == 1:
        host, _, port_text = raw.partition(":")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise TargetParseError(f"not an IP address: {host!r}")
    if ip.is_unspecified or ip.is_multicast:
        raise TargetParseError("address must be unicast")
    if not port_text:
        return str(ip), DEFAULT_PORT
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise TargetParseError(f"bad port: {port_text!r}")
    return str(ip), int(port_text)


async def check_from_servers(servers: list[Server], ip: str, port: int) -> list[dict]:
    semaphore = asyncio.Semaphore(CHECK_CONCURRENCY)

    async def guarded(server: Server) -> dict:
        async with semaphore:
            return await _check_one(server, ip, port)

    results = await asyncio.gather(*(guarded(server) for server in servers))
    return sorted(results, key=lambda r: (r["status"] != CheckStatus.OK, -(r.get("loss_pct") or 0), r["name"]))


async def _check_one(server: Server, ip: str, port: int) -> dict:
    result = {"server_id": server.id, "name": server.name}
    allowed, _, _ = server_allows_path(server, CHECK_PATH, "POST")
    if not allowed:
        return {**result, "status": CheckStatus.DENIED}
    try:
        response = await get_node_client(server).post(
            f"{server.url}{CHECK_PATH}",
            headers=node_auth_headers(server),
            json={"ip": ip, "port": port},
            timeout=CHECK_TIMEOUT_SEC,
        )
    except httpx.HTTPError as exc:
        logger.info("loss_check_unreachable server_id=%s error=%s", server.id, exc)
        return {**result, "status": CheckStatus.UNREACHABLE}
    if response.status_code == 404:
        return {**result, "status": CheckStatus.UNSUPPORTED}
    if response.status_code == 403:
        await learn_from_denial(server.id, response.status_code, _json_or_none(response))
        return {**result, "status": CheckStatus.DENIED}
    body = _json_or_none(response)
    if response.status_code != 200 or not isinstance(body, dict):
        return {**result, "status": CheckStatus.UNREACHABLE}
    return {
        **result,
        "status": CheckStatus.OK,
        "loss_pct": body.get("loss_pct"),
        "rtt_ms": body.get("rtt_ms"),
        "samples": body.get("samples"),
    }


def _json_or_none(response: httpx.Response) -> Optional[object]:
    try:
        return response.json()
    except ValueError:
        return None
