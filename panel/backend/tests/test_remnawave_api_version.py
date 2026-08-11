"""Совместимость клиента Remnawave с панелями 2.x и 3.x.

В 3.x пользователь потерял uuid и адресуется числовым id, а часть путей переехала
(ip-control → connections). Клиент определяет версию сам, поэтому проверяем и сам
детект, и то, что по определённой версии выбираются правильные путь и тело запроса.
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # локальный прогон без установленного рантайма панели
    from app.services import remnawave_api
    from app.services.remnawave_api import (
        API_V2,
        API_V3,
        RemnawaveAPI,
        RemnawaveAPIError,
        api_version_from_nodes,
        api_version_from_users,
    )
except ImportError as e:  # pragma: no cover
    raise unittest.SkipTest(f"remnawave client requires the panel runtime: {e}")


class FakeAPI(RemnawaveAPI):
    """Клиент с записью запросов вместо сети."""

    def __init__(self, responses: dict = None, version: int = None):
        super().__init__("https://panel.example", "token")
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.responses = responses or {}
        if version:
            remnawave_api._api_version_by_url[self.base_url] = version

    async def _request(self, method, endpoint, params=None, json_body=None, retries=3):
        self.calls.append((method, endpoint, params, json_body))
        if endpoint not in self.responses:
            raise RemnawaveAPIError("Not found", status_code=404)
        return self.responses[endpoint]


class ApiVersionDetectionTest(unittest.TestCase):
    def setUp(self):
        remnawave_api._api_version_by_url.clear()

    def test_node_with_numeric_id_is_v3(self):
        self.assertEqual(api_version_from_nodes([{"uuid": "n-1", "id": 7}]), API_V3)

    def test_node_without_id_is_v2(self):
        self.assertEqual(api_version_from_nodes([{"uuid": "n-1", "name": "de-1"}]), API_V2)

    def test_no_nodes_tells_nothing(self):
        self.assertIsNone(api_version_from_nodes([]))

    def test_user_with_uuid_is_v2(self):
        self.assertEqual(api_version_from_users([{"id": 5, "uuid": "u-1"}]), API_V2)

    def test_user_without_uuid_is_v3(self):
        self.assertEqual(api_version_from_users([{"id": 5, "shortUuid": "abc"}]), API_V3)

    def test_get_all_nodes_remembers_version(self):
        api = FakeAPI({"/api/nodes": {"response": [{"uuid": "n-1", "id": 3}]}})
        asyncio.run(api.get_all_nodes())
        self.assertEqual(asyncio.run(api.get_api_version()), API_V3)

    def test_probe_falls_back_to_users_when_no_nodes(self):
        api = FakeAPI({
            "/api/nodes": {"response": []},
            "/api/users": {"response": {"users": [{"id": 1, "uuid": "u-1"}], "total": 1}},
        })
        self.assertEqual(asyncio.run(api.get_api_version()), API_V2)

    def test_empty_panel_is_treated_as_v3(self):
        api = FakeAPI({
            "/api/nodes": {"response": []},
            "/api/users": {"response": {"users": [], "total": 0}},
        })
        self.assertEqual(asyncio.run(api.get_api_version()), API_V3)


class VersionedPathsTest(unittest.TestCase):
    def setUp(self):
        remnawave_api._api_version_by_url.clear()

    def test_ip_fetch_uses_connections_on_v3(self):
        api = FakeAPI({"/api/connections/by-node/n-1": {"response": {"jobId": "j-1"}}}, version=API_V3)
        self.assertEqual(asyncio.run(api.fetch_users_ips("n-1")), "j-1")
        self.assertEqual(api.calls[0][1], "/api/connections/by-node/n-1")

    def test_ip_fetch_uses_ip_control_on_v2(self):
        api = FakeAPI({"/api/ip-control/fetch-users-ips/n-1": {"response": {"jobId": "j-2"}}}, version=API_V2)
        self.assertEqual(asyncio.run(api.fetch_users_ips("n-1")), "j-2")
        self.assertEqual(api.calls[0][1], "/api/ip-control/fetch-users-ips/n-1")

    def test_missing_new_path_falls_back_to_old_one(self):
        """Панель обновили/откатили под нами — кэш версии чинится по 404."""
        api = FakeAPI({"/api/ip-control/fetch-users-ips/n-1": {"response": {"jobId": "j-3"}}}, version=API_V3)
        self.assertEqual(asyncio.run(api.fetch_users_ips("n-1")), "j-3")
        self.assertEqual([c[1] for c in api.calls], [
            "/api/connections/by-node/n-1",
            "/api/ip-control/fetch-users-ips/n-1",
        ])
        self.assertEqual(asyncio.run(api.get_api_version()), API_V2)

    def test_job_result_path_follows_version(self):
        api = FakeAPI({"/api/connections/by-node/j-1": {"response": {"isCompleted": True}}}, version=API_V3)
        asyncio.run(api.get_fetch_users_ips_result("j-1"))
        self.assertEqual(api.calls[0][1], "/api/connections/by-node/j-1")


class UserIdentityTest(unittest.TestCase):
    def setUp(self):
        remnawave_api._api_version_by_url.clear()

    def test_hwid_wipe_sends_user_id_on_v3(self):
        api = FakeAPI({"/api/hwid/devices/delete-all": {"response": {"total": 0}}}, version=API_V3)
        asyncio.run(api.delete_all_user_hwid_devices(42, "u-1"))
        self.assertEqual(api.calls[0][3], {"userId": 42})

    def test_hwid_wipe_sends_uuid_on_v2(self):
        api = FakeAPI({"/api/hwid/devices/delete-all": {"response": {"total": 0}}}, version=API_V2)
        asyncio.run(api.delete_all_user_hwid_devices(42, "u-1"))
        self.assertEqual(api.calls[0][3], {"userUuid": "u-1"})

    def test_hwid_wipe_without_uuid_on_v2_is_an_error(self):
        api = FakeAPI({"/api/hwid/devices/delete-all": {"response": {"total": 0}}}, version=API_V2)
        with self.assertRaises(RemnawaveAPIError):
            asyncio.run(api.delete_all_user_hwid_devices(42))

    def test_traffic_stats_addressed_by_id_on_v3(self):
        api = FakeAPI({"/api/bandwidth-stats/users/42": {"response": {"series": [{"total": 512}]}}}, version=API_V3)
        self.assertEqual(asyncio.run(api.get_user_traffic_bytes(42, "u-1")), 512)
        self.assertEqual(api.calls[0][1], "/api/bandwidth-stats/users/42")

    def test_traffic_stats_prefer_legacy_window_on_v2(self):
        api = FakeAPI({"/api/bandwidth-stats/users/u-1/legacy": {"response": [{"total": 128}]}}, version=API_V2)
        self.assertEqual(asyncio.run(api.get_user_traffic_bytes(42, "u-1")), 128)
        self.assertEqual(api.calls[0][1], "/api/bandwidth-stats/users/u-1/legacy")

    def test_traffic_stats_need_uuid_on_v2(self):
        api = FakeAPI(version=API_V2)
        self.assertIsNone(asyncio.run(api.get_user_traffic_bytes(42)))
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
