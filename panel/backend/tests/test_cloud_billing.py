import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.cloud_billing import (  # noqa: E402
    CloudAuthError,
    CloudBillingError,
    _apply_snapshot,
    get_provider,
    sync_cloud_balance,
)
from app.services.cloud_billing.base import CloudSnapshot, compute_days_left  # noqa: E402
from app.services.cloud_billing import (  # noqa: E402
    HISTORY_WINDOW_DAYS,
    _balance_history_daily_cost,
)
from app.services.cloud_billing.selectel import (  # noqa: E402
    SelectelProvider,
    _billing_sum,
    _payload,
    _pick_prediction_days,
)
from app.services.cloud_billing.timeweb import TimewebProvider, _tariff_daily_cost  # noqa: E402
from app.services.cloud_billing.vk_cloud import VkCloudProvider, _average_daily_spend  # noqa: E402
from app.services.cloud_billing.yandex import (  # noqa: E402
    YC_USAGE_URL,
    YandexCloudProvider,
    _grpc_frame,
    _grpc_unframe,
    _pb_string,
    _pb_submessage,
)
from app.services.http_client import close_http_clients, get_external_client  # noqa: E402
from app.services.yc_token_manager import YC_IAM_ENDPOINT, get_yc_token_manager  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakeClient:
    """Отдаёт заранее заданные ответы по пути запроса (query отбрасывается)."""

    def __init__(self, by_path: dict, base: str = "https://api.selectel.ru"):
        self.by_path = by_path
        self.base = base
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, headers=None, timeout=None):
        target = url.replace(self.base, "")
        path = target.split("?", 1)[0]
        self.calls.append((target, headers or {}))
        response = self.by_path[path]
        if isinstance(response, Exception):
            raise response
        return response


def balances_response(final_sum: float, debt_sum: float = 0, currency: str = "RUB"):
    return FakeResponse(200, {
        "status": "success",
        "data": {
            "settings": {"currency": currency},
            "billings": [{"final_sum": final_sum, "debt_sum": debt_sum}],
        },
    })


def prediction_response(**groups):
    """Форма ответа /v2/billing/prediction: дни по группам балансов."""
    return FakeResponse(200, {"status": "success", "data": groups})


def billing_server(**overrides):
    server = SimpleNamespace(
        name="cloud-1",
        billing_type="cloud",
        cloud_provider="selectel",
        cloud_credential="token",
        cloud_account_id=None,
        cloud_login=None,
        cloud_proxy=None,
        cloud_balance_threshold=0,
        cloud_daily_cost=None,
        cloud_last_sync_at=None,
        cloud_last_error=None,
        cloud_balance_history=None,
        account_balance=None,
        balance_updated_at=None,
        currency="RUB",
        monthly_cost=None,
        paid_until=None,
    )
    for key, value in overrides.items():
        setattr(server, key, value)
    return server


class DaysLeftTests(unittest.TestCase):
    def test_remaining_days_over_threshold(self):
        self.assertEqual(compute_days_left(1000.0, 200.0, 40.0), 20.0)

    def test_balance_below_threshold_is_zero(self):
        self.assertEqual(compute_days_left(100.0, 200.0, 40.0), 0.0)

    def test_without_daily_cost_there_is_no_forecast(self):
        self.assertIsNone(compute_days_left(1000.0, 0.0, None))
        self.assertIsNone(compute_days_left(1000.0, 0.0, 0.0))


class SelectelParsingTests(unittest.TestCase):
    def test_payload_requires_data_object(self):
        self.assertEqual(_payload({"status": "success", "data": {"a": 1}}), {"a": 1})
        with self.assertRaises(CloudBillingError):
            _payload({"status": "error"})

    def test_billing_sum_prefers_final_sum(self):
        self.assertEqual(
            _billing_sum({"final_sum": 210000, "balances": [{"value": 1}]}), 210000
        )

    def test_billing_sum_falls_back_to_balances(self):
        self.assertEqual(
            _billing_sum({"balances": [{"value": 1000}, {"value": 500}]}), 1500
        )

    def test_prediction_picks_nearest_non_zero_group(self):
        self.assertEqual(
            _pick_prediction_days({"primary": 240, "storage": 0, "vpc": 96}), 96
        )

    def test_prediction_ignores_empty_groups(self):
        # Реальный ответ аккаунта: считаются только заполненные группы
        self.assertEqual(
            _pick_prediction_days(
                {"primary": 46, "storage": None, "vmware": None, "vpc": None}
            ),
            46,
        )

    def test_prediction_all_zero_means_no_forecast(self):
        self.assertIsNone(_pick_prediction_days({"primary": 0, "storage": None}))


