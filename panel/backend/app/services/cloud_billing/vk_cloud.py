"""VK Cloud: остаток и расход через Billing API.

Статических API-ключей у VK Cloud нет: каждая синхронизация получает токен
Keystone по логину и паролю (почта аккаунта или сервисная учётная запись) с
областью проекта. Billing API адресует проект по PID вида `mcsNNNNNNNNNN` —
это имя проекта в Keystone, поэтому PID берётся из ответа на выдачу токена,
а не спрашивается отдельным полем.

Бонусы VK Cloud списывает раньше денег, поэтому и в остаток, и в расход
входят оба счёта: лицевой и бонусный.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.services.cloud_billing.base import (
    CloudAuthError,
    CloudBillingError,
    CloudProvider,
    CloudSnapshot,
    describe_request_error,
)

logger = logging.getLogger(__name__)

KEYSTONE_TOKENS_URL = "https://infra.mail.ru:5000/v3/auth/tokens"
BILLING_PROJECTS_URL = "https://msk.cloud.vk.ru/billing/public/v1/projects"
# Пользователи VK Cloud и сервисные учётные записи живут в домене Keystone «users»
USER_DOMAIN = "users"

# Москва без перехода на летнее время с 2014 года; zoneinfo в образе может не быть
MOSCOW_TZ = timezone(timedelta(hours=3))
REPORT_TIMEZONE = "Europe/Moscow"
# Расход — среднее за последние закрытые сутки: текущие ещё не досчитаны
CONSUMPTION_WINDOW_DAYS = 3

REQUEST_TIMEOUT = 20.0
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0


class VkCloudProvider(CloudProvider):
    id = "vk_cloud"
    default_currency = "RUB"
    requires_account_id = True
    requires_login = True

    async def fetch(
        self,
        client: httpx.AsyncClient,
        credential: str,
        account_id: Optional[str],
        login: Optional[str] = None,
    ) -> CloudSnapshot:
        if not login:
            raise CloudBillingError("Login is required")
        if not account_id:
            raise CloudBillingError("Project ID is required")

        token, pid = await self._issue_token(client, login, credential, account_id)
        balance = await self._fetch_balance(client, token, pid)
        daily_cost, warning = await self._fetch_daily_cost(client, token, pid)

        return CloudSnapshot(
            balance=balance,
            currency=self.default_currency,
            daily_cost=daily_cost,
            warning=warning,
        )

    async def _issue_token(
        self, client: httpx.AsyncClient, login: str, password: str, project_id: str
    ) -> tuple[str, str]:
        body = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": login,
                            "domain": {"name": USER_DOMAIN},
                            "password": password,
                        }
                    },
                },
                "scope": {"project": {"id": project_id}},
            }
        }
        resp = await self._send(client, "POST", KEYSTONE_TOKENS_URL, json=body)

        if resp.status_code == 401:
            raise CloudAuthError(
                "Auth failed: wrong login or password, or API access is not enabled"
            )
        if resp.status_code == 403:
            raise CloudAuthError(f"Forbidden: no access to project {project_id}")
        if resp.status_code == 404:
            raise CloudBillingError(f"Project {project_id} not found")
        if resp.status_code != 201:
            raise CloudBillingError(f"Keystone HTTP {resp.status_code}: {resp.text[:200]}")

        token = resp.headers.get("X-Subject-Token")
        if not token:
            raise CloudBillingError("Keystone response has no token")
        return token, _project_pid(resp.json())

    async def _fetch_balance(self, client: httpx.AsyncClient, token: str, pid: str) -> float:
        resp = await self._send(
            client, "GET", f"{BILLING_PROJECTS_URL}/{pid}/balances/amount",
            headers={"X-Auth-Token": token},
        )
        data = _billing_payload(resp, pid)
        return _as_number(data.get("base")) + _as_number(data.get("bonus"))

    async def _fetch_daily_cost(
        self, client: httpx.AsyncClient, token: str, pid: str
    ) -> tuple[Optional[float], Optional[str]]:
        """Средний расход в сутки по гранулярному отчёту. Ошибка не фатальна —
        баланс уже получен, без расхода теряется только прогноз."""
        today = datetime.now(MOSCOW_TZ).date()
        params = {
            "date_from": (today - timedelta(days=CONSUMPTION_WINDOW_DAYS)).isoformat(),
            "date_to": (today - timedelta(days=1)).isoformat(),
            "timezone": REPORT_TIMEZONE,
        }
        try:
            resp = await self._send(
                client, "GET", f"{BILLING_PROJECTS_URL}/{pid}/reports/lite-granular",
                headers={"X-Auth-Token": token}, params=params,
            )
            data = _billing_payload(resp, pid)
        except CloudBillingError as e:
            logger.warning("VK Cloud consumption report failed for %s: %s", pid, e)
            return None, f"Consumption report unavailable: {e}"

        return _average_daily_spend(data.get("items")), None

    async def _send(self, client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
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
                logger.warning("VK Cloud %s %s failed: %s", method, url, e)
            else:
                if resp.status_code < 500:
                    return resp
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"

            if attempt < RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))

        raise CloudBillingError(last_error)


def _project_pid(body) -> str:
    project = ((body or {}).get("token") or {}).get("project") or {}
    pid = project.get("name")
    if not pid:
        raise CloudBillingError("Keystone response has no project PID")
    return pid


def _billing_payload(resp: httpx.Response, pid: str) -> dict:
    if resp.status_code == 401:
        raise CloudAuthError("Auth failed: billing rejected the Keystone token")
    if resp.status_code == 403:
        raise CloudAuthError("Forbidden: the user has no access to billing")
    if resp.status_code == 404:
        raise CloudBillingError(f"Project {pid} not found in billing")
    if resp.status_code != 200:
        raise CloudBillingError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    body = resp.json()
    if not isinstance(body, dict):
        raise CloudBillingError("Unexpected response shape")
    return body


def _average_daily_spend(items) -> Optional[float]:
    """Деньги и бонусы, списанные за сутки, в среднем по дням отчёта.

    Делим на число дней, которые есть в отчёте, а не на всё окно: у проекта,
    созданного вчера, деление на три занизило бы расход втрое."""
    if not isinstance(items, list):
        return None

    spent_by_day: dict[str, float] = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("usage_day"):
            continue
        spent = _as_number(item.get("money")) + _as_number(item.get("bonus"))
        spent_by_day[item["usage_day"]] = spent_by_day.get(item["usage_day"], 0.0) + spent

    total = sum(spent_by_day.values())
    if total <= 0:
        return None
    return round(total / len(spent_by_day), 4)


def _as_number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
