"""Срок хранения истории метрик идёт от последнего ответа сервера (services/metrics_collector).

Голый unittest, без PostgreSQL: сессия записывает запросы вместо их выполнения.

Раньше срок шёл от «сейчас»: через сутки после смерти ноды её снапшоты
стирались целиком, через месяц — часовые агрегаты, и странице сервера было
нечего показать за последние часы его работы. Теперь у замолчавшего сервера
часы стоят — хранится то же окно, но перед его последним ответом.

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

RAW_KEEP = timedelta(hours=24)
HOURLY_KEEP = timedelta(days=30)
DAILY_KEEP = timedelta(days=365)


class RecordingSession:
    def __init__(self) -> None:
        self.deletes: list[Delete] = []
        self.commit = mock.AsyncMock()

    async def execute(self, statement):
        if isinstance(statement, Delete):
            self.deletes.append(statement)
        return SimpleNamespace(rowcount=0)


class HistoryRetentionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = RecordingSession()
        await MetricsCollector()._cleanup_old_data(self.db)

    def cleanup_of(self, table: str, keep: timedelta) -> tuple[str, dict]:
        """Запрос чистки таблицы с данным сроком хранения и его параметры."""
        for statement in self.db.deletes:
            compiled = statement.compile(dialect=postgresql.dialect())
            if statement.table.name == table and keep in compiled.params.values():
                return " ".join(str(compiled).split()), compiled.params
        self.fail(f"no cleanup of {table} with retention {keep}")

    def test_every_tier_counts_its_retention_from_the_last_answer_of_the_server(self):
        for table, keep in (
            ("metrics_snapshots", RAW_KEEP),
            ("aggregated_metrics", HOURLY_KEEP),
            ("aggregated_metrics", DAILY_KEEP),
        ):
            with self.subTest(table=table, keep=keep):
                query, _ = self.cleanup_of(table, keep)

                self.assertIn(f"{table}.server_id = servers.id", query)
                self.assertIn(f"{table}.timestamp < servers.last_seen - ", query)

    def test_rows_newer_than_the_plain_retention_are_never_touched(self):
        # Граница «от сейчас» остаётся: last_seen не бывает позже «сейчас»,
        # а по одной колонке timestamp планировщик идёт индексом
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        for table, keep in (
            ("metrics_snapshots", RAW_KEEP),
            ("aggregated_metrics", HOURLY_KEEP),
            ("aggregated_metrics", DAILY_KEEP),
        ):
            with self.subTest(table=table, keep=keep):
                _, params = self.cleanup_of(table, keep)
                cutoff, = (value for value in params.values() if isinstance(value, datetime))

                self.assertLess(abs((cutoff - (now - keep)).total_seconds()), 5)

    def test_chunk_is_picked_from_the_table_itself_not_correlated_to_the_delete(self):
        # Без FROM по самой таблице подзапрос стал бы коррелированным: LIMIT
        # считался бы на каждую строку, и чистка шла бы одним DELETE на всё разом
        for table, keep in (("metrics_snapshots", RAW_KEEP), ("aggregated_metrics", HOURLY_KEEP)):
            with self.subTest(table=table):
                query, _ = self.cleanup_of(table, keep)
                chunk = query[query.index("IN (SELECT"):]

                self.assertRegex(chunk, rf"FROM [a-z_, ]*\b{table}\b")
                self.assertIn("LIMIT", chunk)

    def test_hourly_and_daily_aggregates_keep_their_own_scope(self):
        hourly, hourly_params = self.cleanup_of("aggregated_metrics", HOURLY_KEEP)
        daily, daily_params = self.cleanup_of("aggregated_metrics", DAILY_KEEP)

        self.assertIn("aggregated_metrics.period_type = ", hourly)
        self.assertIn("hour", hourly_params.values())
        self.assertIn("day", daily_params.values())

    def test_traffic_history_still_ages_from_now(self):
        traffic = [
            " ".join(str(statement.compile(dialect=postgresql.dialect())).split())
            for statement in self.db.deletes if statement.table.name == "server_traffic"
        ]

        self.assertTrue(traffic)
        for query in traffic:
            self.assertNotIn("servers.last_seen", query)


if __name__ == "__main__":
    unittest.main()
