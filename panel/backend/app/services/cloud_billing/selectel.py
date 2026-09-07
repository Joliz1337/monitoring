"""Selectel: остаток, расход и запасной прогноз через Billing API.

Статический API-ключ (заголовок X-Token) покрывает все три метода. Расход
берётся готовым из статистики биллинга — той же, что Selectel показывает в
своём разделе «Статистика», — за то же окно, что у Yandex Cloud. Прогноз
самого Selectel остаётся запасным вариантом, потому что его единица измерения
расходится с документацией (см. _fetch_prediction_days).
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

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
SUMMARY_STATS_PATH = "/v1/cloud_billing/statistic/summary_stats"

# Статистика отдаётся только по перечисленным продуктам, а нужен расход по
# аккаунту целиком — поэтому перечислены все ключи из спецификации API
PROVIDER_KEYS = (
    "vpc", "serverless", "mks", "dbaas", "storage", "cdn",
    "vmware", "craas", "ones", "mobfarm", "ses",
)

# Суммы в Billing API приходят целыми числами в минимальных единицах (копейки).
MINOR_UNITS = 100
# Столько же, сколько у Yandex Cloud: свежая оценка важнее сглаживания
CONSUMPTION_WINDOW_DAYS = 3
REQUEST_TIMEOUT = 20.0
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0


class SelectelProvider(CloudProvider):
    id = "selectel"
    default_currency = "RUB"
    requires_account_id = False

    async def fetch(self, credential: str, account_id: Optional[str]) -> CloudSnapshot:
        balance, currency, warning = await self._fetch_balance(credential)
        daily_cost = await self._fetch_daily_cost(credential)
        days_left = None if daily_cost else await self._fetch_prediction_days(credential)

        return CloudSnapshot(
            balance=balance,
            currency=currency,
            daily_cost=daily_cost,
            days_left=days_left,
            warning=warning,
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

    async def _fetch_daily_cost(self, token: str) -> Optional[float]:
        """Средний расход в сутки по статистике биллинга за последние дни.

        Ошибка не фатальна: баланс уже получен, срок посчитается по прогнозу."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=CONSUMPTION_WINDOW_DAYS)

        try:
            spent_minor = await self._fetch_spent(token, start, now)
        except CloudBillingError as e:
            logger.warning("Selectel billing statistics unavailable: %s", e)
            return None

        if spent_minor <= 0:
            return None

        return round(spent_minor / MINOR_UNITS / CONSUMPTION_WINDOW_DAYS, 4)

    async def _fetch_spent(self, token: str, start: datetime, end: datetime) -> float:
        """Расход за период в копейках, посчитанный самим Selectel."""
        query = "?" + urlencode(
            {
                "provider_keys": PROVIDER_KEYS,
                "start": f"{start:%Y-%m-%dT%H:%M:%S}",
                "end": f"{end:%Y-%m-%dT%H:%M:%S}",
                "group_type": "project",
                "period_group_type": "all",
                "items_count_period_group_type": "all",
            },
            doseq=True,
        )
        data = await self._get(token, SUMMARY_STATS_PATH, query)
        return sum(_as_number(row.get("value")) for row in _statistic_rows(data))

    async def _fetch_prediction_days(self, token: str) -> Optional[float]:
        """Запасной прогноз, когда расхода в окне нет (свежий аккаунт).

        Документация Selectel называет значения часами, но на реальном аккаунте
        число совпадает с расчётом по списаниям только как дни: 46 против
        посчитанных 53 дней, тогда как «46 часов» разошлось бы в 28 раз."""
        try:
            data = await self._get(token, PREDICTION_PATH)
        except CloudBillingError as e:
            logger.warning("Selectel prediction unavailable: %s", e)
            return None

        return _pick_prediction_days(data)

    async def _get(self, token: str, path: str, query: str = "") -> dict | list:
        url = f"{SELECTEL_BASE}{path}{query}"
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


def _payload(body: dict) -> dict | list:
    if not isinstance(body, dict):
        raise CloudBillingError("Unexpected response shape")
    data = body.get("data")
    if data is None:
        raise CloudBillingError("Response has no data")
    return data


def _statistic_rows(data) -> list[dict]:
    """Сводка расхода — массив строк: по одной на период и группу."""
    if not isinstance(data, list):
        raise CloudBillingError("Unexpected statistics shape")
    return [row for row in data if isinstance(row, dict)]


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
    if not isinstance(data, dict):
        return None
    positive = [_as_number(v) for v in data.values() if _as_number(v) > 0]
    if not positive:
        return None
    return round(min(positive), 1)