class SelectelProviderTests(unittest.TestCase):
    def _fetch(self, client):
        return asyncio.run(SelectelProvider().fetch(client, "static-token", None))

    def test_balance_and_forecast_come_from_selectel(self):
        # Форма реального ответа аккаунта: остаток 17 824,41 ₽, хватит на 46 дней
        client = FakeClient({
            "/v3/balances": balances_response(1782441),
            "/v2/billing/prediction": prediction_response(
                primary=46, storage=None, vmware=None, vpc=None
            ),
        })

        snapshot = self._fetch(client)

        self.assertEqual(snapshot.balance, 17824.41)
        self.assertEqual(snapshot.currency, "RUB")
        self.assertEqual(snapshot.days_left, 46)
        # Расход панель не считает — он выводится из прогноза в _apply_snapshot
        self.assertIsNone(snapshot.daily_cost)
        self.assertIsNone(snapshot.warning)
        self.assertEqual(client.calls[0][1]["X-Token"], "static-token")
        self.assertEqual(
            [c[0] for c in client.calls], ["/v3/balances", "/v2/billing/prediction"]
        )

    def test_forecast_turns_into_term_and_daily_cost(self):
        server = billing_server()
        now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
        client = FakeClient({
            "/v3/balances": balances_response(1782441),
            "/v2/billing/prediction": prediction_response(primary=46),
        })

        with patch("app.services.cloud_billing.get_external_client", return_value=client):
            asyncio.run(sync_cloud_balance(server, now))

        # Срок в панели — ровно прогноз Selectel, расход в день выведен из него
        self.assertEqual(server.paid_until, now + timedelta(days=46))
        self.assertEqual(server.cloud_daily_cost, round(17824.41 / 46, 4))

    def test_debt_is_reported_as_warning(self):
        client = FakeClient({
            "/v3/balances": balances_response(0, debt_sum=50000),
            "/v2/billing/prediction": FakeResponse(400, None, "bad request"),
        })

        snapshot = self._fetch(client)

        self.assertEqual(snapshot.balance, 0.0)
        self.assertIsNone(snapshot.days_left)
        # Долг объясняет и пустой прогноз, поэтому вытесняет его ошибку
        self.assertIn("Debt: 500.00", snapshot.warning)

    def test_bad_token_raises_auth_error(self):
        client = FakeClient({"/v3/balances": FakeResponse(401, None, "unauthorized")})
        with self.assertRaises(CloudAuthError):
            self._fetch(client)

    def test_prediction_failure_keeps_balance(self):
        client = FakeClient({
            "/v3/balances": balances_response(100000),
            "/v2/billing/prediction": FakeResponse(400, None, "bad request"),
        })

        snapshot = self._fetch(client)

        self.assertEqual(snapshot.balance, 1000.0)
        self.assertIsNone(snapshot.days_left)
        self.assertIn("Prediction unavailable", snapshot.warning)

    def test_empty_prediction_means_unknown_term(self):
        client = FakeClient({
            "/v3/balances": balances_response(100000),
            "/v2/billing/prediction": prediction_response(primary=0, storage=None),
        })

        snapshot = self._fetch(client)

        self.assertIsNone(snapshot.days_left)
        self.assertIsNone(snapshot.warning)


def finances_response(**finances):
    return FakeResponse(200, {"finances": finances})


