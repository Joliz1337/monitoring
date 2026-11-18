"""Потери до адресов назначения релея (см. services/loss_probe)."""

import ipaddress
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# Разовая проверка по запросу панели: 20 попыток — пара секунд, и этого хватает,
# чтобы отличить 40% потерь от одного случайного пропуска
CHECK_ATTEMPTS_DEFAULT = 20
CHECK_ATTEMPTS_MAX = 50


class ProbeStats(BaseModel):
    """Итог окна последних попыток подключения к одному адресу:порту."""
    loss_pct: float
    # Средняя задержка ответивших попыток; None — не ответила ни одна
    rtt_ms: Optional[float] = None
    samples: int
    # Ответил ли сервер на проверку «как клиент» (начало TLS-рукопожатия): подключение
    # проходит и там, где данные не идут. None — проверка не идёт либо подключиться не удалось
    answers: Optional[bool] = None


class LossProbeEntry(ProbeStats):
    """Строка блока `loss_probe` в метриках."""
    ip: str
    port: int
    # Падений сервера HAProxy с этим адресом за полчаса; None — адреса среди
    # серверов HAProxy нет (цель DNAT), и вывести его из балансировки нельзя
    downs: Optional[int] = None
    # Клиенты шлют, а сервер HAProxy с этим адресом не отвечает ни байтом;
    # None — судить не по чему: мало новых сессий либо адреса в HAProxy нет
    stalled: Optional[bool] = None


class WayStats(ProbeStats):
    """Один путь к адресу назначения: с какого своего адреса релей к нему идёт и что там с потерями."""
    # None — адрес по умолчанию, который ядро не назвало
    local_ip: Optional[str] = None
    # Ходит ли этим адресом правило; False — адрес проверяется справочно
    used: bool
    # За сколько секунд набрано окно пути: адреса одной очереди проверяются по одному за раунд
    span_sec: int


class WaysResponse(BaseModel):
    """Разбивка замера по адресам релея — подсказка страницы «Потери»."""
    ip: str
    port: int
    ways: list[WayStats]


class AllWaysResponse(BaseModel):
    """Разбивка по адресам релея для всех его адресов назначения разом — странице HAProxy панели."""
    targets: list[WaysResponse]


class TargetRequest(BaseModel):
    """Адрес:порт, к которому нода подключается по запросу панели."""
    ip: str
    port: int = Field(443, ge=1, le=65535)

    @field_validator("ip")
    @classmethod
    def _unicast_ip(cls, value: str) -> str:
        address = ipaddress.ip_address(value.strip())
        if address.is_unspecified or address.is_multicast:
            raise ValueError("ip must be a unicast address")
        return str(address)


class LossCheckRequest(TargetRequest):
    attempts: int = Field(CHECK_ATTEMPTS_DEFAULT, ge=1, le=CHECK_ATTEMPTS_MAX)
    # Добавить к серии подключений проверку «как клиент» (поле answers ответа)
    client_probe: bool = False
