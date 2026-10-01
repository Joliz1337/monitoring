"""Карта «адрес → сервер панели»: подписи целей правил HAProxy именами серверов.

Источник — то, что панель уже хранит: хост из URL сервера и IPv4 интерфейсов из
последних метрик ноды. С интерфейсов берутся только публичные адреса: docker0
172.17.0.1 или общий 10.0.0.1 есть на многих нодах, и цель подписывалась бы
случайным сервером. Хост из URL берётся любой, включая домен, — его оператор
задал сам, и цель правила бывает доменом.
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
    for server in servers:
        host = _url_host(server.url)
        if host:
            owners.setdefault(host, IpOwner(server.id, server.name))
    for server in servers:
        for address in _public_interface_ipv4(server.last_metrics):
            owners.setdefault(address, IpOwner(server.id, server.name))
    return owners


def _url_host(url: str) -> Optional[str]:
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def _public_interface_ipv4(last_metrics: Optional[str]) -> list[str]:
    if not last_metrics:
        return []
    try:
        metrics = json.loads(last_metrics)
    except json.JSONDecodeError:
        return []
    network = metrics.get("network") if isinstance(metrics, dict) else None
    interfaces = network.get("interfaces") if isinstance(network, dict) else None
    return [
        entry["address"]
        for interface in interfaces or []
        for entry in interface.get("addresses") or []
        if entry.get("type") == "ipv4" and _is_public_ipv4(entry.get("address"))
    ]


def _is_public_ipv4(address: Optional[str]) -> bool:
    try:
        return ipaddress.IPv4Address(address).is_global
    except ipaddress.AddressValueError:
        return False