class TimewebProviderTests(unittest.TestCase):
    def _fetch(self, client):
        return asyncio.run(TimewebProvider().fetch(client, "bearer-token", None))

    def test_balance_and_tariff_estimate(self):
        # Форма реального ответа: /account/finances → finances
        client = FakeClient({
            "/account/finances": finances_response(
                balance=8.24, total_balance=8.24454143, currency="RUB",
                hourly_fee=0.41, monthly_fee=300, hours_left=20,
            ),
        }, base="https://api.timeweb.cloud/api/v1")

        snapshot = self._fetch(client)

        self.assertEqual(snapshot.balance, 8.24454143)
        self.assertEqual(snapshot.currency, "RUB")
        self.assertEqual(snapshot.daily_cost, round(0.41 * 24, 4))
        self.assertEqual(client.calls[0][1]["Authorization"], "Bearer bearer-token")

    def test_monthly_fee_backs_up_missing_hourly(self):
        self.assertEqual(_tariff_daily_cost({"hourly_fee": 0, "monthly_fee": 300}), 10.0)
        self.assertIsNone(_tariff_daily_cost({"hourly_fee": 0, "monthly_fee": 0}))

    def test_rounded_balance_is_the_fallback(self):
        client = FakeClient({
            "/account/finances": finances_response(balance=8.24, hourly_fee=0.41),
        }, base="https://api.timeweb.cloud/api/v1")

        self.assertEqual(self._fetch(client).balance, 8.24)

    def test_bad_token_raises_auth_error(self):
        client = FakeClient(
            {"/account/finances": FakeResponse(401, None, "unauthorized")},
            base="https://api.timeweb.cloud/api/v1",
        )
        with self.assertRaises(CloudAuthError):
            self._fetch(client)

    def test_missing_finances_is_an_error(self):
        client = FakeClient(
            {"/account/finances": FakeResponse(200, {"status": "ok"})},
            base="https://api.timeweb.cloud/api/v1",
        )
        with self.assertRaises(CloudBillingError):
            self._fetch(client)


class FakeVkResponse(FakeResponse):
    def __init__(self, status_code: int, payload=None, text: str = "", headers: dict | None = None):
        super().__init__(status_code, payload, text)
        self.headers = headers or {}


def keystone_token_response(pid: str = "mcs0123456789", token: str = "gAAAA-token"):
    return FakeVkResponse(
        201,
        {"token": {"project": {"id": "b5b7ffd4ef05", "name": pid}}},
        headers={"X-Subject-Token": token},
    )


def report_item(day: str, money: str, bonus: str = "0"):
    return {"pid": "mcs0123456789", "usage_day": day, "resource_price": "1", "money": money,
            "bonus": bonus, "labels": {}}


class FakeVkClient:
    """Ответы по (метод, путь); каждый запрос записывается с телом и параметрами."""

    def __init__(self, by_route: dict):
        self.by_route = by_route
        self.calls: list[dict] = []

    async def request(self, method, url, headers=None, timeout=None, **kwargs):
        path = url.split("://", 1)[1].split("/", 1)[1]
        self.calls.append({"method": method, "path": path, "headers": headers or {}, **kwargs})
        response = self.by_route[(method, "/" + path)]
        if isinstance(response, Exception):
            raise response
        return response


VK_BALANCE = ("GET", "/billing/public/v1/projects/mcs0123456789/balances/amount")
VK_REPORT = ("GET", "/billing/public/v1/projects/mcs0123456789/reports/lite-granular")
VK_TOKEN = ("POST", "/v3/auth/tokens")


