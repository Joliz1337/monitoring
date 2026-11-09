"""Веса бэкендов балансировщика HAProxy по загрузке их канала и процессора.

Чистый расчёт без ввода-вывода: что отправить релеям, решает он, а ходит в БД
и на ноды исполнитель (`haproxy_load_weights_runner`).
"""

import json
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from app.services.haproxy_config import BackendServer

# До LOAD_SOFT сервер получает долю по каналу целиком, дальше доля линейно
# уходит в ноль к LOAD_HARD: забитому серверу новых клиентов не шлём
LOAD_SOFT = 0.70
LOAD_HARD = 0.95
# Шкала веса от 100 — шаг в процент, как у ручных весов формы правила
MAX_WEIGHT = 100

BITS_PER_BYTE = 8
BITS_PER_MBIT = 1_000_000


@dataclass(frozen=True)
class NodeLoad:
    rx_bytes_per_sec: float
    tx_bytes_per_sec: float
    cpu_percent: float


@dataclass(frozen=True)
class LineWeight:
    server_name: str
    panel_server_id: int
    weight: int
    # None — свежих данных о нагрузке нет, стоит вес по каналу
    utilization: Optional[float]
    capacity_mbit: int


def _round(value: float) -> int:
    # round() банковский: 2.5 дал бы 2, а соседний 3.5 — 4
    return int(value + 0.5)


def utilization(load: NodeLoad, capacity_mbit: int) -> float:
    """Порт full-duplex, поэтому упирается большее из направлений; процессор
    может упереться раньше канала (шифрование на ВМ без AES-NI)."""
    busiest_bytes = max(load.rx_bytes_per_sec, load.tx_bytes_per_sec)
    net = busiest_bytes * BITS_PER_BYTE / (capacity_mbit * BITS_PER_MBIT)
    return min(max(net, load.cpu_percent / 100, 0.0), 1.0)


def load_factor(util: float) -> float:
    if util <= LOAD_SOFT:
        return 1.0
    if util >= LOAD_HARD:
        return 0.0
    return (LOAD_HARD - util) / (LOAD_HARD - LOAD_SOFT)


def rule_line_weights(
    servers: Sequence[BackendServer],
    owners: Mapping[str, int],
    capacities: Mapping[int, int],
    loads: Mapping[int, NodeLoad],
) -> list[LineWeight]:
    """Веса строк одного правила. Строки без сервера панели или без канала не
    получают веса — у них остаётся вес из конфига."""
    candidates: list[tuple[BackendServer, int]] = []
    for line in servers:
        if line.backup or line.disabled:
            continue
        server_id = owners.get(line.address.strip().lower())
        if server_id is not None and capacities.get(server_id):
            candidates.append((line, server_id))
    if not candidates:
        return []

    node_ids = list(dict.fromkeys(server_id for _, server_id in candidates))
    max_capacity = max(capacities[server_id] for server_id in node_ids)
    base = {server_id: MAX_WEIGHT * capacities[server_id] / max_capacity for server_id in node_ids}
    util = {
        server_id: utilization(loads[server_id], capacities[server_id])
        for server_id in node_ids if server_id in loads
    }
    node_weight = {
        server_id: _round(base[server_id] * load_factor(util[server_id])) if server_id in util else _round(base[server_id])
        for server_id in node_ids
    }
    # Все разом забиты — отрезать клиентов хуже, чем дать им лаги
    if not any(node_weight.values()):
        node_weight = {server_id: _round(base[server_id]) for server_id in node_ids}

    # Канал у сервера один на все его адреса: иначе нода с тремя IP получила бы тройную долю.
    # Копии одного адреса:порта под выходные IP релея (_oN) — одна цель, вес у них общий.
    targets: dict[int, set[tuple[str, int]]] = {}
    for line, server_id in candidates:
        targets.setdefault(server_id, set()).add((line.address.strip().lower(), line.port))

    result = []
    for line, server_id in candidates:
        weight = node_weight[server_id]
        share = 0 if weight == 0 else max(1, _round(weight / len(targets[server_id])))
        result.append(LineWeight(line.name, server_id, share, util.get(server_id), capacities[server_id]))
    return result


def nic_speed_mbps(last_metrics: Optional[str]) -> Optional[int]:
    """Скорость карты, через которую идёт трафик: подсказка к каналу сервера.

    Виртуальные интерфейсы (docker0, veth) отчитываются фиктивными 10 Гбит/с,
    поэтому берётся невиртуальный с наибольшим трафиком."""
    if not last_metrics:
        return None
    try:
        metrics = json.loads(last_metrics)
    except json.JSONDecodeError:
        return None
    network = metrics.get("network") if isinstance(metrics, dict) else None
    interfaces = network.get("interfaces") if isinstance(network, dict) else None
    candidates = [
        interface for interface in interfaces or []
        if isinstance(interface, dict)
        and not interface.get("is_virtual")
        and isinstance(interface.get("speed_mbps"), int)
        and interface["speed_mbps"] > 0
    ]
    if not candidates:
        return None
    busiest = max(candidates, key=lambda i: (i.get("rx_bytes") or 0) + (i.get("tx_bytes") or 0))
    return busiest["speed_mbps"]
