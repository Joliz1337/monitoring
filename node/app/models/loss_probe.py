"""Потери до адресов назначения релея (см. services/loss_probe)."""

from typing import Optional

from pydantic import BaseModel


class ProbeStats(BaseModel):
    """Итог окна последних попыток подключения к одному адресу:порту."""
    loss_pct: float
    # Средняя задержка ответивших попыток; None — не ответила ни одна
    rtt_ms: Optional[float] = None
    samples: int
