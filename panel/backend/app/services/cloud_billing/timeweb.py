"""Timeweb Cloud: остаток и текущий тариф через /account/finances.

Публичного API истории списаний у Timeweb нет (PaymentsAPI — только два
snapshot-метода), поэтому фактический расход панель считает сама по снижению
баланса между синхронизациями (`uses_balance_history`, см. __init__.py).
Тариф `hourly_fee × 24` — стартовая оценка, пока истории мало, и запасная,
когда списаний в окне не было.
"""
import asyncio
import logging
from typing import Optional

from app.services.cloud_billing.base import (
    CloudAuthError,
    CloudBillingError,
    CloudProvider,
    CloudSnapshot,
)
from app.services.http_client import get_external_client

logger = logging.getLogger(__name__)

TIMEWEB_BASE = "https://api.timeweb.cloud/api/v1"
FINANCES_PATH = "/account/finances"
REQUEST_TIMEOUT = 20.0
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0
HOURS_PER_DAY = 24
DAYS_PER_MONTH = 30


class TimewebProvider(CloudProvider):
    id = "timeweb"
    default_currency = "RUB"
    requires_account_id = False
    uses_balance_history = True

    async def fetch(self, credential: str, account_id: Optional[str]) -> CloudSnapshot:
        data = await self._get(credential, FINANCES_PATH)

        finances = data.get("finances")
        if not isinstance(finances, dict):
            raise CloudBillingError("Response has no finances")

        # total_balance точнее округлённого balance, но приходит не всегда
        balance = finances.get("total_balance")
        if balance is None:
            balance = finances.get("balance")

        return CloudSnapshot(
            balance=_as_number(balance),
            currency=finances.get("currency") or self.default_currency,
            daily_cost=_tariff_daily_cost(finances),
        )

    async def _get(self, token: str, path: str) -> dict:
        url = f"{TIMEWEB_BASE}{path}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        last_error = "no attempts"

        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = await get_external_client().get(
                    url, headers=headers, timeout=REQUEST_TIMEOUT
                )
            except Exception as e:
                last_error = str(e)
                logger.warning("Timeweb %s request failed: %s", path, e)
            else:
                if resp.status_code == 401:
                    raise CloudAuthError("Auth failed: invalid or revoked API token")
                if resp.status_code == 403:
                    raise CloudAuthError("Forbidden: the token has no access to finances")
                if resp.status_code == 200:
                    body = resp.json()
                    if not isinstance(body, dict):
                        raise CloudBillingError("Unexpected response shape")
                    return body
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code < 500:
                    raise CloudBillingError(last_error)

            if attempt < RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))

        raise CloudBillingError(last_error)


def _tariff_daily_cost(finances: dict) -> Optional[float]:
    """Расход в сутки по текущему тарифу — цена набора ресурсов, а не факт."""
    hourly = _as_number(finances.get("hourly_fee"))
    if hourly > 0:
        return round(hourly * HOURS_PER_DAY, 4)

    monthly = _as_number(finances.get("monthly_fee"))
    if monthly > 0:
        return round(monthly / DAYS_PER_MONTH, 4)

    return None


def _as_number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
