"""Тесты данных о нодах в разделе профилей HAProxy (app/routers/haproxy_profiles.py).

Голый unittest, без PostgreSQL и без сети: сессия подменена.

Число ядер, скорость сетевой карты и загрузка серверов профиля берутся из
последнего ответа ноды. Панель держит его в памяти, поэтому поднимать ради них
~18 КБ JSON на сервер из `servers.last_metrics` и разбирать их незачем.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.dialects import postgresql  # noqa: E402

from app.models import HAProxyConfigProfile, MetricsSnapshot, Server  # noqa: E402
from app.routers import haproxy_profiles  # noqa: E402
from app.services.latest_metrics import LatestMetrics  # noqa: E402


def server(server_id: int, **fields) -> Server:
    return Server(id=server_id, name=f"node-{server_id}", url=f"https://10.0.0.{server_id}:9100", **fields)


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

    fetchall = all


class FakeSession:
    """Сессия без базы: отдаёт заготовленные строки по сущности запроса и запоминает его текст."""

    def __init__(self, rows: dict[type, list]) -> None:
        self._rows = rows
        self.queries: list[str] = []

    async def execute(self, statement) -> FakeResult:
        self.queries.append(str(statement.compile(dialect=postgresql.dialect())))
        entity = statement.column_descriptions[0]["entity"]
        return FakeResult(self._rows.get(entity, []), [column.name for column in statement.selected_columns])

    async def commit(self) -> None:
        pass


class NodeMetricsCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.latest = LatestMetrics()
        store_patch = mock.patch.object(haproxy_profiles, "get_latest_metrics", return_value=self.latest)
        store_patch.start()
        self.addCleanup(store_patch.stop)


class ServerCoresTest(NodeMetricsCase):
    async def test_cores_of_backend_servers_come_from_memory(self):
        self.latest.put(1, {"cpu": {"cores_logical": 8}})
        db = FakeSession({Server: [server(1), server(2)]})
        request = haproxy_profiles.ServerCoresRequest(profile_id=1, addresses=["10.0.0.1", "10.0.0.2", "10.0.0.9"])

        cores = await haproxy_profiles.get_server_cores(request, db=db, _=None)

        self.assertEqual(cores, {"10.0.0.1": 8})


class AvailableServersTest(NodeMetricsCase):
    async def test_nic_speed_hint_comes_from_memory(self):
        nic = {"name": "eth0", "speed_mbps": 1000, "rx_bytes": 10, "tx_bytes": 0, "is_virtual": False}
        self.latest.put(1, {"network": {"interfaces": [nic]}})
        db = FakeSession({Server: [server(1, link_capacity_mbit=500), server(2)]})

        listed = await haproxy_profiles.get_available_servers(db=db, _=None)

        self.assertEqual({item["id"]: item["nic_speed_mbps"] for item in listed}, {1: 1000, 2: None})
        self.assertEqual(listed[0]["link_capacity_mbit"], 500)


class ProfileServersStatusTest(NodeMetricsCase):
    PROFILE_ID = 5

    def profile_fleet(self) -> FakeSession:
        return FakeSession({
            HAProxyConfigProfile: [HAProxyConfigProfile(id=self.PROFILE_ID, name="relays")],
            Server: [
                server(1, active_haproxy_profile_id=self.PROFILE_ID),
                server(2, active_haproxy_profile_id=self.PROFILE_ID),
            ],
            MetricsSnapshot: [MetricsSnapshot(server_id=1, net_rx_bytes_per_sec=100.0, net_tx_bytes_per_sec=50.0)],
        })

    async def test_load_of_profile_servers_comes_from_memory(self):
        self.latest.put(1, {
            "cpu": {"usage_percent": 12.5, "load_avg_1": 0.5, "cores_logical": 8},
            "memory": {"ram": {"percent": 40.0}},
        })

        first, second = await haproxy_profiles.get_servers_status(self.PROFILE_ID, db=self.profile_fleet(), _=None)

        self.assertEqual(
            first["metrics"],
            {"cpu": 12.5, "ram": 40.0, "net_rx": 100.0, "net_tx": 50.0, "la1": 0.5, "cores": 8},
        )
        self.assertIsNone(second["metrics"])

    async def test_saved_answers_are_not_read_from_the_database(self):
        db = self.profile_fleet()
        request = haproxy_profiles.ServerCoresRequest(profile_id=self.PROFILE_ID, addresses=["10.0.0.1"])

        await haproxy_profiles.get_server_cores(request, db=db, _=None)
        await haproxy_profiles.get_available_servers(db=db, _=None)
        await haproxy_profiles.get_servers_status(self.PROFILE_ID, db=db, _=None)

        self.assertTrue(db.queries)
        self.assertFalse([query for query in db.queries if "last_metrics" in query])


if __name__ == "__main__":
    unittest.main()
