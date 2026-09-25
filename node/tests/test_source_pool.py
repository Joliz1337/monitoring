"""Пул исходящих адресов: раскладка меток, разбор вывода `ip`, план команд.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"

Инварианты, которые здесь закреплены: метки раскладываются по адресам по кругу
(иначе на ноде с 3 адресами 27 из 30 меток свалились бы по основной таблице на
один IP, и потолок портов не сдвинулся бы); нода трогает только свои метки и
таблицы; в устоявшемся состоянии цикл самолечения не пишет на хост ничего.
"""

import asyncio
import json
import os
import subprocess
import sys
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.source_pool import (  # noqa: E402
    MARK_BASE,
    MARK_COUNT,
    RULE_PRIORITY_BASE,
    TABLE_BASE,
    SourcePoolConfig,
)
from app.services.source_pool import (  # noqa: E402
    build_bindings,
    clear_commands,
    parse_default_gateway,
    parse_rules,
    parse_table_routes,
    plan_commands,
    probe_command,
    split_probe,
)


class BuildBindingsTest(unittest.TestCase):
    def test_marks_spread_evenly_over_addresses(self):
        bindings = build_bindings(["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        self.assertEqual(len(bindings), MARK_COUNT)
        per_address = {}
        for binding in bindings:
            per_address[binding.address] = per_address.get(binding.address, 0) + 1
        self.assertEqual(per_address, {"10.0.0.1": 10, "10.0.0.2": 10, "10.0.0.3": 10})

    def test_two_addresses_split_in_half(self):
        bindings = build_bindings(["10.0.0.1", "10.0.0.2"])
        halves = {binding.address for binding in bindings[:2]}
        self.assertEqual(halves, {"10.0.0.1", "10.0.0.2"})
        self.assertEqual(sum(1 for b in bindings if b.address == "10.0.0.1"), MARK_COUNT // 2)

    def test_single_address_produces_nothing(self):
        """Раскладывать нечего: метка и так уйдёт по основной таблице с того же IP."""
        self.assertEqual(build_bindings(["10.0.0.1"]), [])
        self.assertEqual(build_bindings([]), [])

    def test_one_table_per_address(self):
        bindings = build_bindings(["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        self.assertEqual({b.table for b in bindings}, {TABLE_BASE, TABLE_BASE + 1, TABLE_BASE + 2})
        for binding in bindings:
            same_table = [b.address for b in bindings if b.table == binding.table]
            self.assertEqual(set(same_table), {binding.address})


class ParseRulesTest(unittest.TestCase):
    def test_hex_string_and_int_forms(self):
        text = json.dumps([
            {"priority": RULE_PRIORITY_BASE, "fwmark": hex(MARK_BASE), "table": str(TABLE_BASE)},
            {"priority": RULE_PRIORITY_BASE + 1, "fwmark": MARK_BASE + 1, "table": TABLE_BASE + 1},
        ])
        self.assertEqual(
            parse_rules(text),
            {
                MARK_BASE: (RULE_PRIORITY_BASE, TABLE_BASE),
                MARK_BASE + 1: (RULE_PRIORITY_BASE + 1, TABLE_BASE + 1),
            },
        )

    def test_mark_with_mask(self):
        text = json.dumps([{"priority": 101, "fwmark": f"{MARK_BASE:#x}/0xffffffff", "table": "101"}])
        self.assertEqual(parse_rules(text), {MARK_BASE: (101, TABLE_BASE)})

    def test_foreign_rules_ignored(self):
        text = json.dumps([
            {"priority": 0, "src": "all", "table": "local"},
            {"priority": 32766, "src": "all", "table": "main"},
            {"priority": 500, "fwmark": "0x4d440001", "table": "77"},
        ])
        self.assertEqual(parse_rules(text), {})

    def test_broken_json(self):
        self.assertEqual(parse_rules("not json"), {})
        self.assertEqual(parse_rules(""), {})


class ParseRoutesTest(unittest.TestCase):
    def test_reads_source_from_our_tables(self):
        text = json.dumps([
            {"dst": "default", "gateway": "1.2.3.1", "dev": "bond0", "prefsrc": "1.2.3.4", "table": str(TABLE_BASE)},
            {"dst": "default", "gateway": "1.2.3.1", "dev": "bond0", "src": "1.2.3.5", "table": TABLE_BASE + 1},
        ])
        self.assertEqual(
            parse_table_routes(text),
            {TABLE_BASE: ("1.2.3.1", "1.2.3.4"), TABLE_BASE + 1: ("1.2.3.1", "1.2.3.5")},
        )

    def test_route_without_via_keeps_none_gateway(self):
        text = json.dumps([{"dst": "default", "dev": "eth0", "prefsrc": "1.2.3.4", "table": str(TABLE_BASE)}])
        self.assertEqual(parse_table_routes(text), {TABLE_BASE: (None, "1.2.3.4")})

    def test_foreign_tables_and_non_default_routes_ignored(self):
        text = json.dumps([
            {"dst": "default", "dev": "bond0", "prefsrc": "1.2.3.4", "table": "main"},
            {"dst": "10.0.0.0/8", "dev": "bond0", "prefsrc": "1.2.3.4", "table": str(TABLE_BASE)},
            {"dst": "default", "dev": "bond0", "prefsrc": "9.9.9.9", "table": "77"},
        ])
        self.assertEqual(parse_table_routes(text), {})


class GatewayTest(unittest.TestCase):
    def test_prefers_matching_interface(self):
        text = json.dumps([
            {"dst": "default", "gateway": "10.0.0.1", "dev": "eth1"},
            {"dst": "default", "gateway": "1.2.3.1", "dev": "bond0"},
        ])
        self.assertEqual(parse_default_gateway(text, "bond0"), "1.2.3.1")

    def test_point_to_point_has_no_gateway(self):
        text = json.dumps([{"dst": "default", "dev": "eth0"}])
        self.assertIsNone(parse_default_gateway(text, "eth0"))


class SplitProbeTest(unittest.TestCase):
    def test_three_sections(self):
        output = "#RULES\n[{\"a\":1}]\n#TABLES\n[]\n#DEFAULT\n[{\"b\":2}]\n"
        rules, tables, default = split_probe(output)
        self.assertEqual(json.loads(rules), [{"a": 1}])
        self.assertEqual(json.loads(tables), [])
        self.assertEqual(json.loads(default), [{"b": 2}])

    def test_probe_command_asks_for_all_three(self):
        command = probe_command()
        self.assertIn("ip -j -4 rule show", command)
        self.assertIn("ip -j -4 route show table all", command)
        self.assertIn("ip -j -4 route show default", command)

    def test_probe_markers_survive_real_bash(self):
        """Голый `#` в echo bash принял бы за комментарий и обрезал бы всю строку —
        опрос возвращал бы пустоту. Прогон через настоящий bash закрепляет кавычки."""
        result = subprocess.run(["bash", "-c", probe_command()], capture_output=True, text=True)
        positions = [result.stdout.find(marker) for marker in ("#RULES", "#TABLES", "#DEFAULT")]
        self.assertTrue(all(pos >= 0 for pos in positions), result.stdout)
        self.assertEqual(positions, sorted(positions))


class PlanCommandsTest(unittest.TestCase):
    def setUp(self):
        self.addresses = ["1.2.3.4", "1.2.3.5"]
        self.bindings = build_bindings(self.addresses)
        self.rules = {
            b.mark: (RULE_PRIORITY_BASE + index, b.table)
            for index, b in enumerate(self.bindings)
        }
        self.gateway = "1.2.3.1"
        self.routes = {b.table: (self.gateway, b.address) for b in self.bindings}

    def test_steady_state_writes_nothing(self):
        self.assertEqual(
            plan_commands(self.bindings, self.rules, self.routes, "bond0", "1.2.3.1", {}), []
        )

    def test_missing_rule_is_added(self):
        self.rules.pop(MARK_BASE + 5)
        commands = plan_commands(self.bindings, self.rules, self.routes, "bond0", "1.2.3.1", {})
        self.assertEqual(len(commands), 2)
        self.assertIn(f"ip rule del fwmark {MARK_BASE + 5}", commands[0])
        self.assertIn(f"ip rule add fwmark {MARK_BASE + 5}", commands[1])

    def test_rule_pointing_at_wrong_table_is_rebound(self):
        self.rules[MARK_BASE] = (RULE_PRIORITY_BASE, TABLE_BASE + 1)
        commands = plan_commands(self.bindings, self.rules, self.routes, "bond0", "1.2.3.1", {})
        self.assertTrue(any(f"ip rule add fwmark {MARK_BASE} lookup {TABLE_BASE} " in c for c in commands))

    def test_route_with_wrong_source_is_replaced(self):
        self.routes[TABLE_BASE] = (self.gateway, "9.9.9.9")
        commands = plan_commands(self.bindings, self.rules, self.routes, "bond0", self.gateway, {})
        self.assertEqual(
            commands,
            [f"ip route replace default via 1.2.3.1 dev bond0 src 1.2.3.4 table {TABLE_BASE}"],
        )

    def test_route_without_via_is_replaced_when_gateway_known(self):
        """Маршрут без шлюза выглядит рабочим по адресу, но пакеты по нему уходят
        в линк напрямую — его надо переписать, а не считать совпавшим."""
        self.routes[TABLE_BASE] = (None, "1.2.3.4")
        commands = plan_commands(self.bindings, self.rules, self.routes, "bond0", self.gateway, {})
        self.assertEqual(
            commands,
            [f"ip route replace default via 1.2.3.1 dev bond0 src 1.2.3.4 table {TABLE_BASE}"],
        )

    def test_address_with_own_gateway_leaves_through_it(self):
        own = {"1.2.3.5": "5.6.7.1"}
        commands = plan_commands(self.bindings, self.rules, self.routes, "bond0", self.gateway, own)
        self.assertEqual(
            commands, [f"ip route replace default via 5.6.7.1 dev bond0 src 1.2.3.5 table {TABLE_BASE + 1} onlink"]
        )
        self.routes[TABLE_BASE + 1] = ("5.6.7.1", "1.2.3.5")
        self.assertEqual(plan_commands(self.bindings, self.rules, self.routes, "bond0", self.gateway, own), [])

    def test_route_without_gateway(self):
        routes = {b.table: (None, b.address) for b in self.bindings}
        routes.pop(TABLE_BASE)
        commands = plan_commands(self.bindings, self.rules, routes, "eth0", None, {})
        self.assertEqual(
            commands, [f"ip route replace default dev eth0 src 1.2.3.4 table {TABLE_BASE}"]
        )


class ClearCommandsTest(unittest.TestCase):
    def test_removes_only_our_rules_and_tables(self):
        commands = clear_commands({MARK_BASE: (RULE_PRIORITY_BASE, TABLE_BASE)}, {TABLE_BASE: ("1.2.3.1", "1.2.3.4")})
        self.assertEqual(
            commands,
            [
                f"ip rule del fwmark {MARK_BASE} priority {RULE_PRIORITY_BASE} 2>/dev/null || true",
                f"ip route flush table {TABLE_BASE} 2>/dev/null || true",
            ],
        )


class ConfigValidationTest(unittest.TestCase):
    def test_excluded_addresses_normalised(self):
        config = SourcePoolConfig(enabled=True, excluded=[" 1.2.3.5 ", "1.2.3.4", "1.2.3.5"])
        self.assertEqual(config.excluded, ["1.2.3.4", "1.2.3.5"])

    def test_non_ipv4_rejected(self):
        with self.assertRaises(Exception):
            SourcePoolConfig(excluded=["not-an-ip"])


@dataclass
class FakeResult:
    success: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str = ""


class FakeExecutor:
    def __init__(self, probe_output: str):
        self.probe_output = probe_output
        self.commands: list[str] = []

    async def execute(self, command: str, timeout: int = 30, shell: str = "sh") -> FakeResult:
        self.commands.append(command)
        if "rule show" in command:
            return FakeResult(stdout=self.probe_output)
        return FakeResult()


class ManagerReconcileTest(unittest.TestCase):
    """Раскладка целиком: от адресов интерфейса до команд на хосте."""

    def _probe_output(self, rules=None, routes=None, gateway="1.2.3.1"):
        default = [{"dst": "default", "gateway": gateway, "dev": "bond0"}] if gateway else []
        return (
            "#RULES\n" + json.dumps(rules or []) + "\n"
            "#TABLES\n" + json.dumps(routes or []) + "\n"
            "#DEFAULT\n" + json.dumps(default) + "\n"
        )

    def _manager(self, tmp_path, probe_output):
        from pathlib import Path

        from app.services.source_pool import SourcePoolManager

        executor = FakeExecutor(probe_output)
        return SourcePoolManager(executor, state_path=Path(tmp_path)), executor

    def test_enabled_pool_writes_rules_for_every_mark(self):
        import tempfile
        import unittest.mock

        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "source_pool.json")
            manager, executor = self._manager(state_path, self._probe_output())

            from app.services import source_pool as module

            discovery = module._Discovery("bond0", ["1.2.3.4", "1.2.3.5"], None)
            with unittest.mock.patch.object(module, "discover_addresses", _fake_discover(discovery)), \
                 unittest.mock.patch.object(module, "exit_proxy_enabled", return_value=False):
                asyncio.run(manager.apply_config(SourcePoolConfig(enabled=True)))

            written = "\n".join(executor.commands)
            for index in range(MARK_COUNT):
                self.assertIn(f"ip rule add fwmark {MARK_BASE + index} ", written)
            self.assertIn(f"src 1.2.3.4 table {TABLE_BASE}", written)
            self.assertIn(f"src 1.2.3.5 table {TABLE_BASE + 1}", written)

    def test_exit_proxy_blocks_enabling(self):
        import tempfile
        import unittest.mock

        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "source_pool.json")
            manager, executor = self._manager(state_path, self._probe_output())

            from app.services import source_pool as module

            discovery = module._Discovery("bond0", ["1.2.3.4", "1.2.3.5"], None)
            with unittest.mock.patch.object(module, "discover_addresses", _fake_discover(discovery)), \
                 unittest.mock.patch.object(module, "exit_proxy_enabled", return_value=True):
                state = asyncio.run(manager.apply_config(SourcePoolConfig(enabled=True)))

            self.assertFalse(state.enabled)
            self.assertIsNotNone(state.conflict)
            self.assertFalse(any("ip rule add" in c for c in executor.commands))


def _fake_discover(discovery):
    """Каждый вызов должен давать свежую корутину: apply_config зовёт обнаружение
    и в reconcile, и в state()."""

    async def _discover():
        return discovery

    return _discover


if __name__ == "__main__":
    unittest.main()
