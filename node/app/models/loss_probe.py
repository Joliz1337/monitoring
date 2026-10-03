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


class LossProbeEntry(ProbeStats):
    """Строка блока `loss_probe` в метриках."""
    ip: str
    port: int


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
