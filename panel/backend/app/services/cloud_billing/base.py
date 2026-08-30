"""Общий контракт облачных биллинг-провайдеров.

Провайдер отдаёт только сырой снимок своего аккаунта; пересчёт в срок оплаты,
запись в модель и обработка порога — в sync.py, одинаково для всех провайдеров.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


class CloudBillingError(Exception):
    """Провайдер не отдал данные: сеть, неверный ответ, отказ сервиса."""


class CloudAuthError(CloudBillingError):
    """Токен не принят или у него нет прав на биллинг."""


@dataclass(slots=True)
class CloudSnapshot:
    """Снимок аккаунта: остаток и всё, из чего считается срок жизни баланса."""

    balance: float
    currency: str
    daily_cost: Optional[float] = None
    days_left: Optional[float] = None
    warning: Optional[str] = None


class CloudProvider(ABC):
    id: str
    default_currency: str
    requires_account_id: bool = False

    @abstractmethod
    async def fetch(self, credential: str, account_id: Optional[str]) -> CloudSnapshot:
        """Снимок аккаунта. Бросает CloudBillingError при любой неудаче."""


def compute_days_left(
    balance: float,
    threshold: float,
    daily_cost: Optional[float],
) -> Optional[float]:
    """Сколько дней проживёт остаток над порогом при текущем дневном расходе."""
    if daily_cost is None or daily_cost <= 0:
        return None

    usable = balance - threshold
    if usable <= 0:
        return 0.0

    return round(usable / daily_cost, 1)
