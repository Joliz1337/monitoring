"""Тесты сборки белого списка анти-DDoS (app/services/antiddos_manager.py).

Голый unittest, без PostgreSQL и без сети: сессия и внешние источники подменены.

В список попадают все адреса нод, включая дополнительные и вторые карты, — их
нода сообщает в метриках. Последний ответ ноды панель держит в памяти, поэтому
поднимать его копию из `servers.last_metrics` у всего парка незачем.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.dialects import postgresql  # noqa: E402

from app.models import AntiDdosSettings, Server  # noqa: E402
from app.services import antiddos_manager  # noqa: E402
from app.services.antiddos_manager import AntiDdosManager  # noqa: E402
from app.services.latest_metrics import LatestMetrics  # noqa: E402

PANEL_IP = "203.0.113.5"


def answer_with(*addresses: str) -> dict:
    return {"network": {"interfaces": [{"name": "eth0", "addresses": [
        {"type": "ipv4", "address": address} for address in addresses
    ]}]}}


class FakeResult:
    def __init__(self, entities: list, columns: list[str]) -> None:
        self._entities = entities
        self._columns = columns

    def scalars(self) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: list(self._entities))

    def scalar_one_or_none(self):
        return self._entities[0] if self._entities else None

    def all(self) -> list[tuple]:
        return [tuple(getattr(entity, column) for column in self._columns) for entity in self._entities]


class FakeSession:
    """Сессия без базы: отдаёт заготовленные строки по сущности запроса и запоминает его текст."""

    def __init__(self, rows: dict[type, list]) -> None:
        self._rows = rows
        self.queries: list[str] = []

    async def execute(self, statement) -> FakeResult:
        self.queries.append(str(statement.compile(dialect=postgresql.dialect())))
        entity = statement.column_descriptions[0]["entity"]
        return FakeResult(self._rows.get(entity, []), [column.name for column in statement.selected_columns])


class WhitelistBuildTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.latest = LatestMetrics()
        self.manager = AntiDdosManager()
        self.manager.fetch_all_source_ips = mock.AsyncMock(return_value=[])
        for name, replacement in (
            ("get_latest_metrics", mock.Mock(return_value=self.latest)),
            ("resolve_panel_ip", mock.AsyncMock(return_value=PANEL_IP)),
        ):
            patcher = mock.patch.object(antiddos_manager, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.db = FakeSession({
            Server: [
                Server(id=1, url="https://5.9.0.10:9100", dedicated_cert=False),
                Server(id=2, url="https://95.216.0.7:9100", dedicated_cert=False),
            ],
            AntiDdosSettings: [AntiDdosSettings(user_cidrs=None)],
        })

    async def test_interface_addresses_of_nodes_come_from_memory(self):
        self.latest.put(1, answer_with("5.9.0.10", "5.9.0.11"))

        whitelist = await self.manager.build_whitelist(self.db)

        self.assertEqual(whitelist, sorted([PANEL_IP, "5.9.0.10", "5.9.0.11", "95.216.0.7"]))

    async def test_saved_answers_are_not_read_from_the_database(self):
        await self.manager.build_whitelist(self.db)

        self.assertTrue(self.db.queries)
        self.assertFalse([query for query in self.db.queries if "last_metrics" in query])


if __name__ == "__main__":
    unittest.main()
