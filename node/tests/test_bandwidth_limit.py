"""Лимит полосы: разбор состояния и вывода `tc -j`, расчёт burst и самолечение.

Шейпер — tbf. cake на боевой VPS (kvm-clock) недобирал ~35% полосы при
свободном CPU (лимит 950 → фактические ~600, 8% дропов), tbf с burst от
скорости держит полку вплотную к лимиту без единого дропа. cake распознаётся
как legacy — самолечение мигрирует его на tbf.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.bandwidth_limit import (  # noqa: E402
    BURST_MAX_BYTES,
    BURST_MIN_BYTES,
    BandwidthLimiter,
    parse_state,
    parse_tc_root,
    render_state,
    tbf_burst_bytes,
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


CAKE_JSON = '[{"kind":"cake","handle":"8001:","root":true,"refcnt":2,"options":{"bandwidth":118750000,"diffserv":"besteffort"}}]'
TBF_500_JSON = '[{"kind":"tbf","handle":"8002:","root":true,"options":{"rate":62500000,"burst":524288}}]'
TBF_950_JSON = '[{"kind":"tbf","handle":"8003:","root":true,"options":{"rate":118750000,"burst":3563520}}]'
MQ_JSON = '[{"kind":"mq","handle":"0:","root":true,"refcnt":2},{"kind":"fq_codel","handle":"0:","parent":"0:1","options":{"limit":10240}}]'


# JSON tc отдаёт байты/с: 950 Мбит/с = 118 750 000, 500 Мбит/с = 62 500 000
class ParseTest(unittest.TestCase):
    def test_state_roundtrip(self):
        self.assertEqual(parse_state(render_state(950, "eth0")), (950, "eth0"))
        self.assertEqual(parse_state(""), (0, ""))
        self.assertEqual(parse_state("BANDWIDTH_LIMIT_MBIT=abc\nBANDWIDTH_LIMIT_IFACE='ens3'"), (0, "ens3"))

    def test_tc_root(self):
        self.assertEqual(parse_tc_root(CAKE_JSON), ("cake", 950))
        self.assertEqual(parse_tc_root(TBF_500_JSON), ("tbf", 500))
        self.assertEqual(parse_tc_root(MQ_JSON), ("mq", None))
        self.assertEqual(parse_tc_root("not json"), (None, None))
        self.assertEqual(parse_tc_root("[]"), (None, None))


class BurstTest(unittest.TestCase):
    def test_burst_is_30ms_of_bandwidth(self):
        # 950 Мбит/с → 30 мс полосы ≈ 3.56 МБ (проверено на боевой: полка 929-940)
        self.assertEqual(tbf_burst_bytes(950), int(950e6 / 8 * 0.03))

    def test_small_limit_gets_the_floor(self):
        # 100 Мбит → 375 КБ по формуле, но ниже пола tbf теряет точность
        self.assertEqual(tbf_burst_bytes(100), BURST_MIN_BYTES)

    def test_huge_limit_is_capped(self):
        self.assertEqual(tbf_burst_bytes(50_000), BURST_MAX_BYTES)


class LimiterTest(unittest.TestCase):
    def test_state_in_sync_with_tbf(self):
        ok = FakeExecutor({"cat ": FakeResult(stdout=render_state(950, "eth0")), "tc -j": FakeResult(stdout=TBF_950_JSON)})
        state = asyncio.run(BandwidthLimiter(ok).state())
        self.assertTrue(state["enabled"] and state["applied"] and state["in_sync"])
        self.assertEqual((state["qdisc"], state["applied_mbit"]), ("tbf", 950))

    def test_legacy_cake_is_applied_but_not_in_sync(self):
        # Лимит работает (applied), но самолечение обязано мигрировать его на tbf
        legacy = FakeExecutor({"cat ": FakeResult(stdout=render_state(950, "eth0")), "tc -j": FakeResult(stdout=CAKE_JSON)})
        state = asyncio.run(BandwidthLimiter(legacy).state())
        self.assertTrue(state["enabled"] and state["applied"])
        self.assertFalse(state["in_sync"])
        self.assertEqual(state["qdisc"], "cake")

    def test_state_drift_and_off(self):
        drift = FakeExecutor({"cat ": FakeResult(stdout=render_state(950, "eth0")), "tc -j": FakeResult(stdout=MQ_JSON)})
        state = asyncio.run(BandwidthLimiter(drift).state())
        self.assertTrue(state["enabled"])
        self.assertFalse(state["applied"])
        self.assertFalse(state["in_sync"])

        off = FakeExecutor({"cat ": FakeResult(success=False, exit_code=1), "tc -j": FakeResult(stdout=MQ_JSON)})
        state = asyncio.run(BandwidthLimiter(off).state())
        self.assertFalse(state["enabled"])
        self.assertTrue(state["in_sync"])

    def test_apply_uses_tbf_with_computed_burst(self):
        executor = FakeExecutor({
            "cat ": FakeResult(stdout=render_state(950, "eth0")),
            "tc -j": FakeResult(stdout=MQ_JSON),
            "root tbf": FakeResult(),
        })
        self.assertIn("re-applied 950", asyncio.run(BandwidthLimiter(executor).ensure()))
        expected = f"root tbf rate 950mbit burst {tbf_burst_bytes(950)} latency 100ms"
        self.assertTrue(any(expected in c for c in executor.commands), executor.commands)
        self.assertFalse(any("cake" in c for c in executor.commands))

    def test_ensure_migrates_legacy_cake_to_tbf(self):
        executor = FakeExecutor({
            "cat ": FakeResult(stdout=render_state(950, "eth0")),
            "tc -j": FakeResult(stdout=CAKE_JSON),
            "root tbf": FakeResult(),
        })
        self.assertIn("re-applied 950", asyncio.run(BandwidthLimiter(executor).ensure()))
        self.assertTrue(any("root tbf rate 950mbit" in c for c in executor.commands))

    def test_ensure_does_nothing_when_tbf_matches(self):
        fine = FakeExecutor({"cat ": FakeResult(stdout=render_state(950, "eth0")), "tc -j": FakeResult(stdout=TBF_950_JSON)})
        self.assertIsNone(asyncio.run(BandwidthLimiter(fine).ensure()))
        self.assertFalse(any("root tbf rate" in c for c in fine.commands))

    def test_remove_only_own_qdisc(self):
        # Свой шейпер (tbf и legacy cake) снимается, чужой корневой qdisc (mq хоста) не трогается
        executor = FakeExecutor({"tc -j": FakeResult(stdout=MQ_JSON), "qdisc del": FakeResult()})
        asyncio.run(BandwidthLimiter(executor)._remove("eth0"))
        self.assertFalse(any("qdisc del" in c for c in executor.commands))
        for own_json in (CAKE_JSON, TBF_950_JSON):
            executor = FakeExecutor({"tc -j": FakeResult(stdout=own_json), "qdisc del": FakeResult()})
            asyncio.run(BandwidthLimiter(executor)._remove("eth0"))
            self.assertTrue(any("qdisc del dev eth0 root" in c for c in executor.commands))


if __name__ == "__main__":
    unittest.main()
