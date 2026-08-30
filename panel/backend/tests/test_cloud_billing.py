import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.cloud_billing import (  # noqa: E402
    CloudAuthError,
    CloudBillingError,
    _apply_snapshot,
    get_provider,
    sync_cloud_balance,
)
from app.services.cloud_billing.base import CloudSnapshot, compute_days_left  # noqa: E402
from app.services.cloud_billing.selectel import (  # noqa: E402
    CONSUMPTION_WINDOW_DAYS,
    SelectelProvider,
    _billing_sum,
    _payload,
    _pick_prediction_days,
    _transaction_rows,
)


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakeClient:
    """Отдаёт заранее заданные ответы по пути запроса (query отбрасывается)."""

    def __init__(self, by_path: dict):
        self.by_path = by_path
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, headers=None, timeout=None):
        target = url.replace("https://api.selectel.ru", "")
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


def transactions_response(*prices):
    return FakeResponse(200, {
        "status": "success",
        "data": [{"price": p, "state": "PAID"} for p in prices],
    })


def billing_server(**overrides):
    server = SimpleNamespace(
        name="cloud-1",
        billing_type="cloud",
        cloud_provider="selectel",
        cloud_credential="token",
        cloud_account_id=None,
        cloud_balance_threshold=0,
        cloud_daily_cost=None,
        cloud_last_sync_at=None,
        cloud_last_error=None,
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

    def test_transaction_rows_accepts_both_shapes(self):
        self.assertEqual(_transaction_rows([{"price": -1}]), [{"price": -1}])
        self.assertEqual(_transaction_rows({"transactions": [{"price": -1}]}), [{"price": -1}])
        self.assertEqual(_transaction_rows({"total": 0}), [])


class SelectelProviderTests(unittest.TestCase):
    def _fetch(self, client):
        with patch("app.services.cloud_billing.selectel.get_external_client", return_value=client):
            return asyncio.run(SelectelProvider().fetch("static-token", None))

    def test_balance_and_daily_cost_come_from_minor_units(self):
        # Форма реального ответа аккаунта: остаток 17 824,41 ₽, списания за месяц
        client = FakeClient({
            "/v3/balances": balances_response(1782441),
            "/v2/billing/transactions": transactions_response(-235609, -773000, 500000),
        })

        snapshot = self._fetch(client)

        self.assertEqual(snapshot.balance, 17824.41)
        self.assertEqual(snapshot.currency, "RUB")
        # Пополнение (+5000) в расход не идёт: (2356.09 + 7730.00) / 30
        self.assertEqual(snapshot.daily_cost, round(10086.09 / CONSUMPTION_WINDOW_DAYS, 4))
        self.assertIsNone(snapshot.days_left)
        self.assertIsNone(snapshot.warning)
        self.assertEqual(client.calls[0][1]["X-Token"], "static-token")

    def test_prediction_is_the_fallback_without_charges(self):
        client = FakeClient({
            "/v3/balances": balances_response(1782441),
            "/v2/billing/transactions": transactions_response(),
            "/v2/billing/prediction": FakeResponse(200, {
                "status": "success",
                "data": {"primary": 46, "storage": None, "vmware": None, "vpc": None},
            }),
        })

        snapshot = self._fetch(client)

        self.assertIsNone(snapshot.daily_cost)
        self.assertEqual(snapshot.days_left, 46)

    def test_transactions_are_paginated(self):
        page = transactions_response(*([-100] * 500))
        client = FakeClient({
            "/v3/balances": balances_response(1000000),
            "/v2/billing/transactions": page,
        })

        with patch("app.services.cloud_billing.selectel.TRANSACTIONS_MAX_PAGES", 3):
            snapshot = self._fetch(client)

        transaction_calls = [c for c in client.calls if c[0].startswith("/v2/billing/transactions")]
        self.assertEqual(len(transaction_calls), 3)
        self.assertIn("offset=1000", transaction_calls[-1][0])
        self.assertEqual(snapshot.daily_cost, round(1500.0 / CONSUMPTION_WINDOW_DAYS, 4))

    def test_debt_is_reported_as_warning(self):
        client = FakeClient({
            "/v3/balances": balances_response(0, debt_sum=50000),
            "/v2/billing/transactions": transactions_response(),
            "/v2/billing/prediction": FakeResponse(200, {"status": "ok", "data": {}}),
        })

        snapshot = self._fetch(client)

        self.assertEqual(snapshot.balance, 0.0)
        self.assertIsNone(snapshot.days_left)
        self.assertIn("500.00", snapshot.warning)

    def test_bad_token_raises_auth_error(self):
        client = FakeClient({"/v3/balances": FakeResponse(401, None, "unauthorized")})
        with self.assertRaises(CloudAuthError):
            self._fetch(client)

    def test_cost_sources_failure_keeps_balance(self):
        client = FakeClient({
            "/v3/balances": balances_response(100000),
            "/v2/billing/transactions": FakeResponse(400, None, "bad request"),
            "/v2/billing/prediction": FakeResponse(400, None, "bad request"),
        })

        snapshot = self._fetch(client)

        self.assertEqual(snapshot.balance, 1000.0)
        self.assertIsNone(snapshot.daily_cost)
        self.assertIsNone(snapshot.days_left)


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

        with patch("app.services.cloud_billing.selectel.get_external_client", return_value=client):
            with self.assertRaises(CloudAuthError):
                asyncio.run(sync_cloud_balance(server, datetime.now(timezone.utc)))

        self.assertIn("Forbidden", server.cloud_last_error)

    def test_registry_exposes_both_providers(self):
        self.assertTrue(get_provider("selectel").id == "selectel")
        self.assertTrue(get_provider("yandex_cloud").requires_account_id)
        self.assertFalse(get_provider("selectel").requires_account_id)


if __name__ == "__main__":
    unittest.main()
