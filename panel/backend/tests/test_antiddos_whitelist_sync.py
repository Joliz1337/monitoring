"""Тесты доставки белого списка анти-DDoS на ноду (app/services/antiddos_manager.py).

Голый unittest, без сети: клиент ноды подменён.

Нода старой версии на сбой применения списка отвечает HTTP 200 с телом
{"success": false}. Панель, глядящая только на код ответа, считала такой список
доставленным, и он молча переставал обновляться на всём парке.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import antiddos_manager  # noqa: E402
from app.services.antiddos_manager import AntiDdosManager  # noqa: E402

NODE = SimpleNamespace(
    id=1, name="node-1", url="https://10.0.0.1:9100",
    pki_enabled=True, proxy_url=None, api_key=None, node_capabilities=None,
)


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeClient:
    def __init__(self, response: FakeResponse):
        self._response = response

    async def post(self, url: str, **kwargs):
        return self._response


async def sync(response: FakeResponse) -> tuple[bool, str]:
    with mock.patch.object(antiddos_manager, "get_node_client", return_value=FakeClient(response)):
        return await AntiDdosManager().sync_whitelist_to_node(NODE, ["198.51.100.1"])


class WhitelistSyncResultTest(unittest.IsolatedAsyncioTestCase):
    async def test_node_that_reports_failure_with_http_200_is_not_counted_as_synced(self):
        ok, message = await sync(FakeResponse(200, {"success": False, "message": "Argument list too long", "count": 0}))
        self.assertFalse(ok)
        self.assertIn("Argument list too long", message)

    async def test_node_that_applied_the_list_is_synced(self):
        ok, _ = await sync(FakeResponse(200, {"success": True, "message": "whitelist synced", "count": 1}))
        self.assertTrue(ok)

    async def test_http_200_without_a_json_body_is_still_a_success(self):
        ok, _ = await sync(FakeResponse(200, ValueError("not json")))
        self.assertTrue(ok)

    async def test_node_rejecting_the_list_with_an_error_status_is_a_failure(self):
        ok, message = await sync(FakeResponse(502, {"detail": "sync failed"}))
        self.assertFalse(ok)
        self.assertIn("502", message)


if __name__ == "__main__":
    unittest.main()
