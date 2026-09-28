"""Массовая доставка образа по SSH: чьи креды берутся и кто пропускается.

Креды из запроса — только для серверов без сохранённых: у сервера со своими
кредами они не должны перебиваться общим паролем из формы. Сервер без кредов
вообще пропускается с причиной, а не роняет весь запуск.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.models import Server  # noqa: E402
    from app.routers import node_image  # noqa: E402
except ImportError as e:  # рантайм панели не установлен
    raise unittest.SkipTest(f"node image router requires the panel runtime: {e}")


def make_server(sid: int, **creds) -> Server:
    return Server(id=sid, name=f"node-{sid}", url=f"https://10.0.0.{sid}:9100", position=sid, **creds)


class FakeDb:
    def __init__(self, servers: list[Server]):
        self._servers = servers
        self.commit = mock.AsyncMock()

    async def execute(self, _query):
        result = mock.MagicMock()
        result.scalars.return_value.all.return_value = self._servers
        return result


class BulkDeliverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = mock.MagicMock()
        self.manager.start.side_effect = lambda sid, name, target, tag: f"job-{sid}"
        for patcher in (
            mock.patch.object(node_image, "get_image_delivery_manager", return_value=self.manager),
            mock.patch.object(node_image, "_target_tag", return_value="latest"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def started_targets(self) -> dict[int, node_image.SSHTarget]:
        return {call.args[0]: call.args[2] for call in self.manager.start.call_args_list}

    async def test_form_creds_only_for_servers_without_saved(self):
        saved = make_server(1, ssh_password="own-pass", ssh_port=2222)
        bare = make_server(2)
        db = FakeDb([saved, bare])
        req = node_image.BulkDeliverRequest(server_ids=[1, 2], ssh_password="shared", ssh_port=22)

        result = await node_image.deliver_image_bulk(req, db)

        targets = self.started_targets()
        self.assertEqual((targets[1].password, targets[1].port), ("own-pass", 2222))
        self.assertEqual((targets[2].password, targets[2].port, targets[2].host), ("shared", 22, "10.0.0.2"))
        self.assertEqual(result["skipped"], [])
        db.commit.assert_not_awaited()

    async def test_servers_without_any_creds_are_skipped(self):
        db = FakeDb([make_server(1, ssh_private_key="KEY"), make_server(2)])
        req = node_image.BulkDeliverRequest(server_ids=[1, 2, 99])

        result = await node_image.deliver_image_bulk(req, db)

        self.assertEqual(result["started"], [{"server_id": 1, "job_id": "job-1"}])
        reasons = {s["server_id"]: s["reason"] for s in result["skipped"]}
        self.assertEqual(reasons, {2: "no_creds", 99: "not_found"})

    async def test_save_creds_persists_only_on_servers_that_used_form(self):
        saved = make_server(1, ssh_password="own-pass")
        bare = make_server(2)
        db = FakeDb([saved, bare])
        req = node_image.BulkDeliverRequest(server_ids=[1, 2], ssh_password="shared", save_creds=True)

        await node_image.deliver_image_bulk(req, db)

        self.assertEqual(saved.ssh_password, "own-pass")
        self.assertEqual(bare.ssh_password, "shared")
        db.commit.assert_awaited_once()

    async def test_non_root_user_is_skipped(self):
        db = FakeDb([make_server(1)])
        req = node_image.BulkDeliverRequest(server_ids=[1], ssh_user="ubuntu", ssh_password="x")

        result = await node_image.deliver_image_bulk(req, db)

        self.assertEqual(result["started"], [])
        self.assertEqual(result["skipped"][0]["reason"], "not_root")


if __name__ == "__main__":
    unittest.main()
