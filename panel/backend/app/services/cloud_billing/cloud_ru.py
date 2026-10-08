"""Cloud.ru Evolution: остаток и расход по ключу доступа сервисного аккаунта.

Ключ (Key ID и Key Secret) каждая синхронизация меняет в IAM на токен на час.
Сервисному аккаунту нужна роль «Администратор затрат» в организации.

Метода баланса в публичном API Cloud.ru нет: «Контроль затрат» отдаёт только
потребление. Остаток панель берёт там же, где его показывает личный кабинет, —
из служебного API кабинета (`bff-console`). В документации этого адреса нет,
но токен сервисного аккаунта он принимает.

Список договоров сервисному аккаунту недоступен — API отдаёт его только
пользователю, поэтому ID договора вводится в форме.

Расход панель считает по снижению баланса между синхронизациями
(`uses_balance_history`, см. __init__.py). Пока истории мало, стартовая
оценка — потребление с НДС за вчерашние сутки по Москве. Строки потребления —
мелкие кванты (секунды аренды IP), при частой смене адресов их тысячи в сутки,
поэтому выборка постраничная с потолком, а итог кэшируется.
"""
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import quote

import httpx

from app.services.cloud_billing.base import (
    MOSCOW_TZ,
    CloudAuthError,
    CloudBillingError,
    CloudProvider,
    CloudSnapshot,
    send_with_retry,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "Cloud.ru"
IAM_TOKEN_URL = "https://iam.api.cloud.ru/api/v1/auth/token"
CONSOLE_AGREEMENTS_URL = "https://console.cloud.ru/u-api/bff-console/v2/agreements"
CONSUMPTION_URL = "https://organization.api.cloud.ru/v2/consumption"

CONSUMPTION_PAGE_SIZE = 5000
# Сутки длиннее 50 тысяч строк выкачивать ради стартовой оценки незачем:
# через 6 часов расход даст история баланса
CONSUMPTION_MAX_PAGES = 10
# Закрытые сутки Cloud.ru досчитывает задним числом (пороговое потребление,
# смена тарифа), поэтому итог живёт ограниченное время
CONSUMPTION_CACHE_TTL = 6 * 3600.0

AUTH_REJECTED_CODES = (400, 401, 403, 404)


@dataclass(slots=True)
class _CachedCost:
    day: date
    fetched_at: float
    daily_cost: Optional[float]


class CloudRuProvider(CloudProvider):
    id = "cloud_ru"
    default_currency = "RUB"
    requires_account_id = True
    requires_login = True
    uses_balance_history = True

    def __init__(self) -> None:
        self._consumption_cache: dict[str, _CachedCost] = {}

    async def fetch(
        self,
        client: httpx.AsyncClient,
        credential: str,
        account_id: Optional[str],
        login: Optional[str] = None,
    ) -> CloudSnapshot:
        if not login:
            raise CloudBillingError("Key ID is required")
        if not account_id:
            raise CloudBillingError("Agreement ID is required")

        token = await self._issue_token(client, login, credential)
        balance = await self._fetch_balance(client, token, account_id)
        daily_cost, warning = await self._daily_cost(client, token, account_id)

        return CloudSnapshot(
            balance=balance,
            currency=self.default_currency,
            daily_cost=daily_cost,
            warning=warning,
        )

    async def _issue_token(self, client: httpx.AsyncClient, key_id: str, key_secret: str) -> str:
        resp = await send_with_retry(
            client, "POST", IAM_TOKEN_URL, PROVIDER_NAME,
            json={"keyId": key_id, "secret": key_secret},
        )
        if resp.status_code in AUTH_REJECTED_CODES:
            raise CloudAuthError(
                f"Auth failed: wrong Key ID or Key Secret, or the key is disabled "
                f"(HTTP {resp.status_code})"
            )
        if resp.status_code != 200:
            raise CloudBillingError(f"IAM HTTP {resp.status_code}: {resp.text[:200]}")

        token = _json_object(resp).get("access_token")
        if not token:
            raise CloudBillingError("IAM response has no access_token")
        return token

    async def _fetch_balance(self, client: httpx.AsyncClient, token: str, agreement_id: str) -> float:
        resp = await send_with_retry(
            client, "GET", f"{CONSOLE_AGREEMENTS_URL}/{quote(agreement_id, safe='')}/balance",
            PROVIDER_NAME, headers=_bearer(token),
        )
        _raise_for_status(resp, agreement_id)

        try:
            return float(_json_object(resp)["balance"])
        except (KeyError, TypeError, ValueError):
            raise CloudBillingError("Balance response has no balance") from None

    async def _daily_cost(
        self, client: httpx.AsyncClient, token: str, agreement_id: str
    ) -> tuple[Optional[float], Optional[str]]:
        """Потребление с НДС за вчерашние сутки. Ошибка не фатальна — баланс уже
        получен, а прогноз появится, когда наберётся история баланса."""
        day = datetime.now(MOSCOW_TZ).date() - timedelta(days=1)
        cached = self._consumption_cache.get(agreement_id)
        if (
            cached is not None
            and cached.day == day
            and time.monotonic() - cached.fetched_at < CONSUMPTION_CACHE_TTL
        ):
            return cached.daily_cost, None

        try:
            daily_cost = await self._fetch_day_consumption(client, token, agreement_id, day)
        except CloudBillingError as e:
            logger.warning("Cloud.ru consumption failed for %s: %s", agreement_id, e)
            return None, f"Consumption unavailable: {e}"

        self._consumption_cache[agreement_id] = _CachedCost(day, time.monotonic(), daily_cost)
        return daily_cost, None

    async def _fetch_day_consumption(
        self, client: httpx.AsyncClient, token: str, agreement_id: str, day: date
    ) -> Optional[float]:
        params = {
            "agreement_id": agreement_id,
            "start_date": f"{day.isoformat()}T00:00:00Z",
            "end_date": f"{day.isoformat()}T23:59:59Z",
            "page_filter.page_size": CONSUMPTION_PAGE_SIZE,
        }
        spent = 0.0

        for _ in range(CONSUMPTION_MAX_PAGES):
            resp = await send_with_retry(
                client, "GET", CONSUMPTION_URL, PROVIDER_NAME,
                headers=_bearer(token), params=params,
            )
            _raise_for_status(resp, agreement_id)
            body = _json_object(resp)
            spent += _spent_with_vat(body.get("consumptions"))

            next_page = body.get("next_page_token")
            if not next_page:
                return round(spent, 4) if spent > 0 else None
            params["page_filter.page_token"] = next_page

        logger.info(
            "Cloud.ru consumption of %s for %s exceeds %d pages, waiting for balance history",
            agreement_id, day, CONSUMPTION_MAX_PAGES,
        )
        return None


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _raise_for_status(resp: httpx.Response, agreement_id: str) -> None:
    if resp.status_code == 401:
        raise CloudAuthError("Auth failed: Cloud.ru rejected the token")
    if resp.status_code == 403:
        raise CloudAuthError(
            "Forbidden: check the agreement ID and that the service account has "
            "the Cost administrator role in the organization"
        )
    if resp.status_code == 404:
        raise CloudBillingError(f"Agreement {agreement_id} not found")
    if resp.status_code != 200:
        raise CloudBillingError(f"HTTP {resp.status_code}: {resp.text[:200]}")


def _json_object(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        raise CloudBillingError("Response is not JSON") from None
    if not isinstance(body, dict):
        raise CloudBillingError("Unexpected response shape")
    return body


def _spent_with_vat(rows) -> float:
    """Сумма с НДС — с баланса списывается она. Строки с is_delete Cloud.ru
    просит считать удалёнными в источнике и не учитывать."""
    if not isinstance(rows, list):
        return 0.0

    spent = 0.0
    for row in rows:
        if not isinstance(row, dict) or row.get("is_delete"):
            continue
        try:
            spent += float(row.get("amount_nds") or 0)
        except (TypeError, ValueError):
            continue
    return spent
