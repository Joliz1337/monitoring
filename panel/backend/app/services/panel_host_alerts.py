"""Алерты о нагрузке хоста самой панели: CPU, память, диск.

В панель могут неделями не заходить, и перегруженный хост замечают, только
когда она уже не открывается. Поэтому алертер следит и за ним — тем же
Telegram-ботом, что и за нодами. CPU и память — среднее за интервал проверки
по истории сэмплера (`panel_host_metrics`), диск — текущее заполнение.

У каждого ресурса свой эпизод `ThresholdWatch`: тревога после удержания выше
порога, повторы не чаще общего cooldown, «в норме» после такого же затишья.
В истории алертов у этих записей `server_id` пустой — сервера панели в списке
серверов нет.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.services.threshold_watch import ThresholdEvent, ThresholdPolicy, ThresholdWatch


class PanelResource(Enum):
    CPU = "cpu"
    RAM = "ram"
    DISK = "disk"


@dataclass(frozen=True)
class PanelReading:
    resource: PanelResource
    percent: float
    free_bytes: Optional[int] = None   # память — доступно, диск — свободно
    total_bytes: Optional[int] = None


def alert_type(resource: PanelResource, event: ThresholdEvent) -> str:
    return f"panel_{resource.value}_{event.value}"


RESOURCE_BY_HIGH_TYPE = {alert_type(r, ThresholdEvent.HIGH): r for r in PanelResource}

# «В норме» по ресурсу закрывает его тревогу — так же, как recovery у offline
PAIRED_ALERT_OPENER = {
    alert_type(r, event): alert_type(r, ThresholdEvent.HIGH)
    for r in PanelResource
    for event in ThresholdEvent
}

SEVERITY = {ThresholdEvent.HIGH: "critical", ThresholdEvent.RECOVERED: "info"}

SERVER_NAME = {"ru": "Сервер панели", "en": "Panel server"}
HEADER = {"ru": "Уведомление панели", "en": "Panel Alert"}


@dataclass
class PanelHostWatch:
    watches: dict[PanelResource, ThresholdWatch] = field(
        default_factory=lambda: {resource: ThresholdWatch() for resource in PanelResource}
    )
    last_alert: dict[str, float] = field(default_factory=dict)

    def evaluate(
        self, reading: PanelReading, policy: ThresholdPolicy, now: float, cooldown: float,
    ) -> Optional[ThresholdEvent]:
        """Событие для отправки: тревогу глушит cooldown, «в норме» — нет."""
        watch = self.watches[reading.resource]
        event = watch.evaluate(reading.percent, now, policy)
        if event is not ThresholdEvent.HIGH:
            return event
        high_type = alert_type(reading.resource, event)
        last = self.last_alert.get(high_type)
        if last is not None and now - last < cooldown:
            return None
        self.last_alert[high_type] = now
        watch.alerted = True
        return event

    def restore_alerted(self, high_type: str) -> None:
        self.watches[RESOURCE_BY_HIGH_TYPE[high_type]].alerted = True

    def high_minutes(self, resource: PanelResource, now: float) -> int:
        return max(1, round(self.watches[resource].high_for(now) / 60))

    def reset(self) -> None:
        for watch in self.watches.values():
            watch.reset()


def language(lang: str) -> str:
    return "ru" if lang == "ru" else "en"


def format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    return f"{size:.1f}".rstrip("0").rstrip(".") + f" {units[unit]}"


def format_message(
    reading: PanelReading, event: ThresholdEvent, threshold: float, minutes: int, lang: str,
) -> str:
    ru = language(lang) == "ru"
    percent = f"{reading.percent:.0f}%"
    limit = f"{threshold:.0f}%"
    free = format_bytes(reading.free_bytes) if reading.free_bytes is not None else "?"
    high = event is ThresholdEvent.HIGH

    if reading.resource is PanelResource.CPU:
        if high:
            return (f"Процессор сервера панели загружен на {percent} уже {minutes} мин (порог {limit})" if ru
                    else f"Panel server CPU at {percent} for {minutes} min (threshold {limit})")
        return (f"Процессор сервера панели в норме: {percent} (порог {limit})" if ru
                else f"Panel server CPU is back to normal: {percent} (threshold {limit})")

    if reading.resource is PanelResource.RAM:
        if high:
            return (f"Память сервера панели занята на {percent} уже {minutes} мин (порог {limit}), "
                    f"свободно {free}" if ru
                    else f"Panel server memory at {percent} for {minutes} min (threshold {limit}), "
                    f"{free} available")
        return (f"Память сервера панели в норме: занято {percent} (порог {limit}), свободно {free}" if ru
                else f"Panel server memory is back to normal: {percent} used (threshold {limit}), {free} available")

    total = format_bytes(reading.total_bytes) if reading.total_bytes is not None else "?"
    if high:
        return (f"Диск сервера панели заполнен на {percent} (порог {limit}), свободно {free} из {total}" if ru
                else f"Panel server disk is {percent} full (threshold {limit}), {free} free of {total}")
    return (f"Диск сервера панели в норме: заполнен на {percent} (порог {limit}), свободно {free}" if ru
            else f"Panel server disk is back to normal: {percent} full (threshold {limit}), {free} free")


def history_details(reading: PanelReading, threshold: float) -> dict:
    details = {"resource": reading.resource.value, "percent": round(reading.percent, 1), "threshold": threshold}
    if reading.free_bytes is not None:
        details["free_bytes"] = reading.free_bytes
    if reading.total_bytes is not None:
        details["total_bytes"] = reading.total_bytes
    return details
