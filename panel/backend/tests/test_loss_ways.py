"""Tests for the per-address loss breakdown the panel fetches from a relay.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Подсказка на странице «Потери» открывается по наведению и читает разбивку прямо
у ноды. Нода бывает старой, закрытой правами или недоступной — на каждый случай
оператор должен увидеть причину, а не пустую карточку и не ошибку страницы.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.routers import loss as loss_router  # noqa: E402
from app.services import loss_overview  # noqa: E402
from app.services.loss_overview import WAYS_PATH, CheckStatus, ways_from_server  # noqa: E402

IP, PORT = "176.97.210.52", 8443
USED = {"local_ip": "146.185.243.157", "used": True, "loss_pct": 0.0, "rtt_ms": 41.5, "samples": 60,
        "answers": False, "span_sec": 120}
SPARE = {"local_ip": "10.0.0.97", "used": False, "loss_pct": 3.3, "rtt_ms": 38.0, "samples": 30,
         "answers": True, "span_sec": 60}
NOTHING = {"status": CheckStatus.UNREACHABLE, "ways": []}


def relay(capabilities: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=7, name="Вк 84 1", url="https://relay", node_capabilities=capabilities)


class FakeClient:
    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None):
        self.response, self.error = response, error
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None, **_):
        self.calls.append((url, params))
        if self.error is not None:
            raise self.error
        return self.response


class WaysFromServerTests(unittest.IsolatedAsyncioTestCase):
    async def ask(self, response: httpx.Response | None = None, error: Exception | None = None,
                  capabilities: str | None = None) -> dict:
        self.client = FakeClient(response, error)
        self.learn = mock.AsyncMock()
        with mock.patch.object(loss_overview, "get_node_client", return_value=self.client), \
                mock.patch.object(loss_overview, "node_auth_headers", return_value={}), \
                mock.patch.object(loss_overview, "learn_from_denial", self.learn):
            return await ways_from_server(relay(capabilities), IP, PORT)

    async def test_ways_are_passed_through(self):
        body = {"ip": IP, "port": PORT, "ways": [{**USED, "debug": "extra"}, SPARE]}

        result = await self.ask(httpx.Response(200, json=body))

        self.assertEqual(result, {"status": CheckStatus.OK, "ways": [USED, SPARE]})
        self.assertEqual(self.client.calls, [(f"https://relay{WAYS_PATH}", {"ip": IP, "port": PORT})])

    async def test_no_ways_yet_is_still_an_answer(self):
        result = await self.ask(httpx.Response(200, json={"ip": IP, "port": PORT, "ways": []}))
        self.assertEqual(result, {"status": CheckStatus.OK, "ways": []})

    async def test_old_node_is_unsupported(self):
        result = await self.ask(httpx.Response(404, json={"detail": "Not Found"}))
        self.assertEqual(result, {"status": CheckStatus.UNSUPPORTED, "ways": []})

    async def test_refusal_by_node_is_denied_and_remembered(self):
        denial = {"detail": "forbidden", "capability": "system"}

        result = await self.ask(httpx.Response(403, json=denial))

        self.assertEqual(result, {"status": CheckStatus.DENIED, "ways": []})
        self.learn.assert_awaited_once_with(7, 403, denial)

    async def test_known_restriction_denies_without_a_request(self):
        result = await self.ask(httpx.Response(200, json={"ways": [USED]}), capabilities='{"system": "no"}')

        self.assertEqual(result, {"status": CheckStatus.DENIED, "ways": []})
        self.assertEqual(self.client.calls, [])

    async def test_read_only_system_access_is_enough(self):
        result = await self.ask(httpx.Response(200, json={"ways": [USED]}),
                                capabilities='{"system": "ro", "haproxy": "ro"}')
        self.assertEqual(result["status"], CheckStatus.OK)

    async def test_network_error_is_unreachable(self):
        self.assertEqual(await self.ask(error=httpx.ConnectError("down")), NOTHING)
        self.assertEqual(await self.ask(error=httpx.ReadTimeout("slow")), NOTHING)

    async def test_garbage_answer_is_unreachable(self):
        for response in (httpx.Response(200, text="<html>"), httpx.Response(200, json=[USED]),
                         httpx.Response(200, json={"ways": "many"}), httpx.Response(502, json={"ways": [USED]})):
            with self.subTest(status=response.status_code, body=response.text[:20]):
                self.assertEqual(await self.ask(response), NOTHING)

    async def test_rows_that_are_not_objects_are_dropped(self):
        result = await self.ask(httpx.Response(200, json={"ways": [USED, "junk", None, 5]}))
        self.assertEqual(result["ways"], [USED])


class WaysRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = relay()
        self.find = mock.AsyncMock(return_value=self.server)
        self.fetch = mock.AsyncMock(return_value={"status": CheckStatus.OK, "ways": [USED]})
        for name, replacement in (("_active_server", self.find), ("ways_from_server", self.fetch)):
            patcher = mock.patch.object(loss_router, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.db = object()

    async def test_route_asks_the_relay_about_the_parsed_target(self):
        response = await loss_router.get_ways(server_id=7, target=f" {IP}:{PORT} ", db=self.db)

        self.assertEqual(response, {"status": CheckStatus.OK, "ways": [USED]})
        self.find.assert_awaited_once_with(7, self.db)
        self.fetch.assert_awaited_once_with(self.server, IP, PORT)

    async def test_route_rejects_a_bad_target(self):
        for bad in ("nonsense", "0.0.0.0:443", f"{IP}:99999"):
            with self.subTest(target=bad), self.assertRaises(HTTPException) as caught:
                await loss_router.get_ways(server_id=7, target=bad, db=self.db)
            self.assertEqual(caught.exception.status_code, 400)
        self.find.assert_not_awaited()
        self.fetch.assert_not_awaited()

    async def test_route_of_unknown_server_is_404(self):
        self.find.side_effect = HTTPException(status_code=404, detail="Server not found")

        with self.assertRaises(HTTPException) as caught:
            await loss_router.get_ways(server_id=99, target=f"{IP}:{PORT}", db=self.db)

        self.assertEqual(caught.exception.status_code, 404)
        self.fetch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
