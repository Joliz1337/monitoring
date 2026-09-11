"""Занятость эфемерных портов по исходящим адресам и направлениям.

Потолок исходящих TCP-соединений — не на хост, а на четвёрку
(src ip, src port, dst ip, dst port): на пару «наш адрес → адрес:порт цели»
ядро выдаст не больше портов, чем в ip_local_port_range за вычетом
ip_local_reserved_ports. HAProxy, гонящий всех клиентов на один backend, или
Xray, у которого все клиенты идут на один адрес Google, упираются в этот
потолок задолго до нехватки портов на хосте в целом — connect() начинает
перебирать весь диапазон под спинлоком, и процессор уходит в system time.
Здесь по /proc/net/tcp{,6} считается, сколько портов занято на каждом
исходящем адресе всего и на каждом направлении, чтобы панель показала
остаток до потолка.

Входящие соединения из учёта исключаются по локальному порту: у них это порт
слушающего сокета, а не выданный ядром эфемерный. Адреса держатся в hex-виде
из /proc до самой выдачи — на ноде с сотней тысяч сокетов разбирать текст
каждого было бы дороже самого чтения файла.
"""

import heapq
import ipaddress
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

STATE_LISTEN = "0A"
STATE_TIME_WAIT = "06"
UNCONNECTED_PORT = "0000"

MAX_SOURCES = 32
MAX_DESTINATIONS = 10

# Дефолты ядра — на случай, если /proc не читается
DEFAULT_PORT_RANGE = (32768, 60999)
DEFAULT_TW_REUSE = 2

TW_REUSE_ALL = 1
TW_REUSE_LOOPBACK = 2

# /proc печатает каждое 32-битное слово адреса в порядке байтов хоста, поэтому
# IPv4-mapped IPv6 (::ffff:a.b.c.d) на little-endian выглядит как три слова
# "00000000 00000000 FFFF0000" плюс слово с IPv4 — ровно то, что лежит в
# /proc/net/tcp для того же адреса
_LITTLE_ENDIAN = sys.byteorder == "little"
_MAPPED_V4_PREFIX = "0000000000000000FFFF0000" if _LITTLE_ENDIAN else "00000000000000000000FFFF"
_LOOPBACK_V6_HEX = "00000000000000000000000001000000" if _LITTLE_ENDIAN else "00000000000000000000000000000001"


@dataclass(frozen=True)
class KernelPortSettings:
    low: int
    high: int
    reserved_in_range: int
    tw_reuse: int

    @property
    def capacity(self) -> int:
        return self.high - self.low + 1 - self.reserved_in_range


@dataclass
class _Bucket:
    used: int = 0
    time_wait: int = 0

    @property
    def total(self) -> int:
        return self.used + self.time_wait


def parse_port_range(text: str) -> tuple[int, int] | None:
    parts = text.split()
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    low, high = int(parts[0]), int(parts[1])
    if not 1 <= low <= high <= 65535:
        return None
    return low, high


def count_reserved_in_range(text: str, low: int, high: int) -> int:
    """ip_local_reserved_ports («7500,7501-7564,2222») → сколько портов попало в диапазон."""
    total = 0
    for token in text.split(","):
        token = token.strip()
        start_text, _, end_text = token.partition("-")
        if not start_text.isdigit() or (end_text and not end_text.isdigit()):
            continue
        start = int(start_text)
        end = int(end_text) if end_text else start
        overlap = min(end, high) - max(start, low) + 1
        if overlap > 0:
            total += overlap
    return total


def read_kernel_settings(proc_root: Path) -> KernelPortSettings:
    sysctl_dir = proc_root / "sys/net/ipv4"
    low, high = _read_text(sysctl_dir / "ip_local_port_range", parse_port_range) or DEFAULT_PORT_RANGE
    reserved = _read_text(sysctl_dir / "ip_local_reserved_ports", lambda text: count_reserved_in_range(text, low, high)) or 0
    tw_reuse = _read_text(sysctl_dir / "tcp_tw_reuse", lambda text: int(text.strip()))
    if tw_reuse is None:
        tw_reuse = DEFAULT_TW_REUSE
    return KernelPortSettings(low=low, high=high, reserved_in_range=reserved, tw_reuse=tw_reuse)


def _read_text(path: Path, parse):
    try:
        return parse(path.read_text())
    except (OSError, ValueError):
        return None


def scan_listening_ports(content: str) -> set[str]:
    """Hex-порты слушающих сокетов. Предфильтр по подстроке: столбец st — единственное
    место строки, где двухсимвольный токен «0A» стоит между пробелами."""
    ports: set[str] = set()
    for line in content.split("\n"):
        if " 0A " not in line:
            continue
        parts = line.split(None, 4)
        if len(parts) >= 4 and parts[3] == STATE_LISTEN:
            ports.add(parts[1].rpartition(":")[2])
    return ports


