"""Tests for the path trace verdict and node calls (services/loss_trace).

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Вывод читает mtr как человек: молчащие узлы и потери промежуточного узла,
которые дальше пропадают, — не проблема; место — первый узел, с которого
потери держатся до адреса.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import loss_trace  # noqa: E402
from app.services.loss_trace import (  # noqa: E402
    TraceError,
    TraceErrorCode,
    Verdict,
    analyze_trace,
    fetch_trace,
    start_trace,
)

TARGET = "184.107.64.85"


def hop(n: int, host: str | None, loss: float, received: int = 10) -> dict:
    return {"hop": n, "host": host, "loss_pct": loss, "received": 0 if host is None else received}


class VerdictTests(unittest.TestCase):
    def test_loss_starting_mid_path_and_carried_to_target(self):
        hops = [
            hop(1, "10.0.0.1", 0), hop(2, "95.163.208.1", 0), hop(3, None, 100),
            hop(4, "62.115.1.1", 60),  # ограничение ICMP: дальше пропадает
            hop(5, "62.115.1.2", 0), hop(6, "184.107.1.1", 26), hop(7, "184.107.2.2", 30), hop(8, TARGET, 28),
        ]
        result = analyze_trace(hops, TARGET, finished=True, rounds_done=15)
        self.assertEqual(result["verdict"], Verdict.LOSS_FROM)
        self.assertEqual((result["start_hop"], result["prev_hop"], result["problem_hops"]), (6, 5, [6, 7, 8]))
        self.assertEqual(result["dest_loss"], 28)

    def test_intermediate_loss_alone_means_clean(self):
        hops = [hop(1, "10.0.0.1", 0), hop(2, "62.115.1.1", 80), hop(3, TARGET, 0)]
        self.assertEqual(analyze_trace(hops, TARGET, True, 15)["verdict"], Verdict.CLEAN)

    def test_loss_only_at_target(self):
        hops = [hop(1, "10.0.0.1", 0), hop(2, "62.115.1.1", 0), hop(3, TARGET, 40)]
        result = analyze_trace(hops, TARGET, True, 15)
        self.assertEqual((result["start_hop"], result["prev_hop"]), (3, 2))

    def test_trace_never_reaching_target(self):
        hops = [hop(1, "10.0.0.1", 0), hop(2, "62.115.1.1", 0), hop(3, None, 100), hop(4, None, 100)]
        result = analyze_trace(hops, TARGET, True, 15)
        self.assertEqual((result["verdict"], result["last_hop"]), (Verdict.BROKEN_AFTER, 2))
        # Пока трасса идёт, обрыв ещё не вывод
        self.assertEqual(analyze_trace(hops, TARGET, False, 3)["verdict"], Verdict.WAITING)

    def test_nothing_answers(self):
        self.assertEqual(analyze_trace([hop(1, None, 100)], TARGET, True, 15)["verdict"], Verdict.NO_REPLIES)

    def test_early_rounds_are_preliminary(self):
        hops = [hop(1, "10.0.0.1", 0, 2), hop(2, TARGET, 50, 1)]
        self.assertTrue(analyze_trace(hops, TARGET, False, 2)["preliminary"])
        self.assertFalse(analyze_trace(hops, TARGET, True, 15)["preliminary"])


class NodeCallTests(unittest.IsolatedAsyncioTestCase):
    def server(self, caps=None):
        return SimpleNamespace(id=5, name="Вк мост 5", url="https://relay", node_capabilities=caps)

    def client(self, response=None, error=None):
        async def call(*_args, **_kwargs):
            if error:
                raise error
            return response

        return SimpleNamespace(post=call, get=call)

    def patched(self, client):
        return mock.patch.multiple(loss_trace, get_node_client=lambda server: client, node_auth_headers=lambda server: {})

    async def test_start_returns_node_trace_id(self):
        with self.patched(self.client(httpx.Response(200, json={"id": "abc"}))):
            self.assertEqual(await start_trace(self.server(), TARGET, 8449), "abc")

    async def test_start_errors_map_to_codes(self):
        cases = [
            (httpx.Response(404), TraceErrorCode.UNSUPPORTED),
            (httpx.Response(429, json={"detail": "busy"}), TraceErrorCode.BUSY),
            (httpx.Response(500), TraceErrorCode.UNREACHABLE),
        ]
        for response, code in cases:
            with self.subTest(code=code), self.patched(self.client(response)), self.assertRaises(TraceError) as ctx:
                await start_trace(self.server(), TARGET, 8449)
            self.assertEqual(ctx.exception.code, code)
        with self.patched(self.client(error=httpx.ConnectError("down"))), self.assertRaises(TraceError) as ctx:
            await start_trace(self.server(), TARGET, 8449)
        self.assertEqual(ctx.exception.code, TraceErrorCode.UNREACHABLE)

    async def test_restricted_node_is_not_called(self):
        with self.patched(self.client(error=AssertionError("must not call"))), self.assertRaises(TraceError) as ctx:
            await start_trace(self.server('{"system": "ro"}'), TARGET, 8449)
        self.assertEqual(ctx.exception.code, TraceErrorCode.DENIED)

    async def test_fetch_lost_trace(self):
        with self.patched(self.client(httpx.Response(404))), self.assertRaises(TraceError) as ctx:
            await fetch_trace(self.server(), "abc")
        self.assertEqual(ctx.exception.code, TraceErrorCode.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
