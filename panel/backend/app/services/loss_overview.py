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
from app.services.loss_registry import NO_REPLY_LOSS_PCT, TOTAL_LOSS_PCT, RelaySnapshot, TargetMode, TargetRules
from app.services.node_capabilities import learn_from_denial, server_allows_path

logger = logging.getLogger(__name__)

CHECK_PATH = "/api/loss-probe/check"
WAYS_PATH = "/api/loss-probe/ways"
ALL_WAYS_PATH = "/api/loss-probe/ways/all"
# Подсказка открывается по наведению: дольше ждать ноду незачем
WAYS_TIMEOUT_SEC = 5.0
WAY_FIELDS = ("local_ip", "used", "loss_pct", "rtt_ms", "samples", "answers", "span_sec")
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
                   episodes: dict[str, Episode], rules: TargetRules = TargetRules()) -> list[dict]:
    """Адрес → что видит каждый релей; худшие потери сверху. Скрытые адреса не
    показываются, адреса «только полные потери» — пока у них нет полных потерь
    и открытого эпизода."""
    targets: dict[str, dict] = {}
    for snapshot in snapshots:
        for reading in snapshot.readings:
            mode = rules.mode(reading)
            if mode is TargetMode.HIDDEN:
                continue
            entry = targets.setdefault(reading.key, {
                "target": reading.key,
                "ip": reading.ip,
                "port": reading.port,
                "owner": owners.get(reading.ip),
                "total_only": mode is TargetMode.TOTAL_ONLY,
                "relays": [],
            })
            entry["relays"].append({
                "server_id": snapshot.server_id,
                "name": snapshot.name,
                "loss_pct": reading.loss_pct,
                "rtt_ms": reading.rtt_ms,
                "samples": reading.samples,
                "answers": reading.answers,
            })
    for key, entry in targets.items():
        entry["relays"].sort(key=lambda relay: (-relay["loss_pct"], relay["name"]))
        entry["worst_loss"] = entry["relays"][0]["loss_pct"]
        episode = episodes.get(key)
        entry["episode"] = {"level": episode.level, "opened_at": episode.opened_at} if episode else None
    visible = [
        entry for entry in targets.values()
        if not entry["total_only"] or entry["episode"] or entry["worst_loss"] >= TOTAL_LOSS_PCT
    ]
    return sorted(visible, key=lambda entry: (-entry["worst_loss"], entry["target"]))


def parse_target(text: str) -> tuple[str, int]:
    """`1.2.3.4`, `1.2.3.4:8443`, `[2001:db8::1]:443` или голый IPv6 → (ip, порт)."""
    ip, port = parse_endpoint(text)
    return ip, DEFAULT_PORT if port is None else port


def parse_endpoint(text: str) -> tuple[str, Optional[int]]:
    """То же, что parse_target, но без порта по умолчанию: (ip, None)."""
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
        return str(ip), None
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise TargetParseError(f"bad port: {port_text!r}")
    return str(ip), int(port_text)


def format_endpoint(ip: str, port: Optional[int]) -> str:
    """Обратное parse_endpoint: IPv6 с портом — в квадратных скобках."""
    if port is None:
        return ip
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


async def check_from_servers(servers: list[Server], ip: str, port: int, client_probe: bool = False) -> list[dict]:
    semaphore = asyncio.Semaphore(CHECK_CONCURRENCY)

    async def guarded(server: Server) -> dict:
        async with semaphore:
            return await _check_one(server, ip, port, client_probe)

    results = await asyncio.gather(*(guarded(server) for server in servers))
    return sorted(results, key=lambda r: (r["status"] != CheckStatus.OK, -(r.get("loss_pct") or 0), r["name"]))


async def _check_one(server: Server, ip: str, port: int, client_probe: bool) -> dict:
    result = {"server_id": server.id, "name": server.name}
    allowed, _, _ = server_allows_path(server, CHECK_PATH, "POST")
    if not allowed:
        return {**result, "status": CheckStatus.DENIED}
    try:
        response = await get_node_client(server).post(
            f"{server.url}{CHECK_PATH}",
            headers=node_auth_headers(server),
            json={"ip": ip, "port": port, **({"client_probe": True} if client_probe else {})},
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
    answers = body.get("answers") if isinstance(body.get("answers"), bool) else None
    return {
        **result,
        "status": CheckStatus.OK,
        # Как в реестре замеров: сервер, молчащий на проверку «как клиент», не работает целиком
        "loss_pct": NO_REPLY_LOSS_PCT if answers is False else body.get("loss_pct"),
        "answers": answers,
        "rtt_ms": body.get("rtt_ms"),
        "samples": body.get("samples"),
    }


async def _read_ways(server: Server, path: str, params: Optional[dict]) -> tuple[CheckStatus, Optional[dict]]:
    """Ответ ноды на запрос разбивки по её адресам: статус и тело, если оно пришло словарём.
    Нода читает уже набранные окна и по этому запросу никуда не подключается."""
    allowed, _, _ = server_allows_path(server, path, "GET")
    if not allowed:
        return CheckStatus.DENIED, None
    try:
        response = await get_node_client(server).get(
            f"{server.url}{path}", headers=node_auth_headers(server), params=params, timeout=WAYS_TIMEOUT_SEC,
        )
    except httpx.HTTPError as exc:
        logger.info("loss_ways_unreachable server_id=%s error=%s", server.id, exc)
        return CheckStatus.UNREACHABLE, None
    if response.status_code == 404:
        return CheckStatus.UNSUPPORTED, None
    if response.status_code == 403:
        await learn_from_denial(server.id, response.status_code, _json_or_none(response))
        return CheckStatus.DENIED, None
    body = _json_or_none(response)
    if response.status_code != 200 or not isinstance(body, dict):
        return CheckStatus.UNREACHABLE, None
    return CheckStatus.OK, body


def _way_rows(ways: object) -> Optional[list[dict]]:
    """Строки путей из ответа ноды, только известные поля; None — на месте списка что-то другое."""
    if not isinstance(ways, list):
        return None
    return [{field: way.get(field) for field in WAY_FIELDS} for way in ways if isinstance(way, dict)]


async def ways_from_server(server: Server, ip: str, port: int) -> dict:
    """Потери и ответ сервера с каждого своего адреса релея — как их видит сама нода."""
    status, body = await _read_ways(server, WAYS_PATH, {"ip": ip, "port": port})
    ways = _way_rows(body.get("ways")) if body is not None else None
    if status is CheckStatus.OK and ways is None:
        status = CheckStatus.UNREACHABLE
    return {"status": status, "ways": ways or []}


async def all_ways_from_server(server: Server) -> dict:
    """То же по всем адресам назначения релея одним запросом — странице HAProxy, которая отмечает
    по ним строки бэкендов. Ключ — адрес, как его печатает статистика HAProxy (IPv6 в скобках)."""
    status, body = await _read_ways(server, ALL_WAYS_PATH, None)
    entries = body.get("targets") if body is not None else None
    if status is CheckStatus.OK and not isinstance(entries, list):
        status = CheckStatus.UNREACHABLE
    targets = []
    for entry in entries if isinstance(entries, list) else []:
        ways = _way_rows(entry.get("ways")) if isinstance(entry, dict) else None
        if ways is None or not isinstance(entry.get("ip"), str) or not isinstance(entry.get("port"), int):
            continue
        targets.append({"target": format_endpoint(entry["ip"], entry["port"]), "ways": ways})
    return {"status": status, "targets": targets}


def _json_or_none(response: httpx.Response) -> Optional[object]:
    try:
        return response.json()
    except ValueError:
        return None
