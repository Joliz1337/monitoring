"""Адреса нод, которые панель знает: хост из URL и IPv4 интерфейсов из метрик.

С интерфейсов берутся только публичные адреса. Приватные повторяются в чужих
сетях: docker 172.17.0.1 и 10.0.0.1 есть на многих нодах, а белый список
пускает адрес мимо файрвола на всех нодах сразу — сосед по VLAN хостера с тем
же 10.x получил бы доступ ко всем портам. Вместо приватного адреса берётся тот,
под которым он выходит через NAT облака, — нода сама узнаёт его и присылает в
поле `public_address`. Хост из URL берётся любой, включая приватный: его
оператор задал сам, по нему панель ходит к ноде.

Метрики присылает сама нода, а белый список действует на весь парк, поэтому
доверие к ним ограничено. Нода на персональном сертификате (`dedicated_cert`)
может принадлежать другому владельцу — от неё берётся только хост из URL.
Остальные ноды дают не больше `MAX_METRIC_ADDRESSES_PER_NODE` адресов из метрик
(в порядке появления); хост из URL в этот лимит не входит.
"""

import asyncio
import ipaddress
from itertools import islice
from typing import Iterable, Optional
from urllib.parse import urlparse

from app.services.net_utils import host_to_ip

MAX_METRIC_ADDRESSES_PER_NODE = 256


def url_host(url: str) -> Optional[str]:
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def public_interface_ipv4(metrics: Optional[dict]) -> list[str]:
    """Публичные IPv4 всех интерфейсов из последнего ответа ноды в порядке
    появления, без повторов: адреса карт и адреса, под которыми приватные
    выходят через NAT."""
    if not metrics:
        return []
    network = metrics.get("network")
    interfaces = network.get("interfaces") if isinstance(network, dict) else None
    # Один адрес бывает на двух интерфейсах
    return list(dict.fromkeys(
        address
        for interface in interfaces or []
        if isinstance(interface, dict)
        for entry in interface.get("addresses") or []
        if isinstance(entry, dict) and entry.get("type") == "ipv4"
        for address in (entry.get("address"), entry.get("public_address"))
        if _is_public_ipv4(address)
    ))


async def collect_node_ips(nodes: Iterable[tuple[str, Optional[dict], bool]]) -> set[str]:
    """Все адреса нод для белых списков по тройкам (url, последний ответ ноды, dedicated_cert).

    Сюда попадают доп. адреса и вторые карты — трафик ноды с них не должен
    упираться в блок-лист или лимиты Анти-DDoS соседей. Домены из URL
    резолвятся параллельно."""
    nodes = list(nodes)
    resolved = await asyncio.gather(*(host_to_ip(url_host(url) or "") for url, _, _ in nodes))
    ips = {ip for ip in resolved if ip}
    for _, metrics, dedicated_cert in nodes:
        if dedicated_cert:
            continue
        ips.update(islice(public_interface_ipv4(metrics), MAX_METRIC_ADDRESSES_PER_NODE))
    return ips


def _is_public_ipv4(address: object) -> bool:
    # Число IPv4Address тоже примет как адрес — в метриках это был бы мусор
    if not isinstance(address, str):
        return False
    try:
        return ipaddress.IPv4Address(address).is_global
    except ipaddress.AddressValueError:
        return False
