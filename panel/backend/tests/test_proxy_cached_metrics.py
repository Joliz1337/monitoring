"""Тесты ответа «метрики сервера из панели» (GET /proxy/{id}/metrics, app/routers/proxy.py).

Голый unittest, без PostgreSQL и без сети: сессия подменена.

Страница сервера опрашивает этот ответ постоянно. Последний ответ ноды панель
держит в памяти, поэтому поднимать ~18 КБ JSON из `servers.last_metrics` и
разбирать их на каждый запрос незачем.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

from app.models import Server  # noqa: E402
from app.routers import proxy  # noqa: E402
from app.services.latest_metrics import LatestMetrics  # noqa: E402

SERVER_ID = 1
SNAPSHOT = SimpleNamespace(
    net_rx_bytes_per_sec=800.0, net_tx_bytes_per_sec=200.0,
    disk_read_bytes_per_sec=5.0, disk_write_bytes_per_sec=15.0,
)


def new_agent_answer() -> dict:
    return {
        "live_rates": {"window_sec": 1.0},
        "network": {"total": {"rx_bytes_per_sec": 111.0, "tx_bytes_per_sec": 222.0}},
    }


def old_agent_answer() -> dict:
    """Агент без посекундных скоростей: их панель дописывает из последнего снапшота."""
    return {"network": {"total": {"rx_bytes": 4000, "tx_bytes": 1000}, "interfaces": []}}


class RecordingSession:
    """Сессия без базы: отдаёт сервер (или его отсутствие) и запоминает текст запросов."""

    def __init__(self, server: Server | None) -> None:
        self._server = server
        self.queries: list[str] = []

    async def execute(self, statement):
        self.queries.append(str(statement.compile(dialect=postgresql.dialect())))
        return SimpleNamespace(scalar_one_or_none=lambda: self._server)

    async def commit(self) -> None:
        pass


class CachedMetricsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.latest = LatestMetrics()
        self.db = RecordingSession(Server(id=SERVER_ID, name="node-1", url="https://10.0.0.1:9100"))
        self.snapshot_lookup = mock.AsyncMock(return_value=SNAPSHOT)
        for name, replacement in (
            ("get_latest_metrics", mock.Mock(return_value=self.latest)),
            ("get_latest_snapshot", self.snapshot_lookup),
        ):
            patcher = mock.patch.object(proxy, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def metrics(self) -> dict:
        return await proxy.get_metrics(SERVER_ID, db=self.db, _={})

    async def test_answer_of_the_node_comes_from_memory(self):
        self.latest.put(SERVER_ID, new_agent_answer())

        self.assertEqual(await self.metrics(), new_agent_answer())

    async def test_old_agent_gets_snapshot_speeds_and_the_shared_answer_stays_intact(self):
        # Ответ в памяти один на всех читателей — скорости дописываются в копию
        self.latest.put(SERVER_ID, old_agent_answer())

        metrics = await self.metrics()

        self.assertEqual(metrics["network"]["total"]["rx_bytes_per_sec"], 800.0)
        self.assertEqual(self.latest.get(SERVER_ID), old_agent_answer())

    async def test_answer_with_its_own_speeds_is_returned_as_is(self):
        # Скорости такой ноде не дописываются: ни снапшот, ни копия ответа не нужны
        self.latest.put(SERVER_ID, new_agent_answer())

        metrics = await self.metrics()

        self.assertIs(metrics, self.latest.get(SERVER_ID))
        self.snapshot_lookup.assert_not_awaited()

    async def test_node_that_has_not_answered_yet_gives_503(self):
        with self.assertRaises(HTTPException) as raised:
            await self.metrics()

        self.assertEqual(raised.exception.status_code, 503)

    async def test_unknown_server_gives_404_even_with_an_answer_left_in_memory(self):
        self.latest.put(SERVER_ID, new_agent_answer())
        self.db = RecordingSession(None)

        with self.assertRaises(HTTPException) as raised:
            await self.metrics()

        self.assertEqual(raised.exception.status_code, 404)

    async def test_saved_answer_is_not_read_from_the_database(self):
        self.latest.put(SERVER_ID, new_agent_answer())

        await self.metrics()

        self.assertTrue(self.db.queries)
        self.assertFalse([query for query in self.db.queries if "last_metrics" in query])


if __name__ == "__main__":
    unittest.main()
