"""Тесты тяжёлого списка серверов для дашборда (app/routers/servers.py).

Голый unittest, без PostgreSQL: сборка списка подменена.

На большом парке список собирается дольше своего TTL. Кэш, помеченный временем
начала сборки, рождался протухшим, и каждый запрос пересобирал список заново.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import gzip
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.dialects import postgresql  # noqa: E402

from app.models import Server  # noqa: E402
from app.routers import servers  # noqa: E402
from app.services.latest_metrics import LatestMetrics  # noqa: E402
from app.services.metrics_rates import enrich_metrics_with_speeds  # noqa: E402

SLOW_BUILD_SECONDS = servers._LIST_CACHE_TTL + 2.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ListCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        servers._invalidate_list_cache()
        self.clock = FakeClock()
        self.builds = 0
        clock_patch = mock.patch.object(servers, "_monotonic", self.clock)
        build_patch = mock.patch.object(servers, "_build_servers_list", self._slow_build)
        clock_patch.start()
        build_patch.start()
        self.addCleanup(clock_patch.stop)
        self.addCleanup(build_patch.stop)
        self.addCleanup(servers._invalidate_list_cache)

    async def _slow_build(self, db, include_metrics: bool) -> dict:
        self.builds += 1
        self.clock.now += SLOW_BUILD_SECONDS
        return {"count": 1, "servers": [{"id": 1, "build": self.builds}]}

    async def test_list_built_slower_than_ttl_is_still_served_from_cache(self):
        await servers._get_cached_servers_list(db=None)
        await servers._get_cached_servers_list(db=None)
        self.assertEqual(self.builds, 1)

    async def test_list_is_rebuilt_after_ttl_since_the_build_finished(self):
        await servers._get_cached_servers_list(db=None)
        self.clock.now += servers._LIST_CACHE_TTL + 0.1
        cached = await servers._get_cached_servers_list(db=None)
        self.assertEqual(self.builds, 2)
        self.assertEqual(json.loads(cached.body)["servers"][0]["build"], 2)

    async def test_compressed_copy_matches_the_plain_body(self):
        cached = await servers._get_cached_servers_list(db=None)
        self.assertEqual(gzip.decompress(cached.gzipped), cached.body)


class ListResponseTest(unittest.TestCase):
    def setUp(self) -> None:
        body = json.dumps({"count": 0, "servers": []}).encode()
        self.cached = servers.CachedList(body=body, gzipped=gzip.compress(body), etag='"abc"', built_at=0.0)

    def test_browser_that_accepts_gzip_gets_the_precompressed_body(self):
        response = servers._list_response(self.cached, accept_encoding="gzip, deflate, br", if_none_match=None)
        self.assertEqual(response.headers["content-encoding"], "gzip")
        self.assertEqual(response.body, self.cached.gzipped)

    def test_client_without_gzip_gets_the_plain_body(self):
        response = servers._list_response(self.cached, accept_encoding="", if_none_match=None)
        self.assertNotIn("content-encoding", response.headers)
        self.assertEqual(response.body, self.cached.body)

    def test_unchanged_list_answers_304_without_a_body(self):
        response = servers._list_response(self.cached, accept_encoding="gzip", if_none_match='"abc"')
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.body, b"")


class SnapshotLookupSelectionTest(unittest.TestCase):
    def test_only_nodes_without_their_own_rates_need_the_last_snapshot(self):
        latest = {
            1: {"live_rates": {"window_sec": 1.0}},
            2: {"network": {}},
        }
        self.assertEqual(servers._ids_needing_snapshot(latest), [2])


def server(server_id: int, **fields) -> Server:
    return Server(
        id=server_id, name=f"node-{server_id}", url=f"https://10.0.0.{server_id}:9100",
        position=server_id, is_active=True, pki_enabled=True, uses_shared_cert=True, **fields,
    )


def new_agent_answer() -> dict:
    return {
        "live_rates": {"window_sec": 1.0},
        "network": {"total": {"rx_bytes_per_sec": 111.0, "tx_bytes_per_sec": 222.0}},
    }


def old_agent_answer() -> dict:
    """Агент без посекундных скоростей: их панель дописывает из последнего снапшота."""
    return {
        "network": {
            "total": {"rx_bytes": 4000, "tx_bytes": 1000},
            "interfaces": [
                {"name": "eth0", "rx_bytes": 3000, "tx_bytes": 1000},
                {"name": "eth1", "rx_bytes": 1000, "tx_bytes": 0},
            ],
        },
        "disk": {"io": {"sda": {"read_bytes": 10, "write_bytes": 30}}},
    }


SNAPSHOT = SimpleNamespace(
    net_rx_bytes_per_sec=800.0, net_tx_bytes_per_sec=200.0,
    disk_read_bytes_per_sec=5.0, disk_write_bytes_per_sec=15.0,
)


class RecordingSession:
    """Сессия без базы: отдаёт серверы и запоминает текст запросов."""

    def __init__(self, fleet: list[Server]) -> None:
        self._fleet = fleet
        self.queries: list[str] = []

    async def execute(self, statement, params=None):
        self.queries.append(str(statement.compile(dialect=postgresql.dialect())))
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self._fleet))


class ListMetricsTest(unittest.IsolatedAsyncioTestCase):
    """Последние ответы нод список берёт из памяти процесса, а не из `servers.last_metrics`."""

    async def asyncSetUp(self) -> None:
        self.latest = LatestMetrics()
        self.snapshots: dict[int, SimpleNamespace] = {}

        async def snapshots_bulk(server_ids, db) -> dict:
            return {sid: snapshot for sid, snapshot in self.snapshots.items() if sid in server_ids}

        for name, replacement in (
            ("get_latest_metrics", mock.Mock(return_value=self.latest)),
            ("get_offline_threshold", mock.AsyncMock(return_value=60)),
            ("get_latest_snapshots_bulk", snapshots_bulk),
            ("get_traffic_totals_bulk", mock.AsyncMock(return_value={})),
        ):
            patcher = mock.patch.object(servers, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def listed(self, *fleet: Server) -> dict[int, dict]:
        self.db = RecordingSession(list(fleet))
        payload = await servers._build_servers_list(self.db, include_metrics=True)
        return {item["id"]: item for item in payload["servers"]}

    async def test_answer_of_a_node_comes_from_memory(self):
        self.latest.put(1, new_agent_answer())

        listed = await self.listed(server(1, last_seen=datetime.now(timezone.utc)))

        self.assertEqual(listed[1]["metrics"], new_agent_answer())
        self.assertEqual(listed[1]["status"], "online")

    async def test_old_agent_gets_snapshot_speeds_and_the_shared_answer_stays_intact(self):
        # Ответ в памяти один на всех читателей: дописанные в него скорости
        # увидели бы алерты, белые списки и следующая сборка списка
        self.latest.put(2, old_agent_answer())
        self.snapshots[2] = SNAPSHOT

        listed = await self.listed(server(2))

        self.assertEqual(listed[2]["metrics"]["network"]["total"]["rx_bytes_per_sec"], 800.0)
        self.assertEqual(listed[2]["metrics"]["network"]["interfaces"][0]["rx_bytes_per_sec"], 600.0)
        self.assertEqual(listed[2]["metrics"]["disk"]["io"]["sda"]["write_bytes_per_sec"], 15.0)
        self.assertEqual(self.latest.get(2), old_agent_answer())

    async def test_answer_with_its_own_speeds_goes_into_the_list_without_a_copy(self):
        # Копия ответа каждой ноды на каждую сборку — мегабайты аллокаций на большом парке
        self.latest.put(1, new_agent_answer())

        listed = await self.listed(server(1))

        self.assertIs(listed[1]["metrics"], self.latest.get(1))

    async def test_list_does_not_read_saved_answers_from_the_database(self):
        await self.listed(server(1))

        self.assertTrue(self.db.queries)
        self.assertFalse([query for query in self.db.queries if "last_metrics" in query])

    async def test_metrics_match_what_the_saved_json_used_to_give(self):
        def from_saved_json(answer: dict) -> dict:
            return json.loads(json.dumps(answer))

        self.latest.put(1, new_agent_answer())
        self.latest.put(2, old_agent_answer())
        self.snapshots[2] = SNAPSHOT

        listed = await self.listed(server(1), server(2))

        expected = {
            1: from_saved_json(new_agent_answer()),
            2: enrich_metrics_with_speeds(from_saved_json(old_agent_answer()), SNAPSHOT),
        }
        for server_id, metrics in expected.items():
            self.assertEqual(json.dumps(listed[server_id]["metrics"]), json.dumps(metrics))

    async def test_node_without_an_answer_has_no_metrics(self):
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)

        listed = await self.listed(server(1), server(2, last_seen=long_ago))

        self.assertEqual([listed[1]["metrics"], listed[2]["metrics"]], [None, None])
        self.assertEqual([listed[1]["status"], listed[2]["status"]], ["loading", "offline"])


class DeletedServerTest(unittest.IsolatedAsyncioTestCase):
    async def test_answer_of_a_deleted_server_does_not_stay_in_memory(self):
        latest = LatestMetrics()
        latest.put(7, new_agent_answer())
        db = mock.AsyncMock()
        db.execute.return_value = mock.Mock(scalar_one_or_none=mock.Mock(return_value=server(7)))

        with mock.patch.object(servers, "get_latest_metrics", return_value=latest), \
                mock.patch.object(servers, "get_blocklist_manager"):
            await servers.delete_server(7, db=db, _={})

        self.assertIsNone(latest.get(7))


if __name__ == "__main__":
    unittest.main()
