"""Тесты бана по портам (services/ipset_manager: normalize_port_specs, port_ban).

Вместо хоста — iptables и ipset в памяти: правила таблицы raw хранятся строками
аргументов, строки `ipset restore` копятся списком.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import ipset_manager as im  # noqa: E402


class FakeHost:
    def __init__(self):
        self.raw_rules: list[list[str]] = []
        self.created_sets: list[list[str]] = []
        self.restored: list[str] = []
        self.restore_ok = True

    def run(self, cmd: list[str], timeout: int = 30) -> tuple[bool, str, str]:
        if cmd[0] == "ipset":
            if "create" in cmd:
                self.created_sets.append(cmd[1:])
            return True, "", ""
        if cmd[:3] != ["iptables", "-t", "raw"]:
            return True, "", ""
        action, spec = cmd[3], cmd[4:]
        if action == "-C":
            return spec in self.raw_rules, "", ""
        if action == "-I":
            chain, _position, rest = spec[0], spec[1], spec[2:]
            self.raw_rules.insert(0, [chain] + rest)
            return True, "", ""
        return True, "", ""

    def restore(self, lines: list[str], timeout: int = 180) -> tuple[bool, str, str]:
        if not self.restore_ok:
            return False, "", "Hash is full, cannot add more elements"
        self.restored.extend(lines)
        return True, "", ""


def make_manager(host: FakeHost) -> im.IpsetManager:
    manager = im.IpsetManager()
    manager._run_cmd = host.run
    manager._run_ipset_restore = host.restore
    return manager


class NormalizePortSpecsTest(unittest.TestCase):
    def test_single_ports_and_ranges(self):
        self.assertEqual(
            im.normalize_port_specs(["8443", " 2053 ", "10000-10010"]),
            ["2053", "8443", "10000-10010"],
        )

    def test_duplicates_collapse_and_degenerate_range_is_a_port(self):
        self.assertEqual(im.normalize_port_specs(["443", "443", "80-80"]), ["80", "443"])

    def test_rejects_garbage(self):
        for bad in (["0"], ["65536"], ["abc"], ["100-50"], ["1-2-3"], [""], ["-5"]):
            with self.subTest(bad=bad), self.assertRaises(im.InvalidPortSpecError):
                im.normalize_port_specs(bad)

    def test_rejects_empty_list(self):
        with self.assertRaises(im.InvalidPortSpecError):
            im.normalize_port_specs([])

    def test_limit_counts_expanded_ports(self):
        # Ядро раскладывает диапазон на отдельные элементы сета — считаем их, а не строки
        im.normalize_port_specs([f"1-{im.MAX_PORT_BAN_PORTS}"])
        with self.assertRaises(im.InvalidPortSpecError):
            im.normalize_port_specs([f"1-{im.MAX_PORT_BAN_PORTS}", "60000"])


class PortBanTest(unittest.TestCase):
    def test_bans_every_port_for_tcp_and_udp_with_timeout(self):
        host = FakeHost()
        added, failed, errors = make_manager(host).port_ban(["1.2.3.4"], ["8443", "10000-10002"], 1800)
        self.assertEqual((added, failed, errors), (1, 0, []))
        self.assertEqual(sorted(host.restored), sorted([
            "add torrent_port_ban 1.2.3.4,tcp:8443 timeout 1800",
            "add torrent_port_ban 1.2.3.4,udp:8443 timeout 1800",
            "add torrent_port_ban 1.2.3.4,tcp:10000-10002 timeout 1800",
            "add torrent_port_ban 1.2.3.4,udp:10000-10002 timeout 1800",
        ]))

    def test_rule_sits_in_raw_prerouting_and_spares_allowlist(self):
        host = FakeHost()
        make_manager(host).port_ban(["1.2.3.4"], ["8443"], 60)
        self.assertEqual(host.raw_rules, [im.PORT_BAN_RULE])
        self.assertEqual(im.PORT_BAN_RULE[0], "PREROUTING")
        self.assertIn("src,dst", im.PORT_BAN_RULE)
        self.assertIn("!", im.PORT_BAN_RULE)
        self.assertIn(im.SET_ALLOW, im.PORT_BAN_RULE)

    def test_repeated_bans_keep_single_rule(self):
        host = FakeHost()
        manager = make_manager(host)
        manager.port_ban(["1.2.3.4"], ["8443"], 60)
        manager.port_ban(["5.6.7.8"], ["8443"], 60)
        self.assertEqual(host.raw_rules, [im.PORT_BAN_RULE])

    def test_set_is_created_without_listing_members(self):
        # `ipset list` на сете в сотни тысяч записей — секунды; -exist create — нет
        host = FakeHost()
        make_manager(host).port_ban(["1.2.3.4"], ["8443"], 60)
        self.assertEqual(len(host.created_sets), 1)
        created = host.created_sets[0]
        self.assertEqual(created[:3], ["-exist", "create", im.SET_PORT_BAN])
        self.assertIn("hash:net,port", created)
        self.assertIn("timeout", created)

    def test_private_and_invalid_addresses_are_refused(self):
        host = FakeHost()
        added, failed, errors = make_manager(host).port_ban(
            ["10.0.0.1", "not-an-ip", "1.2.3.4"], ["8443"], 60,
        )
        self.assertEqual((added, failed), (1, 2))
        self.assertTrue(all(",tcp:" in line or ",udp:" in line for line in host.restored))
        self.assertFalse(any("10.0.0.1" in line for line in host.restored))
        self.assertTrue(errors)

    def test_restore_failure_is_reported(self):
        host = FakeHost()
        host.restore_ok = False
        added, failed, errors = make_manager(host).port_ban(["1.2.3.4"], ["8443"], 60)
        self.assertEqual((added, failed), (0, 1))
        self.assertTrue(any("Hash is full" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
