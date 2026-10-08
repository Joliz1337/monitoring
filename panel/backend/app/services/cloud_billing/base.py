"""Общий контракт облачных биллинг-провайдеров.

Провайдер отдаёт только сырой снимок своего аккаунта; пересчёт в срок оплаты,
запись в модель и обработка порога — в sync.py, одинаково для всех провайдеров.
"""
import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Москва без перехода на летнее время с 2014 года; zoneinfo в образе может не быть
MOSCOW_TZ = timezone(timedelta(hours=3))

REQUEST_TIMEOUT = 20.0
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0


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
    # Ключа API нет, вход по логину и паролю: пароль лежит в credential
    requires_login: bool = False
    # API провайдера не даёт текущего расхода (нет истории списаний или только
    # закрытые сутки): расход считается по снижению баланса между
    # синхронизациями, историю снимков ведёт панель
    uses_balance_history: bool = False

    @abstractmethod
    async def fetch(
        self,
        client: httpx.AsyncClient,
        credential: str,
        account_id: Optional[str],
        login: Optional[str] = None,
    ) -> CloudSnapshot:
        """Снимок аккаунта. Бросает CloudBillingError при любой неудаче.

        Все запросы — через переданный клиент: в нём уже выбран прокси проекта."""


def describe_request_error(error: Exception) -> str:
    # Оборванный SOCKS-туннель даёт ConnectError('') — без имени типа
    # в cloud_last_error осталась бы пустая строка
    return str(error) or type(error).__name__


async def send_with_retry(
    client: httpx.AsyncClient, method: str, url: str, provider_name: str, **kwargs
) -> httpx.Response:
    """Запрос с повтором при сетевой ошибке и 5xx; остальные коды разбирает вызывающий."""
    headers = {"Accept": "application/json", **kwargs.pop("headers", {})}
    last_error = "no attempts"

    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = await client.request(
                method, url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs
            )
        except Exception as e:
            last_error = describe_request_error(e)
            logger.warning("%s %s %s failed: %s", provider_name, method, url, e)
        else:
            if resp.status_code < 500:
                return resp
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"

        if attempt < RETRY_ATTEMPTS - 1:
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))

    raise CloudBillingError(last_error)


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
