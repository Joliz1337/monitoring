"""Tests for the on-demand path trace (services/path_trace).

Runnable with plain stdlib:  python -m unittest discover -s node/tests

mtr на Windows не запустить — раунды подаются готовым JSON в формате `mtr -j`;
DNS-клиент проверяется на собранном вручную пакете с указателем сжатия.
"""

import asyncio
import json
import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.host_executor import ExecuteResult  # noqa: E402
from app.services.path_trace import (  # noqa: E402
    MAX_ACTIVE_TRACES,
    ROUNDS,
    AsnInfo,
    AsnResolver,
    PathTrace,
    PathTraceManager,
    TraceBusyError,
    TraceState,
    build_txt_query,
    mtr_round_command,
    origin_query_name,
    parse_round,
    parse_txt_answer,
)


def mtr_json(*hubs: tuple) -> str:
    """Раунд в формате `mtr -j`: (номер, адрес, ответил, задержка)."""
    return json.dumps({"report": {"mtr": {"dst": "184.107.64.85", "tests": 1}, "hubs": [
        {"count": hop, "host": host, "Loss%": 0.0 if ok else 100.0, "Snt": 1,
         "Last": rtt if ok else 0.0, "Avg": rtt if ok else 0.0, "Best": 0.0, "Wrst": 0.0, "StDev": 0.0}
        for hop, host, ok, rtt in hubs
    ]}})


def ok(stdout: str = "") -> ExecuteResult:
    return ExecuteResult(success=True, exit_code=0, stdout=stdout, stderr="", execution_time_ms=1)


def failed(stderr: str) -> ExecuteResult:
    return ExecuteResult(success=False, exit_code=1, stdout="", stderr=stderr, execution_time_ms=1)


class RoundTests(unittest.TestCase):
    def test_parse_round_marks_silent_hops(self):
        output = mtr_json((1, "10.0.0.1", True, 0.4), (2, "???", False, 0.0), (3, "184.107.64.85", True, 136.2))
        self.assertEqual(parse_round(output), [(1, "10.0.0.1", 0.4), (2, None, None), (3, "184.107.64.85", 136.2)])

    def test_count_as_string_like_older_mtr(self):
        output = json.dumps({"report": {"hubs": [{"count": "1", "host": "10.0.0.1", "Loss%": 0.0, "Last": 1.5}]}})
        self.assertEqual(parse_round(output), [(1, "10.0.0.1", 1.5)])

    def test_rounds_accumulate_into_loss_and_latency(self):
        trace = PathTrace(id="t", ip="184.107.64.85", port=8449, created_at=0)
        trace.record_round(parse_round(mtr_json((1, "10.0.0.1", True, 1.0), (2, "184.107.64.85", True, 130.0))))
        trace.record_round(parse_round(mtr_json((1, "10.0.0.1", True, 3.0), (2, "184.107.64.85", False, 0.0))))
        view = trace.to_dict(lambda ip: AsnInfo("AS32613", "iWeb") if ip == "184.107.64.85" else None)
        first, last = view["hops"]
        self.assertEqual((first["loss_pct"], first["avg_ms"], first["best_ms"], first["worst_ms"]), (0.0, 2.0, 1.0, 3.0))
        self.assertEqual((last["host"], last["asn"], last["as_name"], last["loss_pct"]), ("184.107.64.85", "AS32613", "iWeb", 50.0))
        self.assertEqual((view["rounds_done"], view["rounds_total"]), (2, ROUNDS))

    def test_command_is_tcp_on_the_port(self):
        self.assertEqual(
            mtr_round_command("184.107.64.85", 8449), "mtr -n -T -P 8449 -c 1 -G 1 -U 30 -j 184.107.64.85",
        )