class VkCloudProviderTests(unittest.TestCase):
    def _fetch(self, client, login="user@example.com", account_id="b5b7ffd4ef05"):
        return asyncio.run(VkCloudProvider().fetch(client, "secret", account_id, login))

    def test_balance_sums_personal_and_bonus_accounts(self):
        client = FakeVkClient({
            VK_TOKEN: keystone_token_response(),
            VK_BALANCE: FakeVkResponse(200, {"base": 1200.5, "bonus": 300}),
            VK_REPORT: FakeVkResponse(200, {"items": []}),
        })

        snapshot = self._fetch(client)

        self.assertEqual(snapshot.balance, 1500.5)
        self.assertEqual(snapshot.currency, "RUB")
        self.assertIsNone(snapshot.daily_cost)

    def test_token_is_scoped_to_the_project_and_reused_for_billing(self):
        client = FakeVkClient({
            VK_TOKEN: keystone_token_response(token="tok-1"),
            VK_BALANCE: FakeVkResponse(200, {"base": 10, "bonus": 0}),
            VK_REPORT: FakeVkResponse(200, {"items": []}),
        })

        self._fetch(client)

        auth = client.calls[0]["json"]["auth"]
        self.assertEqual(auth["identity"]["password"]["user"]["name"], "user@example.com")
        self.assertEqual(auth["identity"]["password"]["user"]["domain"]["name"], "users")
        self.assertEqual(auth["scope"]["project"]["id"], "b5b7ffd4ef05")
        self.assertEqual(client.calls[1]["headers"]["X-Auth-Token"], "tok-1")

    def test_pid_for_billing_comes_from_keystone_project_name(self):
        client = FakeVkClient({
            VK_TOKEN: keystone_token_response(pid="mcs9999999999"),
            ("GET", "/billing/public/v1/projects/mcs9999999999/balances/amount"):
                FakeVkResponse(200, {"base": 1, "bonus": 0}),
            ("GET", "/billing/public/v1/projects/mcs9999999999/reports/lite-granular"):
                FakeVkResponse(200, {"items": []}),
        })

        self.assertEqual(self._fetch(client).balance, 1)

    def test_daily_cost_averages_money_and_bonus_over_reported_days(self):
        client = FakeVkClient({
            VK_TOKEN: keystone_token_response(),
            VK_BALANCE: FakeVkResponse(200, {"base": 900, "bonus": 0}),
            VK_REPORT: FakeVkResponse(200, {"items": [
                report_item("2026-10-01", "40.5", "9.5"),
                report_item("2026-10-01", "10"),
                report_item("2026-10-02", "50"),
            ]}),
        })

        snapshot = self._fetch(client)

        self.assertEqual(snapshot.daily_cost, 55.0)
        params = client.calls[2]["params"]
        self.assertEqual(params["timezone"], "Europe/Moscow")
        self.assertLess(params["date_from"], params["date_to"])

    def test_report_failure_keeps_the_balance(self):
        client = FakeVkClient({
            VK_TOKEN: keystone_token_response(),
            VK_BALANCE: FakeVkResponse(200, {"base": 100, "bonus": 0}),
            VK_REPORT: FakeVkResponse(422, None, "bad range"),
        })

        snapshot = self._fetch(client)

        self.assertEqual(snapshot.balance, 100)
        self.assertIsNone(snapshot.daily_cost)
        self.assertIn("Consumption report unavailable", snapshot.warning)

    def test_wrong_password_raises_auth_error(self):
        client = FakeVkClient({VK_TOKEN: FakeVkResponse(401, None, "unauthorized")})
        with self.assertRaises(CloudAuthError):
            self._fetch(client)

    def test_billing_forbidden_raises_auth_error(self):
        client = FakeVkClient({
            VK_TOKEN: keystone_token_response(),
            VK_BALANCE: FakeVkResponse(403, None, "forbidden"),
        })
        with self.assertRaises(CloudAuthError):
            self._fetch(client)

    def test_login_and_project_are_required(self):
        with self.assertRaises(CloudBillingError):
            self._fetch(FakeVkClient({}), login=None)
        with self.assertRaises(CloudBillingError):
            self._fetch(FakeVkClient({}), account_id=None)

    def test_average_ignores_malformed_rows(self):
        self.assertIsNone(_average_daily_spend(None))
        self.assertIsNone(_average_daily_spend([{"money": "5"}]))
        self.assertEqual(_average_daily_spend([report_item("2026-10-01", "7", "bad")]), 7.0)


