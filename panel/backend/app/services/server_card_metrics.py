"""Срез ответа ноды для списка серверов.

Полный ответ ноды — около 15 КБ: процессы, счётчики интерфейсов, эфемерные порты,
диски. Карточкам дашборда, сводке парка и дереву инфраструктуры из него нужны два
десятка чисел, а полные ответы 2000 нод — это 30 МБ JSON на каждый опрос списка.

Срез повторяет форму ответа (те же поля на тех же местах), поэтому фронтенд читает
его теми же путями. Набор полей обязан совпадать с типом `ServerCardMetrics` в
`panel/frontend/src/api/client.ts`: поле, которого здесь нет, до страницы не дойдёт.
"""

from typing import Any, Iterable, Optional

CPU_FIELDS = ("usage_percent", "per_cpu_percent", "cores_logical", "load_avg_1", "load_avg_5", "load_avg_15")
RAM_FIELDS = ("percent", "used", "total")
PARTITION_FIELDS = ("percent", "used", "total")
SPEED_FIELDS = ("rx_bytes_per_sec", "tx_bytes_per_sec")
SYSTEM_FIELDS = ("hostname", "os", "uptime_seconds")
EXPIRY_FIELDS = ("domain", "days_left", "expired")
WINDOW_FIELDS = ("cpu_avg", "per_cpu_avg", "net_rx_avg", "net_tx_avg")

# Петля и link-local есть на каждом сервере — по ним поиск находил бы весь парк
UNSEARCHABLE_PREFIXES = ("127.", "169.254.", "::1", "fe80:")


def _pick(source: Optional[dict], fields: Iterable[str]) -> dict:
    source = source or {}
    return {name: source[name] for name in fields if name in source}


def _total_speed(metrics: dict, snapshot: Any) -> Optional[dict]:
    total = (metrics.get("network") or {}).get("total")
    if total is None:
        return None
    # Старый агент скоростей не считает — их панель берёт из своего последнего снапшота
    if snapshot is not None and not metrics.get("live_rates"):
        return {
            "rx_bytes_per_sec": snapshot.net_rx_bytes_per_sec or 0,
            "tx_bytes_per_sec": snapshot.net_tx_bytes_per_sec or 0,
        }
    return _pick(total, SPEED_FIELDS)


def _searchable_addresses(interface: dict) -> list[dict]:
    return [
        {"address": entry["address"]}
        for entry in interface.get("addresses") or []
        if entry.get("address") and not entry["address"].lower().startswith(UNSEARCHABLE_PREFIXES)
    ]


def _searchable_interfaces(metrics: dict) -> list[dict]:
    """Адреса, по которым сервер ищут на дашборде. Виртуальные карты (docker, veth,
    wg) пропускаются: их адреса повторяются на многих нодах."""
    interfaces = (metrics.get("network") or {}).get("interfaces") or []
    found = [
        {"addresses": _searchable_addresses(interface)}
        for interface in interfaces
        if not interface.get("is_virtual", False)
    ]
    return [interface for interface in found if interface["addresses"]]


def card_metrics(metrics: dict, snapshot: Any = None) -> dict:
    """Срез ответа ноды; `snapshot` — последний снапшот сервера, нужен только
    старому агенту. Ответ общий для всех читателей хранилища и не меняется."""
    partitions = (metrics.get("disk") or {}).get("partitions") or []
    card = {
        "cpu": _pick(metrics.get("cpu"), CPU_FIELDS),
        "memory": {"ram": _pick((metrics.get("memory") or {}).get("ram"), RAM_FIELDS)},
        # Карточка показывает первый раздел
        "disk": {"partitions": [_pick(partitions[0], PARTITION_FIELDS)] if partitions else []},
        "network": {
            "total": _total_speed(metrics, snapshot),
            "interfaces": _searchable_interfaces(metrics),
        },
        "system": _pick(metrics.get("system"), SYSTEM_FIELDS),
    }

    expiry = (metrics.get("certificates") or {}).get("closest_expiry")
    if expiry:
        card["certificates"] = {"closest_expiry": _pick(expiry, EXPIRY_FIELDS)}
    window = metrics.get("window")
    if window:
        card["window"] = _pick(window, WINDOW_FIELDS)
    return card
