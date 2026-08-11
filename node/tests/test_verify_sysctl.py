"""Tests for the tuning verification logic in app.services.sysctl_verify.

Runnable with plain stdlib:  python -m unittest discover -s node/tests
(pytest picks these up too — they are ordinary unittest TestCases.)

These cover the two things the rewrite actually got wrong before: multi-value
sysctl keys come back TAB-separated while the config file uses single spaces,
and the expected set has to survive keys the kernel does not have.
"""

import asyncio
import json
import os
import sys
import unittest
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import sysctl_verify  # noqa: E402


@dataclass
class FakeResult:
    success: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeExecutor:
    """Minimal stand-in for HostExecutor.

    Records the commands it was given so the "one round-trip, not N" property
    can be asserted rather than assumed.
    """

    def __init__(self, sysctl_values: dict, hashsize: Optional[int] = None):
        self.sysctl_values = sysctl_values
        self.hashsize = hashsize
        self.calls = []

    async def execute(self, cmd, timeout=None, shell=None):
        self.calls.append(cmd)
        if "hashsize" in cmd:
            if self.hashsize is None:
                return FakeResult(success=False, exit_code=1)
            return FakeResult(stdout=f"{self.hashsize}\n")
        # The real script prints one `key=value` line per key.
        lines = []
        for key, value in self.sysctl_values.items():
            lines.append(f"{key}={value}")
        return FakeResult(stdout="\n".join(lines) + "\n")


BASE_FACTS = {
    "schema": 1,
    "formula_version": "1.0.0",
    "computed": {
        "net.netfilter.nf_conntrack_max": "524288",
        # Multi-value key: the file writes single spaces.
        "net.ipv4.tcp_mem": "93600 124800 156000",
        "net.core.somaxconn": "8192",
    },
    "static": {
        "net.ipv4.tcp_congestion_control": "bbr",
        "net.ipv4.tcp_timestamps": "1",
    },
    "derived": {"conntrack_hashsize": 131072},
    "unsupported_keys": [],
}


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class TestNormalizeSysctlValue(unittest.TestCase):
    def test_tab_separated_matches_space_separated(self):
        # `sysctl -n net.ipv4.tcp_mem` returns TABs; the config file has spaces.
        # An exact string comparison — which is what the old code did — would
        # report every multi-value key as a mismatch.
        self.assertEqual(
            sysctl_verify._normalize_sysctl_value("93600\t124800\t156000"),
            sysctl_verify._normalize_sysctl_value("93600 124800 156000"),
        )

    def test_collapses_runs_and_trims(self):
        self.assertEqual(sysctl_verify._normalize_sysctl_value("  4096   87380\t6291456 "), "4096 87380 6291456")

    def test_handles_empty_and_none(self):
        self.assertEqual(sysctl_verify._normalize_sysctl_value(""), "")
        self.assertEqual(sysctl_verify._normalize_sysctl_value(None), "")


class TestExpectedFromFacts(unittest.TestCase):
    def test_merges_computed_and_static(self):
        expected = sysctl_verify.expected_from_facts(BASE_FACTS)
        self.assertIn("net.netfilter.nf_conntrack_max", expected)
        self.assertIn("net.ipv4.tcp_congestion_control", expected)
        self.assertEqual(len(expected), 5)

    def test_drops_unsupported_keys(self):
        facts = json.loads(json.dumps(BASE_FACTS))
        facts["computed"]["net.ipv4.tcp_migrate_req"] = "1"
        facts["unsupported_keys"] = ["net.ipv4.tcp_migrate_req"]
        expected = sysctl_verify.expected_from_facts(facts)
        self.assertNotIn("net.ipv4.tcp_migrate_req", expected)

    def test_drops_keys_other_writers_own(self):
        # Xray's WireGuard outbound writes rp_filter=0 and disable_ipv6=0 at
        # runtime; asserting on them gave a permanent false failure.
        facts = json.loads(json.dumps(BASE_FACTS))
        facts["computed"]["net.ipv4.conf.all.rp_filter"] = "2"
        facts["computed"]["net.ipv6.conf.all.disable_ipv6"] = "1"
        expected = sysctl_verify.expected_from_facts(facts)
        self.assertNotIn("net.ipv4.conf.all.rp_filter", expected)
        self.assertNotIn("net.ipv6.conf.all.disable_ipv6", expected)

    def test_static_wins_over_computed(self):
        facts = json.loads(json.dumps(BASE_FACTS))
        facts["computed"]["net.ipv4.tcp_timestamps"] = "0"
        expected = sysctl_verify.expected_from_facts(facts)
        self.assertEqual(expected["net.ipv4.tcp_timestamps"], "1")


