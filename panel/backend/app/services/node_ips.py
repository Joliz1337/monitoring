"""Адреса нод, которые панель знает: хост из URL и IPv4 интерфейсов из метрик.

С интерфейсов берутся только публичные адреса. Приватные повторяются в чужих
сетях: docker 172.17.0.1 и 10.0.0.1 есть на многих нодах, а белый список
пускает адрес мимо файрвола на всех нодах сразу — сосед по VLAN хостера с тем
же 10.x получил бы доступ ко всем портам. Хост из URL берётся любой, включая
приватный: его оператор задал сам, по нему панель ходит к ноде.
"""

import asyncio
import ipaddress
import json
from typing import Iterable, Optional
from urllib.parse import urlparse

from app.services.net_utils import host_to_ip


def url_host(url: str) -> Optional[str]:
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def public_interface_ipv4(last_metrics: Optional[str]) -> list[str]:
    """Публичные IPv4 всех интерфейсов в порядке появления, без повторов."""
    if not last_metrics:
        return []
    try:
        metrics = json.loads(last_metrics)
    except json.JSONDecodeError:
        return []
    network = metrics.get("network") if isinstance(metrics, dict) else None
    interfaces = network.get("interfaces") if isinstance(network, dict) else None
    # Один адрес бывает на двух интерфейсах
    return list(dict.fromkeys(
        entry["address"]
        for interface in interfaces or []
        if isinstance(interface, dict)
        for entry in interface.get("addresses") or []
        if isinstance(entry, dict) and entry.get("type") == "ipv4" and _is_public_ipv4(entry.get("address"))
    ))


async def collect_node_ips(nodes: Iterable[tuple[str, Optional[str]]]) -> set[str]:
    """Все адреса нод для белых списков по парам (url, last_metrics).

    Сюда попадают доп. адреса и вторые карты — трафик ноды с них не должен
    упираться в блок-лист или лимиты Анти-DDoS соседей. Домены из URL
    резолвятся параллельно."""
    nodes = list(nodes)
    resolved = await asyncio.gather(*(host_to_ip(url_host(url) or "") for url, _ in nodes))
    ips = {ip for ip in resolved if ip}
    for _, last_metrics in nodes:
        ips.update(public_interface_ipv4(last_metrics))
    return ips


def _is_public_ipv4(address: Optional[str]) -> bool:
    try:
        return ipaddress.IPv4Address(address).is_global
    except ipaddress.AddressValueError:
        return False