class BalanceHistoryTests(unittest.TestCase):
    """Расход по снимкам баланса — для провайдеров без API истории списаний."""

    def setUp(self):
        self.now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)

    def _history(self, hours_and_balances):
        return json.dumps([
            [(self.now - timedelta(hours=h)).isoformat(), b]
            for h, b in hours_and_balances
        ])

    def test_first_sync_gives_no_cost_but_stores_the_point(self):
        server = billing_server()
        self.assertIsNone(_balance_history_daily_cost(server, 1000.0, self.now))
        self.assertEqual(json.loads(server.cloud_balance_history), [[self.now.isoformat(), 1000.0]])

    def test_cost_from_steady_decline(self):
        # 24 часа истории, -10 в час → 240 в сутки
        server = billing_server(cloud_balance_history=self._history([(24, 1240), (12, 1120)]))
        self.assertEqual(_balance_history_daily_cost(server, 1000.0, self.now), 240.0)

    def test_topup_interval_is_discarded(self):
        # Между -12ч и -6ч баланс вырос (пополнение) — интервал не считается,
        # расход берётся из оставшихся 18 часов: 300 / 18ч → 400 в сутки
        server = billing_server(cloud_balance_history=self._history([
            (24, 1200), (12, 1000), (6, 5000),
        ]))
        self.assertEqual(_balance_history_daily_cost(server, 4900.0, self.now), 400.0)

    def test_short_history_is_not_trusted(self):
        server = billing_server(cloud_balance_history=self._history([(2, 1020)]))
        self.assertIsNone(_balance_history_daily_cost(server, 1000.0, self.now))

    def test_frequent_syncs_move_the_last_point(self):
        server = billing_server(cloud_balance_history=self._history([(12, 1120), (0.1, 1001)]))
        _balance_history_daily_cost(server, 1000.0, self.now)

        points = json.loads(server.cloud_balance_history)
        self.assertEqual(len(points), 2)
        self.assertEqual(points[-1], [self.now.isoformat(), 1000.0])

    def test_old_points_are_pruned(self):
        stale_hours = HISTORY_WINDOW_DAYS * 24 + 1
        server = billing_server(cloud_balance_history=self._history([
            (stale_hours, 9999), (12, 1120),
        ]))
        _balance_history_daily_cost(server, 1000.0, self.now)
        self.assertEqual(len(json.loads(server.cloud_balance_history)), 2)

    def test_garbage_history_resets_cleanly(self):
        server = billing_server(cloud_balance_history="not json")
        self.assertIsNone(_balance_history_daily_cost(server, 1000.0, self.now))
        self.assertEqual(len(json.loads(server.cloud_balance_history)), 1)

    def test_history_cost_wins_over_tariff_estimate(self):
        server = billing_server(
            cloud_provider="timeweb",
            cloud_balance_history=self._history([(24, 1240), (12, 1120)]),
        )
        _apply_snapshot(
            server,
            CloudSnapshot(balance=1000.0, currency="RUB", daily_cost=9.84),
            self.now,
            provider=get_provider("timeweb"),
        )
        self.assertEqual(server.cloud_daily_cost, 240.0)

    def test_tariff_estimate_until_history_grows(self):
        server = billing_server(cloud_provider="timeweb")
        _apply_snapshot(
            server,
            CloudSnapshot(balance=1000.0, currency="RUB", daily_cost=9.84),
            self.now,
            provider=get_provider("timeweb"),
        )
        self.assertEqual(server.cloud_daily_cost, 9.84)
        self.assertEqual(len(json.loads(server.cloud_balance_history)), 1)


class ApplySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def test_forecast_becomes_daily_cost(self):
        server = billing_server()
        _apply_snapshot(server, CloudSnapshot(balance=1000.0, currency="RUB", days_left=10.0), self.now)

        self.assertEqual(server.cloud_daily_cost, 100.0)
        self.assertEqual(server.monthly_cost, 3000.0)
        self.assertEqual(server.paid_until, self.now + timedelta(days=10.0))
        self.assertEqual(server.balance_updated_at, self.now)

    def test_threshold_shortens_the_term(self):
        server = billing_server(cloud_balance_threshold=500)
        _apply_snapshot(server, CloudSnapshot(balance=1000.0, currency="RUB", days_left=10.0), self.now)

        self.assertEqual(server.cloud_daily_cost, 100.0)
        self.assertEqual(server.paid_until, self.now + timedelta(days=5.0))

    def test_daily_cost_from_provider_wins_over_forecast(self):
        server = billing_server(cloud_provider="yandex_cloud")
        _apply_snapshot(
            server,
            CloudSnapshot(balance=900.0, currency="RUB", daily_cost=30.0, days_left=45.0),
            self.now,
        )

        self.assertEqual(server.cloud_daily_cost, 30.0)
        self.assertEqual(server.paid_until, self.now + timedelta(days=30.0))

    def test_without_cost_data_term_is_unknown(self):
        server = billing_server()
        _apply_snapshot(server, CloudSnapshot(balance=1000.0, currency="RUB"), self.now)

        self.assertIsNone(server.paid_until)
        self.assertIsNone(server.cloud_daily_cost)

    def test_warning_is_stored_as_last_error(self):
        server = billing_server(cloud_last_error="old")
        _apply_snapshot(
            server,
            CloudSnapshot(balance=10.0, currency="RUB", warning="No expense data in response"),
            self.now,
        )

        self.assertEqual(server.cloud_last_error, "No expense data in response")