class TestVerifySysctlValues(unittest.TestCase):
    def setUp(self):
        self._orig_read = sysctl_verify.read_host_file

    def tearDown(self):
        sysctl_verify.read_host_file = self._orig_read

    def _patch_facts(self, facts):
        async def fake_read(path):
            if path == sysctl_verify.TUNING_FACTS_PATH:
                return json.dumps(facts) if facts is not None else None
            return None
        sysctl_verify.read_host_file = fake_read

    def test_all_values_match(self):
        self._patch_facts(BASE_FACTS)
        ex = FakeExecutor(
            {
                "net.netfilter.nf_conntrack_max": "524288",
                "net.ipv4.tcp_mem": "93600\t124800\t156000",
                "net.core.somaxconn": "8192",
                "net.ipv4.tcp_congestion_control": "bbr",
                "net.ipv4.tcp_timestamps": "1",
            },
            hashsize=131072,
        )
        result = run(sysctl_verify.verify_sysctl_values(ex))
        self.assertTrue(result["success"], result["failed"])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["checked_count"], 6)  # 5 keys + hashsize

    def test_reads_all_keys_in_one_call(self):
        # The old loop spawned one nsenter process per key; at ~37 keys that is
        # 37 processes on every apply.
        self._patch_facts(BASE_FACTS)
        ex = FakeExecutor(
            {
                "net.netfilter.nf_conntrack_max": "524288",
                "net.ipv4.tcp_mem": "93600 124800 156000",
                "net.core.somaxconn": "8192",
                "net.ipv4.tcp_congestion_control": "bbr",
                "net.ipv4.tcp_timestamps": "1",
            },
            hashsize=131072,
        )
        run(sysctl_verify.verify_sysctl_values(ex))
        sysctl_calls = [c for c in ex.calls if "hashsize" not in c]
        self.assertEqual(len(sysctl_calls), 1)

    def test_reports_mismatch(self):
        self._patch_facts(BASE_FACTS)
        ex = FakeExecutor(
            {
                "net.netfilter.nf_conntrack_max": "262144",  # wrong
                "net.ipv4.tcp_mem": "93600\t124800\t156000",
                "net.core.somaxconn": "8192",
                "net.ipv4.tcp_congestion_control": "cubic",  # BBR failed to load
                "net.ipv4.tcp_timestamps": "1",
            },
            hashsize=131072,
        )
        result = run(sysctl_verify.verify_sysctl_values(ex))
        self.assertFalse(result["success"])
        joined = " ".join(result["failed"])
        self.assertIn("nf_conntrack_max", joined)
        self.assertIn("tcp_congestion_control", joined)

    def test_missing_key_is_a_failure(self):
        self._patch_facts(BASE_FACTS)
        ex = FakeExecutor(
            {
                "net.netfilter.nf_conntrack_max": "524288",
                "net.ipv4.tcp_mem": "MISSING",
                "net.core.somaxconn": "8192",
                "net.ipv4.tcp_congestion_control": "bbr",
                "net.ipv4.tcp_timestamps": "1",
            },
            hashsize=131072,
        )
        result = run(sysctl_verify.verify_sysctl_values(ex))
        self.assertFalse(result["success"])
        self.assertTrue(any("tcp_mem" in f for f in result["failed"]))

    def test_hashsize_is_a_threshold_not_equality(self):
        # A larger live table is fine; the module may already have one.
        self._patch_facts(BASE_FACTS)
        values = {
            "net.netfilter.nf_conntrack_max": "524288",
            "net.ipv4.tcp_mem": "93600 124800 156000",
            "net.core.somaxconn": "8192",
            "net.ipv4.tcp_congestion_control": "bbr",
            "net.ipv4.tcp_timestamps": "1",
        }
        bigger = run(sysctl_verify.verify_sysctl_values(FakeExecutor(values, hashsize=262144)))
        self.assertTrue(bigger["success"], bigger["failed"])

        smaller = run(sysctl_verify.verify_sysctl_values(FakeExecutor(values, hashsize=65536)))
        self.assertFalse(smaller["success"])
        self.assertTrue(any("hashsize" in f for f in smaller["failed"]))

    def test_missing_facts_file_is_the_re_apply_signal(self):
        self._patch_facts(None)
        result = run(sysctl_verify.verify_sysctl_values(FakeExecutor({})))
        self.assertFalse(result["success"])
        self.assertTrue(any("tuning-facts.json" in f for f in result["failed"]))

    def test_corrupt_facts_file_fails_cleanly(self):
        async def fake_read(path):
            return "{not json"
        sysctl_verify.read_host_file = fake_read
        result = run(sysctl_verify.verify_sysctl_values(FakeExecutor({})))
        self.assertFalse(result["success"])
        self.assertTrue(any("valid JSON" in f for f in result["failed"]))


if __name__ == "__main__":
    unittest.main()
