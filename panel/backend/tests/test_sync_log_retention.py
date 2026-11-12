"""Журналы раскатки профилей чистятся по сроку (services/metrics_collector).

Голый unittest, без PostgreSQL: сессия записывает запросы вместо их выполнения.

Каждая раскатка пишет в журнал строку на сервер. Раньше журналы не чистились
вовсе: на 2000 серверах десяток правок профиля в день — это миллионы строк в год,
а страница профиля читает последние пятьдесят.

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
from sqlalchemy.sql.dml import Delete  # noqa: E402

from app.services.metrics_collector import MetricsCollector  # noqa: E402

SYNC_JOURNALS = {"firewall_sync_log", "haproxy_sync_log", "dnat_sync_log", "remnawave_nginx_sync_log"}


class RecordingSession:
    def __init__(self) -> None:
        self.deletes: dict[str, Delete] = {}
        self.commit = mock.AsyncMock()

    async def execute(self, statement):
        if isinstance(statement, Delete):
            self.deletes[statement.table.name] = statement
        return SimpleNamespace(rowcount=0)


class SyncLogRetentionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = RecordingSession()
        await MetricsCollector()._cleanup_old_data(self.db)

    def cutoff(self, journal: str) -> datetime:
        params = self.db.deletes[journal].compile(dialect=postgresql.dialect()).params
        return next(value for value in params.values() if isinstance(value, datetime))

    def test_every_profile_journal_is_cleaned(self):
        self.assertLessEqual(SYNC_JOURNALS, set(self.db.deletes))

    def test_only_rows_older_than_the_retention_are_removed(self):
        expected = datetime.now(timezone.utc) - timedelta(days=MetricsCollector.SYNC_LOG_RETENTION_DAYS)

        for journal in SYNC_JOURNALS:
            with self.subTest(journal=journal):
                query = str(self.db.deletes[journal].compile(dialect=postgresql.dialect()))
                self.assertIn(f"{journal}.created_at <", query)
                self.assertLess(abs((self.cutoff(journal) - expected).total_seconds()), 5)

    def test_cutoff_carries_its_timezone(self):
        # created_at — timestamptz: наивное время драйвер прочёл бы как местное
        for journal in SYNC_JOURNALS:
            with self.subTest(journal=journal):
                self.assertIsNotNone(self.cutoff(journal).tzinfo)

    def test_retention_is_long_enough_to_look_back_at_last_month(self):
        self.assertGreaterEqual(MetricsCollector.SYNC_LOG_RETENTION_DAYS, 30)


if __name__ == "__main__":
    unittest.main()