def decode_address(hex_ip: str) -> str:
    """Адрес из /proc/net/tcp{,6} в текст."""
    words = [bytes.fromhex(hex_ip[offset:offset + 8]) for offset in range(0, len(hex_ip), 8)]
    if _LITTLE_ENDIAN:
        words = [word[::-1] for word in words]
    packed = b"".join(words)
    if len(packed) == 4:
        return ".".join(str(byte) for byte in packed)
    if len(packed) == 16:
        return str(ipaddress.IPv6Address(packed))
    raise ValueError(f"Не адрес из /proc/net/tcp: {hex_ip!r}")


def _is_loopback(hex_ip: str) -> bool:
    if len(hex_ip) == 8:
        # 127.0.0.0/8: на little-endian первый октет — последние два символа
        octet = hex_ip[6:8] if _LITTLE_ENDIAN else hex_ip[0:2]
        return octet == "7F"
    return hex_ip == _LOOPBACK_V6_HEX


class EphemeralPortsAggregator:
    """Копит сокеты построчно из /proc/net/tcp{,6}; summarize() отдаёт словарь для метрик."""

    def __init__(self, settings: KernelPortSettings, listening_ports: set[str]):
        self._settings = settings
        self._listening = listening_ports
        self._sources: dict[str, _Bucket] = defaultdict(_Bucket)
        self._destinations: dict[tuple[str, str, str], _Bucket] = defaultdict(_Bucket)

    def add(self, local: str, remote: str, state: str) -> None:
        if state == STATE_LISTEN:
            return
        local_ip, _, local_port = local.rpartition(":")
        if local_port in self._listening:
            return
        remote_ip, _, remote_port = remote.rpartition(":")
        if remote_port == UNCONNECTED_PORT:
            return
        port = int(local_port, 16)
        if port < self._settings.low or port > self._settings.high:
            return
        if local_ip.startswith(_MAPPED_V4_PREFIX):
            local_ip = local_ip[len(_MAPPED_V4_PREFIX):]
        if remote_ip.startswith(_MAPPED_V4_PREFIX):
            remote_ip = remote_ip[len(_MAPPED_V4_PREFIX):]
        source = self._sources[local_ip]
        destination = self._destinations[(local_ip, remote_ip, remote_port)]
        if state == STATE_TIME_WAIT:
            source.time_wait += 1
            destination.time_wait += 1
        else:
            source.used += 1
            destination.used += 1

    def summarize(self) -> dict:
        settings = self._settings
        by_source: dict[str, list[tuple[tuple[str, str], _Bucket]]] = defaultdict(list)
        for (local_ip, remote_ip, remote_port), bucket in self._destinations.items():
            by_source[local_ip].append(((remote_ip, remote_port), bucket))

        top_sources = heapq.nlargest(MAX_SOURCES, self._sources.items(), key=lambda item: item[1].total)
        sources = []
        for local_ip, bucket in top_sources:
            tw_reusable = self._time_wait_reusable(local_ip)
            destinations = by_source.get(local_ip, [])
            # Направления ранжируются по занятому, а не по числу сокетов: при
            # tcp_tw_reuse=1 направление с горой TIME_WAIT потолок не подпирает,
            # и первым должно стоять то, которому до него реально ближе всех
            top_destinations = heapq.nlargest(
                MAX_DESTINATIONS, destinations, key=lambda item: self._held(item[1], tw_reusable)
            )
            sources.append({
                "ip": decode_address(local_ip),
                "used": bucket.used,
                "time_wait": bucket.time_wait,
                "destinations_total": len(destinations),
                "destinations": [
                    self._describe(remote_ip, remote_port, dest, tw_reusable)
                    for (remote_ip, remote_port), dest in top_destinations
                ],
            })

        return {
            "range_low": settings.low,
            "range_high": settings.high,
            "reserved": settings.reserved_in_range,
            "capacity": settings.capacity,
            "tw_reuse": settings.tw_reuse,
            "sources": sources,
        }

    def _time_wait_reusable(self, local_ip: str) -> bool:
        # tcp_tw_reuse=1 — TIME_WAIT переиспользуется для любого исходящего,
        # 2 (дефолт ядра) — только для loopback; 0 — порт занят до истечения таймера
        if self._settings.tw_reuse == TW_REUSE_ALL:
            return True
        return self._settings.tw_reuse == TW_REUSE_LOOPBACK and _is_loopback(local_ip)

    def _describe(self, remote_ip: str, remote_port: str, bucket: _Bucket, tw_reusable: bool) -> dict:
        held = self._held(bucket, tw_reusable)
        return {
            "ip": decode_address(remote_ip),
            "port": int(remote_port, 16),
            "used": bucket.used,
            "time_wait": bucket.time_wait,
            "held": held,
            "free": max(self._settings.capacity - held, 0),
        }

    @staticmethod
    def _held(bucket: _Bucket, tw_reusable: bool) -> int:
        """Сколько номеров направление реально держит: при tcp_tw_reuse ядро отдаёт
        порты из TIME_WAIT новым исходящим сразу, и занятыми они не считаются."""
        return bucket.used if tw_reusable else bucket.total
