"""Тесты ответа «правила сервера» блок-листа (app/routers/blocklist.py).

Голый unittest, без PostgreSQL: сессия подменена и запоминает запросы.

Страница блок-листов запрашивает правила каждого сервера по два раза. Запрос,
который ради одного числа поднимает все глобальные правила, а ради проверки
«сервер существует» — всю его строку с метриками, на парке в тысячи нод
превращается в тысячи полных чтений списка.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.dialects import postgresql  # noqa: E402

from app.routers import blocklist  # noqa: E402

GLOBAL_RULES_TOTAL = 4321


class RecordingSession:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def execute(self, statement):
        sql = " ".join(str(statement.compile(dialect=postgresql.dialect())).split())
        self.queries.append(sql)
        return SimpleNamespace(
            scalar_one_or_none=lambda: 1,
            scalar_one=lambda: GLOBAL_RULES_TOTAL,
            scalars=lambda: SimpleNamespace(all=lambda: []),
        )


class ServerRulesPayloadTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = RecordingSession()
        self.payload = await blocklist._build_server_rules_payload(self.db, server_id=1, direction="in")

    def test_global_rules_are_counted_by_the_database(self):
        self.assertEqual(self.payload["global_count"], GLOBAL_RULES_TOTAL)
        global_queries = [q for q in self.db.queries if "server_id IS NULL" in q]
        self.assertEqual(len(global_queries), 1)
        self.assertIn("count(", global_queries[0].lower())

    def test_server_existence_check_does_not_load_its_metrics(self):
        self.assertFalse([q for q in self.db.queries if "last_metrics" in q])


if __name__ == "__main__":
    unittest.main()