class DnsTests(unittest.TestCase):
    def test_query_layout(self):
        packet = build_txt_query("AS1299.asn.cymru.com", 0x1234)
        self.assertEqual(packet[:4], b"\x12\x34\x01\x00")
        self.assertIn(b"\x06AS1299\x03asn\x05cymru\x03com\x00\x00\x10\x00\x01", packet)

    def test_answer_with_compressed_name_and_split_text(self):
        query = build_txt_query("AS1299.asn.cymru.com", 7)
        text = b"1299 | EU | ripencc | 2001-11-15 | TWELVE99 Arelion, fka Telia Carrier, SE"
        rdata = bytes([40]) + text[:40] + bytes([len(text) - 40]) + text[40:]
        answer = b"\xc0\x0c" + struct.pack(">HHIH", 16, 1, 300, len(rdata)) + rdata
        response = struct.pack(">HHHHHH", 7, 0x8180, 1, 1, 0, 0) + query[12:] + answer
        self.assertEqual(parse_txt_answer(response, 7), [text.decode()])
        self.assertEqual(parse_txt_answer(response, 8), [])

    def test_origin_names(self):
        self.assertEqual(origin_query_name("184.107.64.85"), "85.64.107.184.origin.asn.cymru.com")
        self.assertTrue(origin_query_name("2001:4860::8888").endswith(".origin6.asn.cymru.com"))
        self.assertIsNone(origin_query_name("10.0.0.1"))


class AsnResolverTests(unittest.TestCase):
    def test_origin_then_name_and_both_cached(self):
        answers = {
            "85.64.107.184.origin.asn.cymru.com": ["32613 | 184.107.0.0/16 | CA | arin | 2010-08-13"],
            "AS32613.asn.cymru.com": ["32613 | CA | arin | 2004-04-13 | IWEB-AS, CA"],
            "84.64.107.184.origin.asn.cymru.com": ["32613 | 184.107.0.0/16 | CA | arin | 2010-08-13"],
        }
        calls = []

        async def txt(name):
            calls.append(name)
            return answers.get(name, [])

        resolver = AsnResolver(txt)
        for ip in ("184.107.64.85", "184.107.64.85", "184.107.64.84", "10.0.0.1"):
            asyncio.run(resolver.resolve(ip))
        self.assertEqual(resolver.known("184.107.64.85"), AsnInfo("AS32613", "IWEB-AS, CA"))
        self.assertEqual(resolver.known("184.107.64.84"), AsnInfo("AS32613", "IWEB-AS, CA"))
        self.assertIsNone(resolver.known("10.0.0.1"))
        self.assertEqual(calls.count("AS32613.asn.cymru.com"), 1)
        self.assertEqual(calls.count("85.64.107.184.origin.asn.cymru.com"), 1)

    def test_dns_failure_is_retried_next_round(self):
        outcomes = [asyncio.TimeoutError(), ["32613 | 184.107.0.0/16 | CA | arin"], ["32613 | CA | arin | x | IWEB-AS, CA"]]

        async def txt(name):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        resolver = AsnResolver(txt)
        asyncio.run(resolver.resolve("184.107.64.85"))
        self.assertIsNone(resolver.known("184.107.64.85"))
        asyncio.run(resolver.resolve("184.107.64.85"))
        self.assertEqual(resolver.known("184.107.64.85"), AsnInfo("AS32613", "IWEB-AS, CA"))


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    def manager(self, results):
        executor = mock.Mock()
        executor.execute = mock.AsyncMock(side_effect=results)

        async def txt(name):
            return []

        return PathTraceManager(executor, AsnResolver(txt)), executor

    async def test_runs_all_rounds(self):
        round_json = mtr_json((1, "10.0.0.1", True, 0.5), (2, "184.107.64.85", True, 130.0))
        manager, executor = self.manager([ok()] + [ok(round_json)] * ROUNDS)
        trace = manager.start("184.107.64.85", 8449)
        await asyncio.gather(*manager._tasks)
        view = manager.view(trace)
        self.assertEqual((view["state"], view["rounds_done"], len(view["hops"])), ("done", ROUNDS, 2))
        self.assertEqual(executor.execute.await_count, ROUNDS + 1)

    async def test_missing_mtr_fails_with_reason(self):
        manager, _ = self.manager([failed("")])
        trace = manager.start("184.107.64.85", 8449)
        await asyncio.gather(*manager._tasks)
        self.assertEqual(trace.state, TraceState.FAILED)
        self.assertIn("mtr-tiny", trace.error)

    async def test_concurrent_traces_are_limited(self):
        never = asyncio.Event()

        async def hang(*_args, **_kwargs):
            await never.wait()

        executor = mock.Mock(execute=hang)
        manager = PathTraceManager(executor, AsnResolver(mock.AsyncMock(return_value=[])))
        for _ in range(MAX_ACTIVE_TRACES):
            manager.start("184.107.64.85", 8449)
        with self.assertRaises(TraceBusyError):
            manager.start("184.107.64.85", 8449)
        for task in list(manager._tasks):
            task.cancel()


if __name__ == "__main__":
    unittest.main()
