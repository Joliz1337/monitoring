"""Тесты NDJSON-стрима массовых операций (app/services/bulk_stream.py).

Голый unittest, без сети: работа над нодой подменена.

Стрим запускал работу сразу по всем серверам. На парке в тысячи нод это тысячи
одновременных соединений к нодам и столько же сессий базы в один момент.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import bulk_stream  # noqa: E402

FLEET = 200


class Tracker:
    def __init__(self) -> None:
        self.running = 0
        self.peak = 0

    async def __call__(self, server) -> dict:
        self.running += 1
        self.peak = max(self.peak, self.running)
        await asyncio.sleep(0.001)
        self.running -= 1
        return {"server_id": server.id, "server_name": server.name, "success": True}


async def consume(servers, worker) -> list[dict]:
    response = bulk_stream.stream_ndjson(servers, worker)
    return [json.loads(chunk) async for chunk in response.body_iterator]


class BulkStreamTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.servers = [SimpleNamespace(id=i, name=f"node-{i}") for i in range(1, FLEET + 1)]

    async def test_nodes_are_processed_in_bounded_groups(self):
        tracker = Tracker()
        await consume(self.servers, tracker)
        self.assertLessEqual(tracker.peak, bulk_stream.MAX_PARALLEL_NODES)
        self.assertGreater(tracker.peak, 1)

    async def test_every_node_still_gets_its_result_line(self):
        lines = await consume(self.servers, Tracker())
        results = [line for line in lines if line["type"] == "result"]
        self.assertEqual(sorted(line["server_id"] for line in results), list(range(1, FLEET + 1)))
        self.assertEqual(lines[-1], {"type": "done", "total": FLEET, "ok": FLEET, "failed": 0})


if __name__ == "__main__":
    unittest.main()
