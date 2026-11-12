"""Тесты обходов парка в коллекторе метрик (app/services/metrics_collector.py).

Голый unittest, без PostgreSQL и без сети: клиент ноды подменён.

Проверяется то, что ломается только на большом парке: вспомогательные обходы
(кэш HAProxy, проверка Xray) не должны отнимать слоты у сбора метрик и висеть
на недоступных нодах, а кривой ответ одной ноды не должен срывать цикл всем.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import metrics_collector  # noqa: E402
from app.services.latest_metrics import LatestMetrics  # noqa: E402
from app.services.metrics_collector import MetricsCollector  # noqa: E402

NOW = datetime(2026, 11, 12, 12, 0, 0)
HAPROXY_TIMEOUT = 15.0


def node(server_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=server_id, name=f"node-{server_id}", url=f"https://10.0.0.{server_id}:9100",
        pki_enabled=True, proxy_url=None, api_key=None, node_capabilities=None,
        has_xray_node=True, remnawave_nginx_detected=True,
        last_seen=None, node_version="10.35.0", tracked_ports=None,
    )


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeNodeClient:
    """Клиент ноды: на каждый путь — своё поведение, все обращения записываются."""

    def __init__(self, behaviour):
        self._behaviour = behaviour
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return await self._behaviour(url)


def patched_client(client: FakeNodeClient):
    return mock.patch.object(metrics_collector, "get_node_client", return_value=client)


async def unreachable(url: str):
    raise httpx.ConnectTimeout("no route")


async def answers_empty(url: str):
    return FakeResponse({})


class HaproxyRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def test_node_that_failed_its_last_metrics_poll_is_not_asked(self):
        collector = MetricsCollector()
        collector._node_failures[1] = 1
        client = FakeNodeClient(unreachable)
        with patched_client(client):
            await collector._cache_server_haproxy(node(1), HAPROXY_TIMEOUT)
        self.assertEqual(client.calls, [])

    async def test_unreachable_node_costs_one_attempt_not_one_per_endpoint(self):
        collector = MetricsCollector()
        client = FakeNodeClient(unreachable)
        with patched_client(client):
            await collector._cache_server_haproxy(node(1), HAPROXY_TIMEOUT)
        self.assertEqual(len(client.calls), 1)

    async def test_connect_timeout_is_short_while_read_timeout_stays(self):
        collector = MetricsCollector()
        client = FakeNodeClient(unreachable)
        with patched_client(client):
            await collector._cache_server_haproxy(node(1), HAPROXY_TIMEOUT)
        timeout = client.calls[0][1]["timeout"]
        self.assertLessEqual(timeout.connect, 3.0)
        self.assertEqual(timeout.read, HAPROXY_TIMEOUT)

    async def test_stuck_haproxy_refresh_does_not_take_metrics_slots(self):
        collector = MetricsCollector()
        never = asyncio.Event()

        async def behaviour(url: str):
            if "/api/haproxy/" in url:
                await never.wait()
            return FakeResponse({"cpu": {}})

        client = FakeNodeClient(behaviour)
        with patched_client(client):
            stuck = [
                asyncio.create_task(collector._cache_server_haproxy(node(i), HAPROXY_TIMEOUT))
                for i in range(2, collector.HTTP_CONCURRENCY + 12)
            ]
            await asyncio.sleep(0.05)
            try:
                metrics, error = await asyncio.wait_for(collector._fetch_server_metrics(node(1)), 2.0)
            finally:
                for task in stuck:
                    task.cancel()
                await asyncio.gather(*stuck, return_exceptions=True)
        self.assertIsNone(error)
        self.assertIsNotNone(metrics)


class XrayProbeTest(unittest.IsolatedAsyncioTestCase):
    async def test_node_that_failed_its_last_metrics_poll_keeps_its_flags(self):
        collector = MetricsCollector()
        collector._node_failures[1] = 2
        client = FakeNodeClient(unreachable)
        with patched_client(client):
            result = await collector._probe_xray(node(1), "/opt/remnawave")
        self.assertEqual(client.calls, [])
        self.assertEqual(result, (1, True, True))

    async def test_unreachable_node_is_probed_with_a_short_connect_timeout(self):
        collector = MetricsCollector()
        client = FakeNodeClient(unreachable)
        with patched_client(client):
            await collector._probe_xray(node(1), "/opt/remnawave")
        self.assertEqual(len(client.calls), 1)
        self.assertLessEqual(client.calls[0][1]["timeout"].connect, 3.0)


class CycleBatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Разбор ответа кладёт его в хранилище процесса, общее для всех тестов
        store_patch = mock.patch.object(metrics_collector, "get_latest_metrics", return_value=LatestMetrics())
        store_patch.start()
        self.addCleanup(store_patch.stop)

    async def test_malformed_answer_of_one_node_does_not_cancel_the_cycle(self):
        collector = MetricsCollector()
        broken = {"cpu": None, "collected_at": 1794483600.0}
        healthy = {"cpu": {"load_avg_1": 0.5}, "collected_at": 1794483600.0}

        batch = await collector._build_cycle_batch(
            [node(1), node(2)], [(broken, None), (healthy, None)], NOW,
        )

        self.assertEqual([s["server_id"] for s in batch.snapshots], [2])
        updates = {u["id"]: u for u in batch.server_updates}
        self.assertEqual(updates[2]["last_seen"], NOW)
        self.assertNotIn("last_seen", updates[1])
        self.assertTrue(updates[1]["last_error"])

    async def test_node_with_malformed_answer_is_not_reported_as_recovered(self):
        collector = MetricsCollector()
        collector._down_servers.add(1)
        collector._open_downtime.add(1)

        batch = await collector._build_cycle_batch(
            [node(1)], [({"memory": None, "collected_at": 1794483600.0}, None)], NOW,
        )

        self.assertEqual(batch.recovered_ids, [])
        self.assertEqual(batch.downtime_over, [])
        self.assertIn(1, collector._down_servers)


class CycleOverrunTest(unittest.TestCase):
    def test_cycle_longer_than_the_interval_is_logged_once_not_every_cycle(self):
        collector = MetricsCollector()
        with self.assertLogs(metrics_collector.logger, level="WARNING") as captured:
            for _ in range(5):
                collector._note_cycle_duration(collector._collect_interval + 4.0)
        self.assertEqual(len(captured.records), 1)

    def test_cycle_within_the_interval_is_silent(self):
        collector = MetricsCollector()
        with self.assertNoLogs(metrics_collector.logger, level="WARNING"):
            collector._note_cycle_duration(collector._collect_interval / 2)


if __name__ == "__main__":
    unittest.main()
