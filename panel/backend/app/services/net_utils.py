"""Общие сетевые хелперы: внешний IP панели, резолв нод и проверка публичности диапазона."""

import asyncio
import ipaddress
import logging
import socket
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Приватные/служебные диапазоны, которые нельзя пускать в block-списки:
# DROP по ним убивает loopback, docker-bridge и внутренние сети хостера
# (инцидент с firehol_level1 — он содержит bogon-диапазоны для бордер-роутеров).
NON_PUBLIC_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
))

# Сервисы «какой у меня IP»: отвечают голым адресом в теле, опрашиваются по очереди
PUBLIC_IP_SERVICES = (
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://ifconfig.me/ip",
    "https://checkip.amazonaws.com",
    "https://ident.me",
)
PUBLIC_IP_SERVICE_TIMEOUT = 3.0
# Адрес, по которому ядро подбирает исходящий интерфейс; UDP connect пакетов не шлёт
ROUTE_PROBE_ADDR = ("1.1.1.1", 53)
PANEL_IP_CACHE_TTL = 600.0
PANEL_IP_RETRY_TTL = 60.0


class PanelIpSource(str, Enum):
    EXTERNAL = "external"
    INTERFACE = "interface"
    DNS = "dns"


@dataclass(frozen=True)
class PanelIp:
    ip: str
    source: PanelIpSource


@dataclass
class _PanelIpCache:
    result: Optional[PanelIp] = None
    expires_at: float = 0.0


_cache = _PanelIpCache()
_cache_lock = asyncio.Lock()


def is_public_range(ip_cidr: str) -> bool:
    """True, если IP/CIDR не пересекается с приватными/служебными диапазонами."""
    try:
        net = ipaddress.ip_network(ip_cidr, strict=False)
    except ValueError:
        return False
    if net.version != 4:
        return True
    return not any(net.overlaps(bad) for bad in NON_PUBLIC_NETS)


def _parse_public_ipv4(raw: str) -> Optional[str]:
    """Публичный IPv4 из ответа сервиса/адреса интерфейса, иначе None."""
    candidate = raw.strip()
    try:
        ipaddress.IPv4Address(candidate)
    except ValueError:
        return None
    return candidate if is_public_range(candidate) else None


async def resolve_host(host: str) -> Optional[str]:
    """DNS-резолв без блокировки цикла.

    socket.gethostbyname синхронный: недоступный резолвер держит его до
    системного таймаута, а вызывают его в цикле по всем нодам — на времени
    сборки whitelist это остановило бы всю панель.
    """
    if not host:
        return None
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    except (socket.gaierror, OSError):
        return None
    return infos[0][4][0] if infos else None


async def fetch_ip_from_services(client: httpx.AsyncClient) -> Optional[str]:
    """Первый публичный IPv4, который вернул один из PUBLIC_IP_SERVICES."""
    for url in PUBLIC_IP_SERVICES:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError:
            continue
        ip = _parse_public_ipv4(response.text)
        if ip:
            return ip
    return None


async def _ip_from_external_services() -> Optional[str]:
    # Привязка к 0.0.0.0 запрещает IPv6-соединения: сервисы с AAAA-записью
    # иначе вернули бы v6-адрес, а whitelist на нодах — IPv4-ipset.
    # trust_env=False — идём напрямую, а не через HTTP_PROXY окружения:
    # нужен адрес, с которого панель реально ходит к нодам.
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(
        transport=transport, timeout=PUBLIC_IP_SERVICE_TIMEOUT, trust_env=False
    ) as client:
        return await fetch_ip_from_services(client)


async def _ip_from_interface() -> Optional[str]:
    """Адрес исходящего интерфейса, если он публичный (host-сеть, VPS с белым IP на карте)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(ROUTE_PROBE_ADDR)
            local_ip = probe.getsockname()[0]
    except OSError:
        return None
    return _parse_public_ipv4(local_ip)


async def _ip_from_domain() -> Optional[str]:
    """Последний резерв: A-запись домена панели (за прокси Cloudflare даёт чужой IP)."""
    domain = get_settings().domain
    if not domain:
        return None
    return await resolve_host(domain)


async def _detect_panel_ip() -> Optional[PanelIp]:
    detectors = (
        (PanelIpSource.EXTERNAL, _ip_from_external_services),
        (PanelIpSource.INTERFACE, _ip_from_interface),
        (PanelIpSource.DNS, _ip_from_domain),
    )
    for source, detector in detectors:
        ip = await detector()
        if ip:
            return PanelIp(ip=ip, source=source)
    return None


def _remember(result: Optional[PanelIp]) -> None:
    if result is None:
        logger.warning("Panel IP not detected: external services, interface and domain all failed")
    elif _cache.result is None or _cache.result.ip != result.ip:
        logger.info(f"Panel IP detected: {result.ip} (source={result.source.value})")
    ttl = PANEL_IP_CACHE_TTL if result else PANEL_IP_RETRY_TTL
    _cache.result = result
    _cache.expires_at = time.monotonic() + ttl


async def panel_ip_info() -> Optional[PanelIp]:
    """Внешний IP панели и способ, которым он получен.

    Порядок: ответ внешнего сервиса (это ровно тот адрес, который видят ноды),
    затем публичный адрес исходящего интерфейса, затем A-запись домена.
    Результат кэшируется: успех — PANEL_IP_CACHE_TTL, промах — PANEL_IP_RETRY_TTL.
    """
    if time.monotonic() < _cache.expires_at:
        return _cache.result
    async with _cache_lock:
        if time.monotonic() < _cache.expires_at:
            return _cache.result
        result = await _detect_panel_ip()
        _remember(result)
    return result


async def resolve_panel_ip() -> Optional[str]:
    """Внешний IP панели (None, если не удалось определить ни одним способом)."""
    found = await panel_ip_info()
    return found.ip if found else None


async def host_to_ip(host: str) -> Optional[str]:
    """IP из host (уже IP — вернуть как есть, иначе DNS-резолв)."""
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    return await resolve_host(host)
