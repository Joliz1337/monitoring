"""Веса бэкендов балансировщика HAProxy по загрузке их канала и процессора.

Чистый расчёт без ввода-вывода: что отправить релеям, решает он, а ходит в БД
и на ноды исполнитель (`haproxy_load_weights_runner`).
"""

import json
from typing import Optional


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
