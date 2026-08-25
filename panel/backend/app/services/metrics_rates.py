"""Нагрузка и скорости в метриках ноды: чьи цифры брать.

Нода с посекундным семплером отвечает на `GET /api/metrics?window=N` блоком
`window` — средние и максимумы за последние N секунд; они ложатся в историю.
Без блока (старый агент) остаются секундные скорости с маркером `live_rates`,
а без него (нода только стартовала, семплер замолчал) — дельты счётчиков,
посчитанные панелью за интервал опроса. Для UI скорость старого агента
размазывается по интерфейсам и дискам пропорционально накопленным байтам.
"""

from dataclasses import dataclass
from typing import Optional

# Глубина кольцевого буфера семплера ноды: окно длиннее она отдать не может.
MAX_WINDOW_SEC = 330


@dataclass(frozen=True)
class NodeRates:
    net_rx: float
    net_tx: float
    disk_read: float
    disk_write: float


@dataclass(frozen=True)
class NodeWindow:
    window_sec: float
    samples: int
    cpu_avg: float
    cpu_max: float
    per_cpu_avg: tuple[float, ...]
    net_rx: float
    net_tx: float
    net_rx_max: float
    net_tx_max: float
    disk_read: float
    disk_write: float


@dataclass(frozen=True)
class SnapshotRates:
    """Что ложится в строку metrics_snapshots; `*_max` — None, когда пика нет."""
    cpu_usage: float
    per_cpu_percent: list[float]
    net_rx: float
    net_tx: float
    disk_read: float
    disk_write: float
    cpu_usage_max: Optional[float]
    net_rx_max: Optional[float]
    net_tx_max: Optional[float]


def poll_window_seconds(elapsed: Optional[float], interval: int) -> int:
    """Сколько секунд окна просить у ноды.

    Окно ложится на промежуток с прошлого удачного опроса этой ноды по
    monotonic-часам панели — так дрейф цикла, пропуски circuit breaker и часы
    ноды (их двигает time_sync) не режут историю. Первый опрос после старта
    и слишком частый — интервал, слишком редкий — потолок буфера ноды.
    """
    if elapsed is None:
        return interval
    return max(interval, min(MAX_WINDOW_SEC, round(elapsed)))


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


def node_window_rates(metrics: dict) -> Optional[NodeWindow]:
    """Средние и пики за окно опроса; None — блока нет или в нём ни одного замера."""
    window = metrics.get("window")
    if not window or not window.get("samples"):
        return None
    return NodeWindow(
        window_sec=float(window.get("window_sec") or 0),
        samples=int(window["samples"]),
        cpu_avg=float(window.get("cpu_avg") or 0),
        cpu_max=float(window.get("cpu_max") or 0),
        per_cpu_avg=tuple(float(value) for value in window.get("per_cpu_avg") or ()),
        net_rx=float(window.get("net_rx_avg") or 0),
        net_tx=float(window.get("net_tx_avg") or 0),
        net_rx_max=float(window.get("net_rx_max") or 0),
        net_tx_max=float(window.get("net_tx_max") or 0),
        disk_read=float(window.get("disk_read_avg") or 0),
        disk_write=float(window.get("disk_write_avg") or 0),
    )


def snapshot_rates(metrics: dict, fallback: NodeRates) -> SnapshotRates:
    """Источник цифр для снапшота: окно ноды → её секундные скорости → дельты панели.

    `cpu_usage_max` — максимум секундного среднего по ядрам за окно,
    не самое горячее ядро.
    """
    cpu = metrics.get("cpu") or {}
    window = node_window_rates(metrics)
    if window:
        # Окно без единого измеренного CPU-замера (нода отдала per_cpu_avg=[])
        # не должно записать ноль: берём секундную пробу, пика у неё нет.
        cpu_measured = bool(window.per_cpu_avg)
        return SnapshotRates(
            cpu_usage=window.cpu_avg if cpu_measured else cpu.get("usage_percent", 0),
            per_cpu_percent=list(window.per_cpu_avg) if cpu_measured else cpu.get("per_cpu_percent") or [],
            net_rx=window.net_rx,
            net_tx=window.net_tx,
            disk_read=window.disk_read,
            disk_write=window.disk_write,
            cpu_usage_max=window.cpu_max if cpu_measured else None,
            net_rx_max=window.net_rx_max,
            net_tx_max=window.net_tx_max,
        )

    rates = node_live_rates(metrics) or fallback
    return SnapshotRates(
        cpu_usage=cpu.get("usage_percent", 0),
        per_cpu_percent=cpu.get("per_cpu_percent") or [],
        net_rx=rates.net_rx,
        net_tx=rates.net_tx,
        disk_read=rates.disk_read,
        disk_write=rates.disk_write,
        cpu_usage_max=None,
        net_rx_max=None,
        net_tx_max=None,
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
