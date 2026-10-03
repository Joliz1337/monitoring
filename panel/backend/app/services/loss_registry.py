"""Последние данные о потерях с каждой ноды — в памяти панели.

Коллектор метрик кладёт сюда блок `loss_probe` и IP интерфейсов ноды при
каждом опросе (раз в ~10 с). Отсюда читают алерт о потерях и страница
«Потери»: без этого обоим пришлось бы заново разбирать `last_metrics` всего
парка — десятки килобайт JSON на ноду при каждом обращении.
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Mapping, Optional

# Нода, не отвечавшая дольше этого, считается без данных: её старые цифры
# не должны ни открывать, ни закрывать эпизод потерь
STALE_AFTER_SEC = 90.0
# Адрес вне учёта с галочкой «только полные потери»: частичные потери на нём
# известны и не интересны, учитывается только почти полный отказ
TOTAL_LOSS_PCT = 95.0


class TargetMode(str, Enum):
    TRACKED = "tracked"
    TOTAL_ONLY = "total_only"
    HIDDEN = "hidden"


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
class ExcludedTarget:
    ip: str
    # None — все порты адреса
    port: Optional[int]
    total_only: bool = False

    @property
    def mode(self) -> TargetMode:
        return TargetMode.TOTAL_ONLY if self.total_only else TargetMode.HIDDEN


@dataclass(frozen=True)
class TargetRules:
    """Как учитывать адрес назначения. Правило на IP:порт сильнее правила на весь IP."""
    ips: Mapping[str, TargetMode] = field(default_factory=dict)
    endpoints: Mapping[tuple[str, int], TargetMode] = field(default_factory=dict)

    def mode(self, reading: LossReading) -> TargetMode:
        return (
            self.endpoints.get((reading.ip, reading.port))
            or self.ips.get(reading.ip)
            or TargetMode.TRACKED
        )


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
        # Серверы вне учёта потерь (loss_exclusions): снимки их храним — нужны их
        # адреса, чтобы спрятать и адреса назначения на них
        self._excluded: frozenset[int] = frozenset()
        self._target_rules = TargetRules()

    def set_excluded(self, server_ids: Iterable[int]) -> None:
        self._excluded = frozenset(server_ids)

    def set_excluded_targets(self, targets: Iterable[ExcludedTarget]) -> None:
        ips: dict[str, TargetMode] = {}
        endpoints: dict[tuple[str, int], TargetMode] = {}
        for target in targets:
            if target.port is None:
                ips[target.ip] = target.mode
            else:
                endpoints[(target.ip, target.port)] = target.mode
        self._target_rules = TargetRules(ips=ips, endpoints=endpoints)

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

    def _recent(self, max_age: float) -> list[RelaySnapshot]:
        now = self._clock()
        return [s for s in self._snapshots.values() if now - s.updated_at <= max_age]

    def fresh(self, max_age: float = STALE_AFTER_SEC) -> list[RelaySnapshot]:
        """Релеи, чьи проверки учитываются: свежие и не исключённые."""
        return [s for s in self._recent(max_age) if s.server_id not in self._excluded]

    def owners(self, max_age: float = STALE_AFTER_SEC) -> dict[str, str]:
        """IP → имя ноды, на которой он висит."""
        return {address: s.name for s in self._recent(max_age) for address in s.addresses}

    def target_rules(self, max_age: float = STALE_AFTER_SEC) -> TargetRules:
        """Адреса вне учёта: заданные вручную и все IP исключённых серверов
        (ручное правило на тот же IP сильнее)."""
        server_ips = {
            address: TargetMode.HIDDEN
            for s in self._recent(max_age) if s.server_id in self._excluded for address in s.addresses
        }
        return TargetRules(
            ips={**server_ips, **self._target_rules.ips},
            endpoints=self._target_rules.endpoints,
        )


_registry: Optional[LossRegistry] = None


def get_loss_registry() -> LossRegistry:
    global _registry
    if _registry is None:
        _registry = LossRegistry()
    return _registry
