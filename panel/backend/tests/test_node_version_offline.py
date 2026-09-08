"""Версия ноды со страниц «Обновления»/«Системные оптимизации»: ноду, которую
коллектор метрик считает офлайн, панель не опрашивает — отдаёт «offline» сразу.

Голый unittest, без PostgreSQL и без сети.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.routers import system as system_router
    from app.services.server_status import DEFAULT_OFFLINE_THRESHOLD
except ImportError as e:  # рантайм панели не установлен
    raise unittest.SkipTest(f"routers.system requires the panel runtime: {e}")


class FakeDb:
    def __init__(self, server):
        self._server = server

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self._server)


def make_server(last_seen_age_sec: int | None, last_error: str | None = None):
    last_seen = None
    if last_seen_age_sec is not None:
        last_seen = datetime.now(timezone.utc) - timedelta(seconds=last_seen_age_sec)
    return SimpleNamespace(
        id=7, name="node-7", url="https://10.0.0.7:9100",
        last_seen=last_seen, last_error=last_error,
    )


NODE_ANSWER = {"node_version": "10.29.0", "optimizations": {"installed": True, "version": "10.9.0", "nic_mode": "rps"}}


class SingleNodeVersionTest(unittest.TestCase):
    def call(self, server):
        calls = []

        async def fake_versions(srv):
            calls.append(srv.id)
            return NODE_ANSWER

        async def fake_threshold(_db):
            return DEFAULT_OFFLINE_THRESHOLD

        with patch.object(system_router, "get_node_all_versions", fake_versions), \
                patch.object(system_router, "get_offline_threshold", fake_threshold):
            payload = asyncio.run(system_router.get_single_node_version(server.id, FakeDb(server), {}))
        return payload, calls

    def test_offline_by_collector_is_not_polled(self):
        payload, calls = self.call(make_server(last_seen_age_sec=DEFAULT_OFFLINE_THRESHOLD * 10))
        self.assertEqual(calls, [])
        self.assertEqual(payload["status"], "offline")
        self.assertIsNone(payload["version"])
        self.assertEqual(payload["optimizations"], {"installed": False, "version": None})

    def test_never_reached_with_error_is_not_polled(self):
        payload, calls = self.call(make_server(last_seen_age_sec=None, last_error="connect timeout"))
        self.assertEqual(calls, [])
        self.assertEqual(payload["status"], "offline")

    def test_online_node_is_polled(self):
        payload, calls = self.call(make_server(last_seen_age_sec=5))
        self.assertEqual(calls, [7])
        self.assertEqual(payload["status"], "online")
        self.assertEqual(payload["version"], "10.29.0")
        self.assertEqual(payload["optimizations"], NODE_ANSWER["optimizations"])

    def test_fresh_node_without_metrics_yet_is_polled(self):
        payload, calls = self.call(make_server(last_seen_age_sec=None))
        self.assertEqual(calls, [7])
        self.assertEqual(payload["status"], "online")


if __name__ == "__main__":
    unittest.main()
