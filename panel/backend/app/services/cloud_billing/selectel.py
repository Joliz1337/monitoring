"""Selectel: остаток и срок жизни баланса через Billing API.

Статический API-ключ (заголовок X-Token) покрывает оба метода. Сколько дней
проживёт баланс, считает сам Selectel (/v2/billing/prediction) — панель эту
цифру не пересчитывает, а только приводит к дневному расходу, чтобы порог
остатка и калькулятор пополнения работали как у других провайдеров.
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

SELECTEL_BASE = "https://api.selectel.ru"
BALANCES_PATH = "/v3/balances"
PREDICTION_PATH = "/v2/billing/prediction"

# Суммы в Billing API приходят целыми числами в минимальных единицах (копейки).
MINOR_UNITS = 100
REQUEST_TIMEOUT = 20.0
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0


class SelectelProvider(CloudProvider):
    id = "selectel"
    default_currency = "RUB"
    requires_account_id = False

    async def fetch(self, credential: str, account_id: Optional[str]) -> CloudSnapshot:
        balance, currency, warning = await self._fetch_balance(credential)
        days_left, prediction_warning = await self._fetch_prediction_days(credential)

        return CloudSnapshot(
            balance=balance,
            currency=currency,
            days_left=days_left,
            # Долг важнее: он объясняет и нулевой остаток, и отсутствие прогноза
            warning=warning or prediction_warning,
        )

    async def _fetch_balance(self, token: str) -> tuple[float, str, Optional[str]]:
        data = await self._get(token, BALANCES_PATH)

        billings = data.get("billings") or []
        total_minor = 0.0
        debt_minor = 0.0
        for billing in billings:
            total_minor += _billing_sum(billing)
            debt_minor += _as_number(billing.get("debt_sum"))

        currency = (data.get("settings") or {}).get("currency") or self.default_currency
        warning = None
        if debt_minor > 0:
            warning = f"Debt: {debt_minor / MINOR_UNITS:.2f} {currency}"

        return total_minor / MINOR_UNITS, currency, warning

    async def _fetch_prediction_days(
        self, token: str
    ) -> tuple[Optional[float], Optional[str]]:
        """Прогноз Selectel: на сколько дней хватит баланса.

        Ошибка не фатальна — баланс уже получен, без прогноза теряется только срок.
        Документация называет значения часами, но на реальном аккаунте число
        сходится с фактическими списаниями только как дни: 46 против посчитанных
        53 дней, тогда как «46 часов» разошлось бы в 28 раз."""
        try:
            data = await self._get(token, PREDICTION_PATH)
        except CloudBillingError as e:
            logger.warning("Selectel prediction unavailable: %s", e)
            return None, f"Prediction unavailable: {e}"

        return _pick_prediction_days(data), None

    async def _get(self, token: str, path: str) -> dict:
        url = f"{SELECTEL_BASE}{path}"
        headers = {"X-Token": token, "Accept": "application/json"}
        last_error = "no attempts"

        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = await get_external_client().get(
                    url, headers=headers, timeout=REQUEST_TIMEOUT
                )
            except Exception as e:
                last_error = str(e)
                logger.warning("Selectel %s request failed: %s", path, e)
            else:
                if resp.status_code == 401:
                    raise CloudAuthError("Auth failed: invalid or revoked API key")
                if resp.status_code == 403:
                    raise CloudAuthError("Forbidden: the key has no access to billing")
                if resp.status_code == 400:
                    raise CloudBillingError(f"Bad request: {resp.text[:200]}")
                if resp.status_code == 200:
                    return _payload(resp.json())
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code < 500:
                    raise CloudBillingError(last_error)

            if attempt < RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))

        raise CloudBillingError(last_error)


def _payload(body) -> dict:
    if not isinstance(body, dict):
        raise CloudBillingError("Unexpected response shape")
    data = body.get("data")
    if not isinstance(data, dict):
        raise CloudBillingError("Response has no data")
    return data


def _as_number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _billing_sum(billing: dict) -> float:
    """Итог по одному биллингу: final_sum, а без него — сумма его балансов."""
    if billing.get("final_sum") is not None:
        return _as_number(billing.get("final_sum"))
    return sum(_as_number(b.get("value")) for b in billing.get("balances") or [])


def _pick_prediction_days(data: dict) -> Optional[float]:
    """Дни жизни баланса. Пустая или нулевая группа значит «услуг нет» или
    «прогноз не считается», поэтому берётся ближайшее исчерпание среди остальных."""
    positive = [_as_number(v) for v in data.values() if _as_number(v) > 0]
    if not positive:
        return None
    return round(min(positive), 1)
