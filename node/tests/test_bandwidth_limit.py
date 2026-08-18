"""Лимит полосы: разбор состояния и вывода `tc -j`, выбор qdisc и самолечение.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.bandwidth_limit import (  # noqa: E402
    BandwidthLimiter,
    parse_state,
    parse_tc_root,
    render_state,
)


@dataclass
class FakeResult:
    success: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str = ""


class FakeExecutor:
    """Отвечает по подстроке команды; запоминает всё, что запускали."""

    def __init__(self, answers: dict[str, FakeResult]):
        self.answers = answers
        self.commands: list[str] = []

    async def execute(self, command: str, timeout: int = 30, shell: str = "sh") -> FakeResult:
        self.commands.append(command)
        for key, result in self.answers.items():
            if key in command:
                return result
        return FakeResult(success=False, exit_code=1, stderr="no answer")


CAKE_JSON = '[{"kind":"cake","handle":"8001:","root":true,"refcnt":2,"options":{"bandwidth":950000000,"diffserv":"besteffort"}}]'
TBF_JSON = '[{"kind":"tbf","handle":"8002:","root":true,"options":{"rate":500000000,"burst":524288}}]'
MQ_JSON = '[{"kind":"mq","handle":"0:","root":true,"refcnt":2},{"kind":"fq_codel","handle":"0:","parent":"0:1","options":{"limit":10240}}]'


class ParseTest(unittest.TestCase):
    def test_state_roundtrip(self):
        self.assertEqual(parse_state(render_state(950, "eth0")), (950, "eth0"))
        self.assertEqual(parse_state(""), (0, ""))
        self.assertEqual(parse_state("BANDWIDTH_LIMIT_MBIT=abc\nBANDWIDTH_LIMIT_IFACE='ens3'"), (0, "ens3"))

    def test_tc_root(self):
        self.assertEqual(parse_tc_root(CAKE_JSON), ("cake", 950))
        self.assertEqual(parse_tc_root(TBF_JSON), ("tbf", 500))
        self.assertEqual(parse_tc_root(MQ_JSON), ("mq", None))
        self.assertEqual(parse_tc_root("not json"), (None, None))
        self.assertEqual(parse_tc_root("[]"), (None, None))


class LimiterTest(unittest.TestCase):
    def test_state_in_sync_and_drift(self):
        ok = FakeExecutor({"cat ": FakeResult(stdout=render_state(950, "eth0")), "tc -j": FakeResult(stdout=CAKE_JSON)})
        state = asyncio.run(BandwidthLimiter(ok).state())
        self.assertTrue(state["enabled"] and state["applied"] and state["in_sync"])
        self.assertEqual((state["qdisc"], state["applied_mbit"]), ("cake", 950))

        drift = FakeExecutor({"cat ": FakeResult(stdout=render_state(950, "eth0")), "tc -j": FakeResult(stdout=MQ_JSON)})
        state = asyncio.run(BandwidthLimiter(drift).state())
        self.assertTrue(state["enabled"])
        self.assertFalse(state["applied"])
        self.assertFalse(state["in_sync"])

        off = FakeExecutor({"cat ": FakeResult(success=False, exit_code=1), "tc -j": FakeResult(stdout=MQ_JSON)})
        state = asyncio.run(BandwidthLimiter(off).state())
        self.assertFalse(state["enabled"])
        self.assertTrue(state["in_sync"])

    def test_ensure_reapplies_only_on_drift(self):
        drift = FakeExecutor({
            "cat ": FakeResult(stdout=render_state(950, "eth0")),
            "tc -j": FakeResult(stdout=MQ_JSON),
            "root cake": FakeResult(),
        })
        self.assertIn("re-applied 950", asyncio.run(BandwidthLimiter(drift).ensure()))
        self.assertTrue(any("root cake bandwidth 950mbit" in c for c in drift.commands))

        fine = FakeExecutor({"cat ": FakeResult(stdout=render_state(950, "eth0")), "tc -j": FakeResult(stdout=CAKE_JSON)})
        self.assertIsNone(asyncio.run(BandwidthLimiter(fine).ensure()))
        self.assertFalse(any("root cake bandwidth" in c for c in fine.commands))

    def test_falls_back_to_tbf_when_cake_missing(self):
        executor = FakeExecutor({
            "cat ": FakeResult(stdout=render_state(300, "eth0")),
            "tc -j": FakeResult(stdout=MQ_JSON),
            "root cake": FakeResult(success=False, exit_code=2, stderr='Unknown qdisc "cake"'),
            "root tbf": FakeResult(),
        })
        self.assertIn("re-applied", asyncio.run(BandwidthLimiter(executor).ensure()))
        self.assertTrue(any("root tbf rate 300mbit" in c for c in executor.commands))

    def test_remove_only_own_qdisc(self):
        # Свой шейпер снимается, чужой корневой qdisc (mq хоста) не трогается
        executor = FakeExecutor({"tc -j": FakeResult(stdout=MQ_JSON), "qdisc del": FakeResult()})
        asyncio.run(BandwidthLimiter(executor)._remove("eth0"))
        self.assertFalse(any("qdisc del" in c for c in executor.commands))
        executor = FakeExecutor({"tc -j": FakeResult(stdout=CAKE_JSON), "qdisc del": FakeResult()})
        asyncio.run(BandwidthLimiter(executor)._remove("eth0"))
        self.assertTrue(any("qdisc del dev eth0 root" in c for c in executor.commands))


if __name__ == "__main__":
    unittest.main()