class SyncCloudBalanceTests(unittest.TestCase):
    def test_unknown_provider_is_rejected(self):
        server = billing_server(cloud_provider="digitalocean")
        with self.assertRaises(CloudBillingError):
            asyncio.run(sync_cloud_balance(server, datetime.now(timezone.utc)))

    def test_missing_credential_is_rejected(self):
        server = billing_server(cloud_credential=None)
        with self.assertRaises(CloudBillingError):
            asyncio.run(sync_cloud_balance(server, datetime.now(timezone.utc)))

    def test_yandex_requires_account_id(self):
        server = billing_server(cloud_provider="yandex_cloud", cloud_account_id=None)
        with self.assertRaises(CloudBillingError):
            asyncio.run(sync_cloud_balance(server, datetime.now(timezone.utc)))

    def test_provider_failure_is_recorded_on_the_server(self):
        server = billing_server()
        client = FakeClient({"/v3/balances": FakeResponse(403, None, "forbidden")})

        with patch("app.services.cloud_billing.get_external_client", return_value=client):
            with self.assertRaises(CloudAuthError):
                asyncio.run(sync_cloud_balance(server, datetime.now(timezone.utc)))

        self.assertIn("Forbidden", server.cloud_last_error)

    def test_requests_go_through_the_project_proxy(self):
        server = billing_server(cloud_proxy="10.0.0.1:1080@user:pass")
        client = FakeClient({
            "/v3/balances": balances_response(100000),
            "/v2/billing/prediction": prediction_response(primary=10),
        })

        with patch("app.services.cloud_billing.get_external_client", return_value=client) as pick:
            asyncio.run(sync_cloud_balance(server, datetime.now(timezone.utc)))

        pick.assert_called_once_with("10.0.0.1:1080@user:pass")

    def test_failure_through_proxy_names_the_proxy_without_password(self):
        server = billing_server(cloud_proxy="10.0.0.1:1080@user:secret")
        client = FakeClient({"/v3/balances": FakeResponse(403, None, "forbidden")})

        with patch("app.services.cloud_billing.get_external_client", return_value=client):
            with self.assertRaises(CloudAuthError) as ctx:
                asyncio.run(sync_cloud_balance(server, datetime.now(timezone.utc)))

        self.assertIn("via proxy 10.0.0.1:1080", str(ctx.exception))
        self.assertEqual(server.cloud_last_error, str(ctx.exception))
        self.assertNotIn("secret", server.cloud_last_error)

    def test_registry_exposes_all_providers(self):
        self.assertTrue(get_provider("selectel").id == "selectel")
        self.assertTrue(get_provider("yandex_cloud").requires_account_id)
        self.assertFalse(get_provider("selectel").requires_account_id)
        self.assertFalse(get_provider("timeweb").requires_account_id)
        self.assertTrue(get_provider("timeweb").uses_balance_history)
        self.assertTrue(get_provider("vk_cloud").requires_login)
        self.assertTrue(get_provider("vk_cloud").requires_account_id)
        self.assertFalse(get_provider("selectel").requires_login)

    def test_vk_cloud_requires_login(self):
        server = billing_server(cloud_provider="vk_cloud", cloud_account_id="b5b7ffd4ef05")
        with self.assertRaises(CloudBillingError):
            asyncio.run(sync_cloud_balance(server, datetime.now(timezone.utc)))


def grpc_response(content: bytes = b"", headers: dict | None = None, http_version: str = "HTTP/2"):
    return SimpleNamespace(
        status_code=200,
        http_version=http_version,
        headers=headers or {},
        content=content,
        text="",
    )


def usage_report(expense: str) -> bytes:
    """BillingAccountUsageReportResponse с одним полем expense (StringDecimal)."""
    return _grpc_frame(_pb_submessage(4, _pb_string(1, expense)))


