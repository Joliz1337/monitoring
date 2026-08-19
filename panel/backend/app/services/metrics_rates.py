"""Скорости сети и диска в метриках ноды: чьи цифры показывать.

Нода с посекундным семплером присылает байт/с по каждому интерфейсу и диску за
последнюю секунду плюс маркер `live_rates` — такие значения берутся как есть.
Без маркера (старый агент, нода только стартовала, семплер замолчал) скорость
берётся из последнего снапшота панели — среднее за интервал опроса,
размазанное по интерфейсам и дискам пропорционально накопленным байтам.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class NodeRates:
    net_rx: float
    net_tx: float
    disk_read: float
    disk_write: float


def node_live_rates(metrics: dict) -> Optional[NodeRates]:
    """Скорости, посчитанные нодой; None — нода их не прислала."""
    if not metrics.get("live_rates"):
        return None
    net_total = (metrics.get("network") or {}).get("total") or {}
    disk_total = (metrics.get("disk") or {}).get("io_total") or {}
    return NodeRates(
        net_rx=float(net_total.get("rx_bytes_per_sec") or 0),
        net_tx=float(net_total.get("tx_bytes_per_sec") or 0),
        disk_read=float(disk_total.get("read_bytes_per_sec") or 0),
        disk_write=float(disk_total.get("write_bytes_per_sec") or 0),
    )


def enrich_metrics_with_speeds(metrics: dict, snapshot) -> dict:
    """Дописать скорости в ответ ноды для UI.

    Скорости ноды не трогаем. Для старого агента — скорость снапшота,
    распределённая только по физическим интерфейсам (veth/docker/br-* зеркалят
    трафик физических) и по дискам пропорционально накопленным байтам.
    """
    if metrics.get("live_rates") or not snapshot:
        return metrics

    network = metrics.get("network")
    if network:
        total_rx_speed = snapshot.net_rx_bytes_per_sec or 0
        total_tx_speed = snapshot.net_tx_bytes_per_sec or 0

        if "total" in network:
            network["total"]["rx_bytes_per_sec"] = total_rx_speed
            network["total"]["tx_bytes_per_sec"] = total_tx_speed

        interfaces = network.get("interfaces", [])
        physical = [i for i in interfaces if not i.get("is_virtual", False)]
        phys_rx = sum(i.get("rx_bytes", 0) for i in physical)
        phys_tx = sum(i.get("tx_bytes", 0) for i in physical)
        for iface in interfaces:
            if iface.get("is_virtual", False):
                iface["rx_bytes_per_sec"] = 0.0
                iface["tx_bytes_per_sec"] = 0.0
                continue
            if phys_rx > 0:
                iface["rx_bytes_per_sec"] = total_rx_speed * iface.get("rx_bytes", 0) / phys_rx
            if phys_tx > 0:
                iface["tx_bytes_per_sec"] = total_tx_speed * iface.get("tx_bytes", 0) / phys_tx

    io_stats = (metrics.get("disk") or {}).get("io")
    if io_stats:
        disk_read_speed = snapshot.disk_read_bytes_per_sec or 0
        disk_write_speed = snapshot.disk_write_bytes_per_sec or 0
        total_read = sum(d.get("read_bytes", 0) for d in io_stats.values())
        total_write = sum(d.get("write_bytes", 0) for d in io_stats.values())
        for disk_io in io_stats.values():
            if total_read > 0:
                disk_io["read_bytes_per_sec"] = disk_read_speed * disk_io.get("read_bytes", 0) / total_read
            if total_write > 0:
                disk_io["write_bytes_per_sec"] = disk_write_speed * disk_io.get("write_bytes", 0) / total_write

    return metrics
