"""Тесты сводки правил по серверам (GET /blocklist/servers/summary, app/routers/blocklist.py).

Голый unittest, без PostgreSQL: сессия подменена, запоминает запросы и отдаёт
заранее заданные строки группировки.

Страница блок-листов показывает счётчики на карточке каждого сервера. Пока их
брали из списков правил, открытие страницы стоило двух запросов на сервер —
четырёх тысяч на парке в две тысячи нод. Сводка обязана уложиться в один запрос
к базе, сколько бы серверов ни было.

Запуск из panel/backend:  python -m unittest tests.test_blocklist_servers_summary -v
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.dialects import postgresql  # noqa: E402

from app.routers import blocklist  # noqa: E402

FLEET_SIZE = 2000


class GroupedRowsSession:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    async def execute(self, statement):
        self.queries.append(" ".join(str(statement.compile(dialect=postgresql.dialect())).split()))
        return SimpleNamespace(all=lambda: self.rows)


class ServersSummaryTest(unittest.IsolatedAsyncioTestCase):
    async def _summary(self, rows: list[tuple]) -> dict:
        self.db = GroupedRowsSession(rows)
        return await blocklist.get_servers_rules_summary(db=self.db, _={})

    async def test_counts_are_split_into_global_and_per_server(self):
        summary = await self._summary([
            (None, "in", 40),
            (None, "out", 2),
            (7, "in", 3),
            (7, "out", 1),
            (9, "out", 5),
        ])

        self.assertEqual(summary["global"], {"in": 40, "out": 2})
        self.assertEqual(summary["servers"], {7: {"in": 3, "out": 1}, 9: {"in": 0, "out": 5}})

    async def test_empty_table_gives_zero_global_counts(self):
        summary = await self._summary([])

        self.assertEqual(summary, {"global": {"in": 0, "out": 0}, "servers": {}})

    async def test_whole_fleet_costs_one_grouped_query(self):
        rows = [(server_id, "in", 1) for server_id in range(1, FLEET_SIZE + 1)]

        summary = await self._summary(rows)

        self.assertEqual(len(summary["servers"]), FLEET_SIZE)
        self.assertEqual(len(self.db.queries), 1)
        query = self.db.queries[0].lower()
        self.assertIn("count(", query)
        self.assertIn("group by blocklist_rules.server_id, blocklist_rules.direction", query)

    async def test_rules_without_direction_are_not_counted(self):
        # Списки правил сервера отбирают по direction = 'in' | 'out': счётчик на
        # карточке не должен расходиться с тем, что покажет раскрытая карточка
        await self._summary([])

        self.assertIn("direction in (", self.db.queries[0].lower())


if __name__ == "__main__":
    unittest.main()
