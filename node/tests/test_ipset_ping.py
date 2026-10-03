"""Тесты закрытого ping (services/ipset_manager: PING_RULE_V4/V6, set_ping_block).

Вместо хоста — iptables/ip6tables в памяти: -C/-I/-D над списком правил цепочки.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import ipset_manager as im  # noqa: E402


class FakeNetfilter:
    """iptables и ip6tables: правила хранятся строками аргументов без действия."""

    def __init__(self, fail_ip6: bool = False):
        self.rules: dict[str, list[list[str]]] = {"iptables": [], "ip6tables": []}
        self.fail_ip6 = fail_ip6

    def run(self, cmd: list[str], timeout: int = 30) -> tuple[bool, str, str]:
        tool, action, spec = cmd[0], cmd[1], cmd[2:]
        if tool not in self.rules:
            return True, "", ""
        if tool == "ip6tables" and self.fail_ip6:
            return False, "", "ip6tables: can't initialize"
        table = self.rules[tool]
        if action == "-C":
            return spec in table, "", ""
        if action == "-I":
            chain, _position, rest = spec[0], spec[1], spec[2:]
            table.insert(0, [chain] + rest)
            return True, "", ""
        if action == "-D":
            if spec in table:
                table.remove(spec)
                return True, "", ""
            return False, "", "Bad rule"
        return True, "", ""


class PingBlockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp.name) / "blocklist.json"
        patcher = mock.patch.object(im, "PERSISTENT_FILE", str(self.state_file))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def make_manager(self, net: FakeNetfilter) -> im.IpsetManager:
        manager = im.IpsetManager()
        manager._run_cmd = net.run
        # Снимок сетов для записи на диск — не предмет этих тестов
        manager._list_members = lambda set_name: []
        return manager

    def test_block_adds_rule_excluding_allowlist(self):
        net = FakeNetfilter()
        ok, _ = self.make_manager(net).set_ping_block(True)
        self.assertTrue(ok)
        self.assertEqual(net.rules["iptables"], [im.PING_RULE_V4])
        self.assertIn("!", im.PING_RULE_V4)
        self.assertIn(im.SET_ALLOW, im.PING_RULE_V4)
        self.assertEqual(net.rules["ip6tables"], [im.PING_RULE_V6])

    def test_block_twice_keeps_single_rule(self):
        net = FakeNetfilter()
        manager = self.make_manager(net)
        manager.set_ping_block(True)
        manager.set_ping_block(True)
        self.assertEqual(net.rules["iptables"], [im.PING_RULE_V4])

    def test_allow_removes_every_copy(self):
        net = FakeNetfilter()
        net.rules["iptables"] = [list(im.PING_RULE_V4), list(im.PING_RULE_V4)]
        ok, _ = self.make_manager(net).set_ping_block(False)
        self.assertTrue(ok)
        self.assertEqual(net.rules["iptables"], [])

    def test_only_echo_request_is_dropped(self):
        # Остальной ICMP нужен сети: без fragmentation-needed виснут соединения
        self.assertIn("echo-request", im.PING_RULE_V4)
        self.assertIn("echo-request", im.PING_RULE_V6)

    def test_ipv6_failure_does_not_leave_ipv4_open(self):
        net = FakeNetfilter(fail_ip6=True)
        ok, _ = self.make_manager(net).set_ping_block(True)
        self.assertTrue(ok)
        self.assertEqual(net.rules["iptables"], [im.PING_RULE_V4])

    def test_flag_survives_agent_restart(self):
        self.make_manager(FakeNetfilter()).set_ping_block(True)
        self.assertTrue(json.loads(self.state_file.read_text())["block_ping"])

        # Ребут хоста: правил в INPUT нет, флаг лежит на диске
        net = FakeNetfilter()
        manager = self.make_manager(net)
        manager._load_config()
        manager._apply_ping_rules(manager._block_ping)
        self.assertEqual(net.rules["iptables"], [im.PING_RULE_V4])

    def test_status_reports_live_rule(self):
        net = FakeNetfilter()
        manager = self.make_manager(net)
        manager._set_count = lambda set_name: 0
        self.assertFalse(manager.get_status().ping_blocked)
        manager.set_ping_block(True)
        self.assertTrue(manager.get_status().ping_blocked)


if __name__ == "__main__":
    unittest.main()
