"""Тесты сброса кэша версий ноды по завершении её обновления.

Голый unittest, без PostgreSQL: сессия подменена.

Версии ноды кэшируются на полминуты. Страница «Обновления» перечитывает ноду,
как только её обновление завершилось, и без сброса кэша получала прежнюю версию:
на большом парке, где фоновый обход нод отключён, она оставалась на экране до
ручной проверки.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers import system  # noqa: E402
from app.services import node_update_watcher  # noqa: E402

NODE_ID = 5
OTHER_NODE_ID = 6
FAR_FUTURE = float("inf")


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        return SimpleNamespace(rowcount=1)

    async def commit(self):
        return None


class UpdateFinishedTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        system._node_versions_cache[NODE_ID] = (FAR_FUTURE, {"node_version": "10.35.0"})
        system._node_versions_cache[OTHER_NODE_ID] = (FAR_FUTURE, {"node_version": "10.35.0"})
        self.addCleanup(system._node_versions_cache.clear)

    async def test_finished_update_drops_the_cached_versions_of_that_node(self):
        attempt = SimpleNamespace(server_id=NODE_ID, started_at=datetime.now(timezone.utc))
        with mock.patch.object(node_update_watcher, "async_session", FakeSession):
            await node_update_watcher._close(attempt)

        self.assertNotIn(NODE_ID, system._node_versions_cache)
        self.assertIn(OTHER_NODE_ID, system._node_versions_cache)


if __name__ == "__main__":
    unittest.main()