class FakeYandexClient:
    """IAM-обмен и gRPC-отчёт — POST, баланс — GET; ответы фиксированные."""

    def __init__(self, consumption=None, iam=None):
        self.consumption = consumption
        self.iam = iam or FakeResponse(200, {"iamToken": "iam-token"})
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url, json=None, content=None, headers=None, timeout=None):
        self.posts.append((url, headers or {}))
        response = self.iam if url == YC_IAM_ENDPOINT else self.consumption
        if isinstance(response, Exception):
            raise response
        return response

    async def get(self, url, headers=None, timeout=None):
        return FakeResponse(200, {"balance": "900.00", "currency": "RUB"})


class YandexProviderTests(unittest.TestCase):
    def setUp(self):
        get_yc_token_manager()._cache.clear()

    def _fetch(self, client):
        return asyncio.run(YandexCloudProvider().fetch(client, "oauth-token", "account-1"))

    def test_daily_cost_from_grpc_usage_report(self):
        client = FakeYandexClient(consumption=grpc_response(usage_report("-90.00")))

        snapshot = self._fetch(client)

        self.assertEqual(snapshot.balance, 900.0)
        self.assertEqual(snapshot.daily_cost, 30.0)
        self.assertIsNone(snapshot.warning)
        url, headers = client.posts[-1]
        self.assertEqual(url, YC_USAGE_URL)
        self.assertEqual(headers["content-type"], "application/grpc")
        self.assertEqual(headers["authorization"], "Bearer iam-token")

    def test_grpc_error_keeps_balance_and_becomes_warning(self):
        # Trailers-Only: статус и percent-encoded сообщение приходят заголовками, тела нет
        client = FakeYandexClient(consumption=grpc_response(headers={
            "grpc-status": "7", "grpc-message": "need%20billing.accounts.viewer",
        }))

        snapshot = self._fetch(client)

        self.assertEqual(snapshot.balance, 900.0)
        self.assertIsNone(snapshot.daily_cost)
        self.assertEqual(snapshot.warning, "gRPC PERMISSION_DENIED: need billing.accounts.viewer")

    def test_http1_answer_is_not_taken_for_grpc(self):
        client = FakeYandexClient(consumption=grpc_response(
            usage_report("90"), http_version="HTTP/1.1",
        ))

        self.assertIn("HTTP/2", self._fetch(client).warning)

    def test_dead_proxy_is_named_in_the_error(self):
        # Мёртвый SOCKS-туннель даёт ConnectError с пустым текстом
        client = FakeYandexClient(iam=httpx.ConnectError(""))

        with self.assertRaises(CloudBillingError) as ctx:
            self._fetch(client)

        self.assertNotIsInstance(ctx.exception, CloudAuthError)
        self.assertIn("ConnectError", str(ctx.exception))

    def test_rejected_oauth_token_is_an_auth_error(self):
        client = FakeYandexClient(iam=FakeResponse(401, None, "unauthorized"))
        with self.assertRaises(CloudAuthError):
            self._fetch(client)


class GrpcFramingTests(unittest.TestCase):
    def test_frame_roundtrip(self):
        self.assertEqual(_grpc_unframe(_grpc_frame(b"payload")), b"payload")

    def test_empty_body_is_an_error(self):
        with self.assertRaises(CloudBillingError):
            _grpc_unframe(b"")

    def test_truncated_message_is_an_error(self):
        with self.assertRaises(CloudBillingError):
            _grpc_unframe(_grpc_frame(b"payload")[:-2])

    def test_compressed_message_is_rejected(self):
        frame = bytearray(_grpc_frame(b"payload"))
        frame[0] = 1
        with self.assertRaises(CloudBillingError):
            _grpc_unframe(bytes(frame))


class ExternalProxyClientTests(unittest.TestCase):
    def test_one_client_per_proxy(self):
        async def scenario():
            first = get_external_client("10.0.0.1:1080@user:p@ss")
            again = get_external_client("10.0.0.1:1080@user:p@ss")
            other = get_external_client("10.0.0.2:1080")
            await close_http_clients()
            return first, again, other

        first, again, other = asyncio.run(scenario())

        self.assertIs(first, again)
        self.assertIsNot(first, other)


if __name__ == "__main__":
    unittest.main()
