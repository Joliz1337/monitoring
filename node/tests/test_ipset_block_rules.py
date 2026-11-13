"""Тесты места правил блок-листа (services/ipset_manager: init_sets, set_timeout).

Блокировки обязаны стоять на хуках, через которые идёт и транзит: трафик к
опубликованным портам Docker и DNAT-проброс цепочек INPUT/OUTPUT не видят.

Вместо хоста — ipset и iptables в памяти с двумя свойствами ядра: правило со
ссылкой на несуществующий сет не принимается, а сет, на который ссылается
правило, не уничтожается.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import ipset_manager as im  # noqa: E402

BLOCK_SETS = (im.SET_PERMANENT, im.SET_TEMP, im.SET_OUT_PERMANENT, im.SET_OUT_TEMP)


def referenced_sets(rule: list[str]) -> list[str]:
    return [rule[i + 1] for i, arg in enumerate(rule) if arg == "--match-set"]


class FakeHost:
    def __init__(self, missing_tables: tuple[str, ...] = ()):
        self.sets: dict[str, list[str]] = {}
        self.rules: dict[str, list[list[str]]] = {"filter": [], "raw": [], "mangle": []}
        self.rules_v6: dict[str, list[list[str]]] = {"filter": []}
        self.missing_tables = set(missing_tables)

    def run(self, cmd: list[str], timeout: int = 30) -> tuple[bool, str, str]:
        if cmd[0] == "ipset":
            return self._ipset([arg for arg in cmd[1:] if arg != "-exist"])
        if cmd[0] == "iptables":
            return self._iptables(cmd[1:], self.rules)
        if cmd[0] == "ip6tables":
            return self._iptables(cmd[1:], self.rules_v6)
        return True, "", ""

    def _ipset(self, args: list[str]) -> tuple[bool, str, str]:
        action, name = args[0], args[1]
        if action == "list":
            return name in self.sets, "", ""
        if action == "create":
            self.sets.setdefault(name, args[2:])
            return True, "", ""
        if action == "destroy":
            if any(name in referenced_sets(rule) for rules in self.rules.values() for rule in rules):
                return False, "", "Set cannot be destroyed: it is in use by a kernel component"
            self.sets.pop(name, None)
            return True, "", ""
        return True, "", ""

    def _iptables(self, args: list[str], tables: dict[str, list[list[str]]]) -> tuple[bool, str, str]:
        table = "filter"
        if args[0] == "-t":
            table, args = args[1], args[2:]
        if table in self.missing_tables:
            return False, "", f"can't initialize iptables table `{table}': Table does not exist"
        action, spec = args[0], args[1:]
        rules = tables[table]
        if action == "-C":
            return spec in rules, "", ""
        if action == "-I":
            rule = [spec[0]] + spec[2:]
            unknown = [name for name in referenced_sets(rule) if name not in self.sets]
            if unknown:
                return False, "", f"Set {unknown[0]} doesn't exist."
            rules.insert(0, rule)
            return True, "", ""
        if action == "-D":
            if spec not in rules:
                return False, "", "Bad rule (does a matching rule exist in that chain?)."
            rules.remove(spec)
            return True, "", ""
        return True, "", ""

    def chain(self, table: str, chain: str) -> list[list[str]]:
        return [rule for rule in self.rules[table] if rule[0] == chain]

    def block_rules(self, table: str, chain: str) -> list[list[str]]:
        return [
            rule for rule in self.chain(table, chain)
            if rule[-1] == "DROP" and set(referenced_sets(rule)) & set(BLOCK_SETS)
        ]


class BlockRulesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(im, "PERSISTENT_FILE", str(Path(self.tmp.name) / "blocklist.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def start_agent(self, host: FakeHost) -> im.IpsetManager:
        manager = im.IpsetManager()
        manager._run_cmd = host.run
        manager._run_ipset_restore = lambda lines, timeout=180: (True, "", "")
        ok, message = manager.init_sets()
        self.assertTrue(ok, message)
        return manager


class PlacementTest(BlockRulesTestCase):
    def test_incoming_block_sits_before_the_input_forward_split(self):
        host = FakeHost()
        self.start_agent(host)
        self.assertCountEqual(host.block_rules("raw", "PREROUTING"), [
            im.block_rule("PREROUTING", im.SET_PERMANENT, "src", im.SET_ALLOW),
            im.block_rule("PREROUTING", im.SET_TEMP, "src", im.SET_ALLOW),
        ])
        self.assertEqual(host.block_rules("filter", "INPUT"), [])

    def test_outgoing_block_covers_host_and_transit(self):
        host = FakeHost()
        self.start_agent(host)
        for table, chain in (("filter", "OUTPUT"), ("mangle", "FORWARD")):
            with self.subTest(table=table, chain=chain):
                self.assertCountEqual(host.block_rules(table, chain), [
                    im.block_rule(chain, im.SET_OUT_PERMANENT, "dst", im.SET_ALLOW_OUT),
                    im.block_rule(chain, im.SET_OUT_TEMP, "dst", im.SET_ALLOW_OUT),
                ])

    def test_every_block_rule_spares_the_allowlist_of_its_direction(self):
        host = FakeHost()
        self.start_agent(host)
        placed = [rule for rules in host.rules.values() for rule in rules if rule[-1] == "DROP"
                  and set(referenced_sets(rule)) & set(BLOCK_SETS)]
        self.assertEqual(len(placed), 6)
        for rule in placed:
            blocked, allowed = referenced_sets(rule)
            expected = im.SET_ALLOW_OUT if blocked in (im.SET_OUT_PERMANENT, im.SET_OUT_TEMP) else im.SET_ALLOW
            with self.subTest(rule=rule):
                self.assertEqual(allowed, expected)
                self.assertEqual(rule[rule.index(allowed) - 2], "!")

    def test_allowlist_accept_stays_on_top_of_input_and_output(self):
        # Он проводит доверенные адреса мимо UFW — блок-листу больше не нужен
        host = FakeHost()
        self.start_agent(host)
        self.assertEqual(
            host.chain("filter", "INPUT")[0],
            ["INPUT", "-m", "set", "--match-set", im.SET_ALLOW, "src", "-j", "ACCEPT"],
        )
        self.assertEqual(
            host.chain("filter", "OUTPUT")[0],
            ["OUTPUT", "-m", "set", "--match-set", im.SET_ALLOW_OUT, "dst", "-j", "ACCEPT"],
        )

    def test_agent_restart_keeps_single_copy_of_each_rule(self):
        host = FakeHost()
        self.start_agent(host)
        before = {table: list(rules) for table, rules in host.rules.items()}
        self.start_agent(host)
        for table, rules in host.rules.items():
            with self.subTest(table=table):
                self.assertCountEqual(rules, before[table])


class UpgradeTest(BlockRulesTestCase):
    def test_rules_of_the_previous_agent_leave_input_and_output(self):
        host = FakeHost()
        for name in (*BLOCK_SETS, im.SET_ALLOW, im.SET_ALLOW_OUT):
            host.sets[name] = []
        stale_in = im.legacy_block_rule("INPUT", im.SET_PERMANENT, "src")
        host.rules["filter"] = [
            list(stale_in), list(stale_in),
            im.legacy_block_rule("INPUT", im.SET_TEMP, "src"),
            im.legacy_block_rule("OUTPUT", im.SET_OUT_PERMANENT, "dst"),
            im.legacy_block_rule("OUTPUT", im.SET_OUT_TEMP, "dst"),
        ]
        self.start_agent(host)
        self.assertEqual(host.block_rules("filter", "INPUT"), [])
        self.assertCountEqual(host.block_rules("filter", "OUTPUT"), [
            im.block_rule("OUTPUT", im.SET_OUT_PERMANENT, "dst", im.SET_ALLOW_OUT),
            im.block_rule("OUTPUT", im.SET_OUT_TEMP, "dst", im.SET_ALLOW_OUT),
        ])
        self.assertEqual(len(host.block_rules("raw", "PREROUTING")), 2)


class FallbackTest(BlockRulesTestCase):
    def test_host_without_raw_table_keeps_protecting_itself(self):
        host = FakeHost(missing_tables=("raw",))
        with self.assertLogs(im.logger, level="ERROR") as logs:
            manager = self.start_agent(host)
        self.assertTrue(any("falling back to INPUT" in line for line in logs.output))
        self.assertCountEqual(host.block_rules("filter", "INPUT"), [
            im.legacy_block_rule("INPUT", im.SET_PERMANENT, "src"),
            im.legacy_block_rule("INPUT", im.SET_TEMP, "src"),
        ])
        # Запасной DROP исключения не несёт — ACCEPT белого списка обязан быть выше
        self.assertEqual(host.chain("filter", "INPUT")[0][-1], "ACCEPT")
        self.assertTrue(manager.get_status().incoming.iptables_rules_exist)

    def test_host_without_mangle_table_leaves_one_rule_per_set_in_output(self):
        host = FakeHost(missing_tables=("mangle",))
        with self.assertLogs(im.logger, level="ERROR"):
            manager = self.start_agent(host)
        self.assertCountEqual(host.block_rules("filter", "OUTPUT"), [
            im.legacy_block_rule("OUTPUT", im.SET_OUT_PERMANENT, "dst"),
            im.legacy_block_rule("OUTPUT", im.SET_OUT_TEMP, "dst"),
        ])
        self.assertEqual(host.chain("filter", "OUTPUT")[0][-1], "ACCEPT")
        self.assertTrue(manager.get_status().outgoing.iptables_rules_exist)
        self.assertEqual(len(host.block_rules("raw", "PREROUTING")), 2)


class StatusTest(BlockRulesTestCase):
    def test_missing_rule_on_any_hook_is_reported(self):
        for table, rule in (
            ("raw", im.block_rule("PREROUTING", im.SET_TEMP, "src", im.SET_ALLOW)),
            ("mangle", im.block_rule("FORWARD", im.SET_OUT_PERMANENT, "dst", im.SET_ALLOW_OUT)),
        ):
            with self.subTest(table=table):
                host = FakeHost()
                manager = self.start_agent(host)
                status = manager.get_status()
                self.assertTrue(status.incoming.iptables_rules_exist)
                self.assertTrue(status.outgoing.iptables_rules_exist)

                host.rules[table].remove(rule)
                status = manager.get_status()
                self.assertEqual(status.incoming.iptables_rules_exist, table != "raw")
                self.assertEqual(status.outgoing.iptables_rules_exist, table != "mangle")


class TimeoutChangeTest(BlockRulesTestCase):
    def test_temp_sets_are_recreated_and_rules_return_to_every_hook(self):
        host = FakeHost()
        manager = self.start_agent(host)
        before = {table: list(rules) for table, rules in host.rules.items()}

        ok, message = manager.set_timeout(120)
        self.assertTrue(ok, message)
        for name in (im.SET_TEMP, im.SET_OUT_TEMP):
            with self.subTest(set=name):
                created = host.sets[name]
                self.assertEqual(created[created.index("timeout") + 1], "120")
        for table, rules in host.rules.items():
            with self.subTest(table=table):
                self.assertCountEqual(rules, before[table])

    def test_fallback_rule_does_not_hold_the_temp_set(self):
        host = FakeHost(missing_tables=("raw",))
        with self.assertLogs(im.logger, level="ERROR"):
            manager = self.start_agent(host)
            ok, message = manager.set_timeout(120)
        self.assertTrue(ok, message)
        created = host.sets[im.SET_TEMP]
        self.assertEqual(created[created.index("timeout") + 1], "120")
        self.assertIn(im.legacy_block_rule("INPUT", im.SET_TEMP, "src"), host.rules["filter"])
        self.assertEqual(host.chain("filter", "INPUT")[0][-1], "ACCEPT")


if __name__ == "__main__":
    unittest.main()
