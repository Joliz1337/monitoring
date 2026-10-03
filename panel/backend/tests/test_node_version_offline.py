"""Версия ноды со страниц «Обновления»/«Системные оптимизации»: ноду, которую
коллектор метрик считает офлайн, панель не опрашивает — отдаёт «offline» сразу.
Плюс общий кэш файлов оптимизаций с GitHub для массового применения.

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
        self.committed = False

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self._server)

    async def commit(self):
        self.committed = True


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
        db = FakeDb(server)
        calls = []

        async def fake_versions(srv):
            calls.append(srv.id)
            self.committed_before_poll = db.committed
            return NODE_ANSWER

        async def fake_threshold(_db):
            return DEFAULT_OFFLINE_THRESHOLD

        with patch.object(system_router, "get_node_all_versions", fake_versions), \
                patch.object(system_router, "get_offline_threshold", fake_threshold):
            payload = asyncio.run(system_router.get_single_node_version(server.id, db, {}))
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

    def test_pool_connection_released_before_polling(self):
        # После «Обновить все» страница перечитывает все ноды разом: коннект
        # пула, провисевший весь запрос к ноде, выгреб бы пул БД
        self.call(make_server(last_seen_age_sec=5))
        self.assertTrue(self.committed_before_poll)


class OptimizationsGithubCacheTest(unittest.TestCase):
    """«Обновить все» шлёт применение на все ноды разом — файлы с GitHub
    должны скачиваться один раз на волну, а не по 12 запросов на ноду."""

    def setUp(self):
        system_router._optimizations_cache.clear()
        self.fetches = []
        self.complete = True

    def run_wave(self, profiles, base="https://raw.example/main/configs"):
        async def fake_fetch(profile):
            self.fetches.append(profile)
            await asyncio.sleep(0.01)
            return {"profile": profile}, self.complete

        async def wave():
            return await asyncio.gather(*(system_router.get_optimizations_from_github(p) for p in profiles))

        # Свой лок на каждый asyncio.run: занятый лок привязывается к циклу событий
        with patch.object(system_router, "_fetch_optimizations_from_github", fake_fetch), \
                patch.object(system_router, "_optimizations_lock", asyncio.Lock()), \
                patch.object(system_router.update_channel, "github_configs_base", lambda: base):
            return asyncio.run(wave())

    def test_concurrent_wave_fetches_once_per_profile(self):
        results = self.run_wave(["vpn"] * 50 + ["panel"] * 10)
        self.assertEqual(sorted(self.fetches), ["panel", "vpn"])
        self.assertEqual(results[0], {"profile": "vpn"})
        self.assertEqual(results[-1], {"profile": "panel"})

    def test_failed_fetch_is_not_repeated_by_waiters(self):
        self.complete = False
        self.run_wave(["vpn"] * 20)
        self.assertEqual(self.fetches, ["vpn"])

    def test_channel_switch_bypasses_cache(self):
        self.run_wave(["vpn"], base="https://raw.example/main/configs")
        self.run_wave(["vpn"], base="https://raw.example/dev/configs")
        self.assertEqual(self.fetches, ["vpn", "vpn"])


if __name__ == "__main__":
    unittest.main()
