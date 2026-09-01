import asyncio
import json
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
from app.services.cloud_billing import (  # noqa: E402
    HISTORY_WINDOW_DAYS,
    _balance_history_daily_cost,
)
from app.services.cloud_billing.selectel import (  # noqa: E402
    CONSUMPTION_WINDOW_DAYS,
    SelectelProvider,
    _billing_sum,
    _payload,
    _pick_prediction_days,
    _transaction_rows,
)
from app.services.cloud_billing.timeweb import TimewebProvider, _tariff_daily_cost  # noqa: E402


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


def finances_response(**finances):
    return FakeResponse(200, {"finances": finances})


class TimewebProviderTests(unittest.TestCase):
    def _fetch(self, client):
        with patch("app.services.cloud_billing.timeweb.get_external_client", return_value=client):
            return asyncio.run(TimewebProvider().fetch("bearer-token", None))

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

        with patch("app.services.cloud_billing.selectel.get_external_client", return_value=client):
            with self.assertRaises(CloudAuthError):
                asyncio.run(sync_cloud_balance(server, datetime.now(timezone.utc)))

        self.assertIn("Forbidden", server.cloud_last_error)

    def test_registry_exposes_all_providers(self):
        self.assertTrue(get_provider("selectel").id == "selectel")
        self.assertTrue(get_provider("yandex_cloud").requires_account_id)
        self.assertFalse(get_provider("selectel").requires_account_id)
        self.assertFalse(get_provider("timeweb").requires_account_id)
        self.assertTrue(get_provider("timeweb").uses_balance_history)


if __name__ == "__main__":
    unittest.main()
