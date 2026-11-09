import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.billing_checker import BillingChecker, _rearm_thresholds  # noqa: E402


class FakeSession:
    def __init__(self, server):
        self.server = server

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, server_id):
        return self.server

    async def commit(self):
        pass


class RearmThresholdsTests(unittest.TestCase):
    def test_topup_far_above_thresholds_rearms_all(self):
        self.assertEqual(_rearm_thresholds([7, 3, 1], 30), [])

    def test_still_below_threshold_stays_notified(self):
        self.assertEqual(_rearm_thresholds([7, 3], 5), [7])

    def test_jitter_just_above_threshold_does_not_rearm(self):
        # Оценка расхода дрожит около границы — без запаса уведомление повторялось бы
        self.assertEqual(_rearm_thresholds([7], 8), [7])

    def test_small_topup_rearms_one_day_threshold(self):
        # Пополнение ещё на сутки работы: следующее «остался день» должно прийти
        self.assertEqual(_rearm_thresholds([1], 1.3), [])


class CloudTopupNotificationTests(unittest.TestCase):
    """Облачный баланс пополняют у провайдера, мимо панели: снова предупредить
    о следующем снижении чекер должен сам, без ручного «Обновить»."""

    def _check(self, server, now, days_left, send):
        async def fake_sync(srv, sync_now):
            srv.paid_until = sync_now + timedelta(days=days_left)

        with patch("app.services.billing_checker.async_session", lambda: FakeSession(server)), \
                patch("app.services.billing_checker.sync_cloud_balance", fake_sync), \
                patch.object(BillingChecker, "_send_notification", send):
            asyncio.run(BillingChecker()._check_server(1, now, [1], "bot", "chat"))

    def test_alert_comes_again_after_topup_at_provider(self):
        server = SimpleNamespace(
            name="Selectel", billing_type="cloud", paid_until=None,
            last_notified_days=json.dumps([1]),
        )
        send = AsyncMock(return_value=True)
        now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

        self._check(server, now, days_left=10, send=send)
        send.assert_not_called()
        self.assertEqual(json.loads(server.last_notified_days), [])

        self._check(server, now + timedelta(days=9.5), days_left=0.5, send=send)
        send.assert_called_once()
        self.assertEqual(json.loads(server.last_notified_days), [1])


if __name__ == "__main__":
    unittest.main()
