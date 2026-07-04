"""Общие сетевые хелперы: резолв IP панели/нод и проверка публичности диапазона."""

import ipaddress
import socket
from typing import Optional

from app.config import get_settings

# Приватные/служебные диапазоны, которые нельзя пускать в block-списки:
# DROP по ним убивает loopback, docker-bridge и внутренние сети хостера
# (инцидент с firehol_level1 — он содержит bogon-диапазоны для бордер-роутеров).
NON_PUBLIC_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
))


def is_public_range(ip_cidr: str) -> bool:
    """True, если IP/CIDR не пересекается с приватными/служебными диапазонами."""
    try:
        net = ipaddress.ip_network(ip_cidr, strict=False)
    except ValueError:
        return False
    if net.version != 4:
        return True
    return not any(net.overlaps(bad) for bad in NON_PUBLIC_NETS)


def resolve_panel_ip() -> Optional[str]:
    """IP панели по её домену из настроек (None, если домен не задан/не резолвится)."""
    domain = get_settings().domain
    if not domain:
        return None
    try:
        return socket.gethostbyname(domain)
    except (socket.gaierror, OSError):
        return None


def host_to_ip(host: str) -> Optional[str]:
    """IP из host (уже IP — вернуть как есть, иначе DNS-резолв)."""
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        return socket.gethostbyname(host)
    except (socket.gaierror, OSError):
        return None
