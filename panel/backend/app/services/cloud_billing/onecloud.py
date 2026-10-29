"""1cloud: остаток и срок жизни баланса проекта через GET /project.

Учётка — персональный API-ключ из «Настройки → API-ключ» (заголовок Bearer).
Дату, до которой баланс останется положительным (`BalanceTillDateUtc`), считает
сам 1cloud — панель переводит её в дни, а дни в дневной расход, как прогноз
Selectel, чтобы порог остатка и калькулятор пополнения работали одинаково.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.services.cloud_billing.base import (
    CloudAuthError,
    CloudBillingError,
    CloudProvider,
    CloudSnapshot,
    send_with_retry,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "1cloud"
PROJECT_URL = "https://api.1cloud.ru/project"

# Прогноз BalanceTillDateUtc дан на проект целиком, без разбивки по видам средств,
# поэтому остаток — их сумма: расход из прогноза выводится по ней же
BALANCE_PARTS = ("Real", "Bonus", "Test")
# Дата дальше десяти лет — не прогноз, а проект без расходов; срок от неё
# вышел бы за пределы datetime при пересчёте в paid_until
MAX_FORECAST_DAYS = 3650
SECONDS_PER_DAY = 86400


class OneCloudProvider(CloudProvider):
    id = "onecloud"
    default_currency = "RUB"

    async def fetch(
        self,
        client: httpx.AsyncClient,
        credential: str,
        account_id: Optional[str],
        login: Optional[str] = None,
    ) -> CloudSnapshot:
        resp = await send_with_retry(
            client, "GET", PROJECT_URL, PROVIDER_NAME,
            headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"},
        )
        if resp.status_code == 401:
            raise CloudAuthError("Auth failed: invalid API key or a new key was generated")
        if resp.status_code == 403:
            raise CloudAuthError("Forbidden: the key has no access to the project")
        if resp.status_code != 200:
            raise CloudBillingError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        project = _json_object(resp)
        days_left, warning = _forecast_days(
            project.get("BalanceTillDateUtc"), datetime.now(timezone.utc)
        )
        return CloudSnapshot(
            balance=_project_balance(project),
            currency=self.default_currency,
            days_left=days_left,
            warning=warning,
        )


def _json_object(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        raise CloudBillingError("Response is not JSON") from None
    if not isinstance(body, dict):
        raise CloudBillingError("Unexpected response shape")
    return body


def _project_balance(project: dict) -> float:
    parts = project.get("Balances")
    if isinstance(parts, dict):
        return round(sum(_as_number(parts.get(name)) for name in BALANCE_PARTS), 2)
    # Общее поле Balance 1cloud объявил устаревшим — только на случай ответа без Balances
    if project.get("Balance") is not None:
        return _as_number(project["Balance"])
    raise CloudBillingError("Project response has no balance")


def _as_number(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        raise CloudBillingError(f"Unexpected balance value: {value!r}") from None


def _forecast_days(raw, now: datetime) -> tuple[Optional[float], Optional[str]]:
    """Дни до BalanceTillDateUtc. Пустая дата — расходов нет, срок неизвестен.
    Непонятная дата не фатальна: баланс уже получен, теряется только срок."""
    if not raw:
        return None, None
    try:
        till = datetime.fromisoformat(str(raw))
    except ValueError:
        logger.warning("1cloud returned unexpected BalanceTillDateUtc: %r", raw)
        return None, f"Forecast unavailable: unexpected BalanceTillDateUtc {raw!r}"
    if till.tzinfo is None:
        till = till.replace(tzinfo=timezone.utc)

    days = (till - now).total_seconds() / SECONDS_PER_DAY
    if days > MAX_FORECAST_DAYS:
        return None, None
    return round(days, 1), None
