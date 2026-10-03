"""Карта «адрес → сервер панели»: подписи целей правил HAProxy именами серверов.

Источник — то, что панель уже хранит: хост из URL сервера и IPv4 интерфейсов из
последних метрик ноды. С интерфейсов берутся только публичные адреса: docker0
172.17.0.1 или общий 10.0.0.1 есть на многих нодах, и цель подписывалась бы
случайным сервером. Хост из URL берётся любой, включая домен, — его оператор
задал сам, и цель правила бывает доменом.

Основной адрес сервера — хост из URL (по нему панель подключается к ноде), а
при домене в URL — первый публичный IPv4 интерфейсов. Остальные публичные IPv4
нумеруются по порядку на интерфейсах: доп. 1, доп. 2…
"""

import ipaddress
import json
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Server


@dataclass(frozen=True)
class ServerAddressSource:
    id: int
    name: str
    url: str
    last_metrics: Optional[str]


@dataclass(frozen=True)
class IpOwner:
    id: int
    name: str
    # None — основной адрес сервера, N — его N-й дополнительный адрес
    extra_number: Optional[int] = None


async def load_ip_owners(db: AsyncSession) -> dict[str, IpOwner]:
    result = await db.execute(
        select(Server.id, Server.name, Server.url, Server.last_metrics)
        .order_by(Server.position, Server.id)
    )
    return build_ip_owners([ServerAddressSource(*row) for row in result.fetchall()])


def build_ip_owners(servers: list[ServerAddressSource]) -> dict[str, IpOwner]:
    """Адрес из URL важнее адреса с интерфейса: его оператор задал явно, а
    интерфейсы в метриках могут отставать (переехавший плавающий IP).
    Между равными источниками адрес достаётся первому серверу в списке."""
    owners: dict[str, IpOwner] = {}
    hosts = [_url_host(server.url) for server in servers]
    for server, host in zip(servers, hosts):
        if host:
            owners.setdefault(host, IpOwner(server.id, server.name))
    for server, host in zip(servers, hosts):
        interface_ips = _public_interface_ipv4(server.last_metrics)
        for address, extra_number in _extra_numbers(interface_ips, host).items():
            owners.setdefault(address, IpOwner(server.id, server.name, extra_number))
    return owners


def _extra_numbers(interface_ips: list[str], url_host: Optional[str]) -> dict[str, Optional[int]]:
    """Нумерация считается по адресам самого сервера, а не по тому, какие из них
    достались ему в общей карте: номер не зависит от конфликтов с соседями."""
    primary = url_host if _is_ip(url_host) else next(iter(interface_ips), None)
    extras = [address for address in interface_ips if address != primary]
    numbers: dict[str, Optional[int]] = {address: number for number, address in enumerate(extras, start=1)}
    if primary in interface_ips:
        numbers[primary] = None
    return numbers


def _url_host(url: str) -> Optional[str]:
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def _is_ip(host: Optional[str]) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _public_interface_ipv4(last_metrics: Optional[str]) -> list[str]:
    if not last_metrics:
        return []
    try:
        metrics = json.loads(last_metrics)
    except json.JSONDecodeError:
        return []
    network = metrics.get("network") if isinstance(metrics, dict) else None
    interfaces = network.get("interfaces") if isinstance(network, dict) else None
    # Один адрес бывает на двух интерфейсах — номер ему нужен один
    return list(dict.fromkeys(
        entry["address"]
        for interface in interfaces or []
        for entry in interface.get("addresses") or []
        if entry.get("type") == "ipv4" and _is_public_ipv4(entry.get("address"))
    ))


def _is_public_ipv4(address: Optional[str]) -> bool:
    try:
        return ipaddress.IPv4Address(address).is_global
    except ipaddress.AddressValueError:
        return False
