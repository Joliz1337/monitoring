"""Последние данные о потерях с каждой ноды — в памяти панели.

Коллектор метрик кладёт сюда блок `loss_probe` и IP интерфейсов ноды при
каждом опросе (раз в ~10 с). Отсюда читают алерт о потерях и страница
«Потери»: без этого обоим пришлось бы заново разбирать `last_metrics` всего
парка — десятки килобайт JSON на ноду при каждом обращении.
"""

import time
from dataclasses import dataclass
from typing import Callable, Optional

# Нода, не отвечавшая дольше этого, считается без данных: её старые цифры
# не должны ни открывать, ни закрывать эпизод потерь
STALE_AFTER_SEC = 90.0


@dataclass(frozen=True)
class LossReading:
    ip: str
    port: int
    loss_pct: float
    rtt_ms: Optional[float]
    samples: int

    @property
    def key(self) -> str:
        return f"{self.ip}:{self.port}"


@dataclass(frozen=True)
class RelaySnapshot:
    server_id: int
    name: str
    readings: tuple[LossReading, ...]
    addresses: frozenset[str]
    updated_at: float


def parse_readings(metrics: dict) -> tuple[LossReading, ...]:
    """Блок `loss_probe` метрик ноды; битые записи пропускаются."""
    readings = []
    for entry in metrics.get("loss_probe") or []:
        try:
            rtt = entry.get("rtt_ms")
            readings.append(LossReading(
                ip=str(entry["ip"]),
                port=int(entry["port"]),
                loss_pct=float(entry["loss_pct"]),
                rtt_ms=None if rtt is None else float(rtt),
                samples=int(entry.get("samples", 0)),
            ))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return tuple(readings)


def parse_addresses(metrics: dict) -> frozenset[str]:
    """IPv4 интерфейсов ноды, включая дополнительные адреса."""
    found = set()
    for iface in (metrics.get("network") or {}).get("interfaces") or []:
        if not isinstance(iface, dict):
            continue
        for address in iface.get("addresses") or []:
            if isinstance(address, dict) and address.get("type") == "ipv4" and address.get("address"):
                found.add(str(address["address"]))
    return frozenset(found)


class LossRegistry:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._snapshots: dict[int, RelaySnapshot] = {}

    def update(self, server_id: int, name: str, metrics: dict) -> None:
        self._snapshots[server_id] = RelaySnapshot(
            server_id=server_id,
            name=name,
            readings=parse_readings(metrics),
            addresses=parse_addresses(metrics),
            updated_at=self._clock(),
        )

    def forget(self, server_id: int) -> None:
        self._snapshots.pop(server_id, None)

    def fresh(self, max_age: float = STALE_AFTER_SEC) -> list[RelaySnapshot]:
        now = self._clock()
        return [s for s in self._snapshots.values() if now - s.updated_at <= max_age]

    def owners(self, max_age: float = STALE_AFTER_SEC) -> dict[str, str]:
        """IP → имя ноды, на которой он висит."""
        return {address: s.name for s in self.fresh(max_age) for address in s.addresses}


_registry: Optional[LossRegistry] = None


def get_loss_registry() -> LossRegistry:
    global _registry
    if _registry is None:
        _registry = LossRegistry()
    return _registry
