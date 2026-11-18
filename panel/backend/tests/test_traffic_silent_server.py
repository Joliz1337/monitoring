"""Страница «Трафик» сервера, который перестал отвечать (app/routers/traffic.py).

Голый unittest, без PostgreSQL: чтение бакетов и сервера подменено.

История трафика лежит в базе панели, но окна считались от «сейчас»: у сервера,
молчащего третий день, график за сутки был пуст, а у молчащего второй месяц
сводка за 30 дней показывала нули. Теперь окна такого сервера считаются от его
последнего ответа.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.dialects import postgresql  # noqa: E402

from app.models import Server  # noqa: E402
from app.routers import traffic  # noqa: E402
from app.services.traffic_ingest import Period  # noqa: E402

SERVER_ID = 1
NOW = datetime(2026, 8, 26, 15, 30)
DIED = datetime(2026, 8, 3, 9, 40, tzinfo=timezone.utc)
DIED_HOUR = datetime(2026, 8, 3, 9)
DIED_DAY = datetime(2026, 8, 3)


class RecordingSession:
    """Сводка читает базу сама: сессия запоминает запрос и отдаёт пустой результат."""

    def __init__(self) -> None:
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: [])

    def window_start(self) -> datetime:
        params = self.statements[-1].compile(dialect=postgresql.dialect()).params
        return next(value for value in params.values() if isinstance(value, datetime))


class TrafficOfSilentServerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = RecordingSession()
        self.frozen_at: datetime | None = None
        self.buckets: dict[Period, dict] = {}
        self.windows: list[tuple[Period, datetime, datetime]] = []
        self.gaps = mock.AsyncMock(return_value=[])
        for name, replacement in (
            ("_utcnow", mock.Mock(return_value=NOW)),
            ("_load_server", mock.AsyncMock(return_value=Server(id=SERVER_ID, name="node-1", url="https://10.0.0.1:9100"))),
            ("last_answer_if_offline", mock.AsyncMock(side_effect=lambda _server, _db: self.frozen_at)),
            ("_load_buckets", self.load_buckets),
            ("_load_gaps", self.gaps),
        ):
            patcher = mock.patch.object(traffic, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def load_buckets(self, _db, _server_id, period_type, _scope, _scope_key, first_bucket, last_bucket):
        self.windows.append((period_type, first_bucket, last_bucket))
        return self.buckets.get(period_type, {})

    async def series(self, period: str) -> dict:
        return await traffic.get_traffic_series(SERVER_ID, period=period, scope="total", key="", db=self.db, _={})


class SeriesWindowTest(TrafficOfSilentServerCase):
    async def test_answering_server_window_ends_now(self):
        self.buckets[Period.HOUR] = {datetime(2026, 8, 26, 14): (10, 20)}

        response = await self.series("24h")

        self.assertEqual(self.windows, [(Period.HOUR, datetime(2026, 8, 25, 16), datetime(2026, 8, 26, 15))])
        self.assertIsNone(response["frozen_at"])
        self.assertFalse(response["coarser_than_period"])

    async def test_silent_server_window_ends_at_its_last_answer(self):
        self.frozen_at = DIED
        self.buckets[Period.HOUR] = {DIED_HOUR: (10, 20)}

        response = await self.series("24h")

        self.assertEqual(self.windows, [(Period.HOUR, DIED_HOUR - timedelta(hours=23), DIED_HOUR)])
        self.assertEqual(response["frozen_at"], "2026-08-03T09:40:00.000Z")
        self.assertEqual(response["points"][-1], {"timestamp": "2026-08-03T09:00:00.000Z", "rx": 10, "tx": 20})
        self.assertFalse(response["coarser_than_period"])

    async def test_gaps_are_looked_up_inside_the_frozen_window(self):
        self.frozen_at = DIED
        self.buckets[Period.HOUR] = {DIED_HOUR: (10, 20)}

        await self.series("24h")

        self.gaps.assert_awaited_once_with(self.db, SERVER_ID, DIED_HOUR - timedelta(hours=23), DIED.replace(tzinfo=None))

    async def test_daily_periods_are_frozen_too(self):
        self.frozen_at = DIED
        self.buckets[Period.DAY] = {DIED_DAY: (10, 20)}

        response = await self.series("30d")

        self.assertEqual(self.windows, [(Period.DAY, DIED_DAY - timedelta(days=29), DIED_DAY)])
        self.assertFalse(response["coarser_than_period"])

    async def test_purged_hourly_buckets_are_replaced_by_daily_ones_of_the_last_month(self):
        # Часовые бакеты живут две недели: у сервера, молчащего дольше, их уже нет
        self.frozen_at = DIED
        self.buckets[Period.DAY] = {DIED_DAY: (1000, 2000)}

        for period in ("24h", "7d"):
            with self.subTest(period=period):
                self.windows.clear()

                response = await self.series(period)

                self.assertEqual(self.windows[-1], (Period.DAY, DIED_DAY - timedelta(days=29), DIED_DAY))
                self.assertEqual(response["period"], period)
                self.assertTrue(response["coarser_than_period"])
                self.assertEqual(len(response["points"]), 30)
                self.assertEqual(response["points"][-1]["rx"], 1000)

    async def test_silent_server_without_any_traffic_keeps_the_requested_period(self):
        self.frozen_at = DIED

        response = await self.series("24h")

        self.assertEqual(len(response["points"]), 24)
        self.assertFalse(response["coarser_than_period"])

    async def test_empty_hours_of_an_answering_server_are_not_replaced_by_older_days(self):
        self.buckets[Period.DAY] = {datetime(2026, 8, 1): (1000, 2000)}

        response = await self.series("24h")

        self.assertEqual(len(self.windows), 1)
        self.assertFalse(response["coarser_than_period"])


class SummaryWindowOfSilentServerTest(TrafficOfSilentServerCase):
    async def summary(self) -> dict:
        return await traffic.get_server_traffic_summary(SERVER_ID, days=30, db=self.db, _={})

    async def test_answering_server_sums_the_last_days_up_to_today(self):
        response = await self.summary()

        self.assertEqual(self.db.window_start(), datetime(2026, 8, 26) - timedelta(days=29))
        self.assertIsNone(response["frozen_at"])

    async def test_silent_server_sums_the_last_days_before_its_last_answer(self):
        self.frozen_at = DIED

        response = await self.summary()

        self.assertEqual(self.db.window_start(), DIED_DAY - timedelta(days=29))
        self.assertEqual(response["frozen_at"], "2026-08-03T09:40:00.000Z")


if __name__ == "__main__":
    unittest.main()
