"""Ответы через свою карту: разбор `status`, менеджер на FakeExecutor, юнит и сам скрипт.

Запуск из node/:  python -m unittest tests.test_nic_routing

Закреплено: таблицы и приоритеты не пересекаются с пулом исходящих адресов и
доп. IP, правило подсети стоит раньше suppress-правила доп. IP, а правило по
источнику — после их правил; включение сначала делает проход и только потом
включает службу, выключение сначала останавливает службу и только потом снимает
правила (иначе служба вернула бы их); просмотр состояния кладёт на хост только
скрипт, юнит — только включение; служба перезапускается после обновления агента,
только если она включена и скрипт поменялся.
"""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import unittest
import unittest.mock
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.source_pool import MARK_COUNT, RULE_PRIORITY_BASE, TABLE_BASE as POOL_TABLE_BASE  # noqa: E402
from app.services import nic_routing  # noqa: E402
from app.services.extra_ips import GATEWAY_TABLE_MAX, GATEWAY_TABLE_MIN  # noqa: E402
from app.services.extra_ips import HOST_SCRIPT as EXTRA_IPS_SCRIPT  # noqa: E402
from app.services.nic_routing import (  # noqa: E402
    HOST_SCRIPT,
    HOST_SCRIPT_PATH,
    UNIT,
    UNIT_PATH,
    NicRoutingError,
    NicRoutingManager,
    parse_status,
)

STATUS = """ENABLED=enabled
ACTIVE=active
NIC eth0 primary 20002 192.168.0.1 192.168.0.106/24
NIC eth1 routed 20003 192.168.0.1 192.168.0.127/24,192.168.0.200/24
NIC eth2 no_gateway 20004 - 10.9.0.5/24
NIC eth3 unknown_role 20005 - 10.8.0.5/24
IN_SYNC=yes
RULES=5
"""


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
        return FakeResult()


def run(coro):
    return asyncio.run(coro)


def script_constant(script: str, name: str) -> int:
    match = re.search(rf"^{name}=(\d+)", script, re.MULTILINE)
    assert match, name
    return int(match.group(1))


def manager(answers: dict[str, FakeResult], installed: bool = True) -> tuple[NicRoutingManager, FakeExecutor]:
    executor = FakeExecutor(answers)
    instance = NicRoutingManager(executor)
    if installed:
        instance._installed = {
            HOST_SCRIPT_PATH: nic_routing.hashlib.sha256(HOST_SCRIPT.encode()).hexdigest(),
            UNIT_PATH: nic_routing.hashlib.sha256(UNIT.encode()).hexdigest(),
        }
    return instance, executor


class ParseStatusTests(unittest.TestCase):
    def test_roles_gateway_and_addresses(self):
        interfaces, in_sync, rules = parse_status(STATUS)
        self.assertEqual([(i.name, i.role) for i in interfaces],
                         [("eth0", "primary"), ("eth1", "routed"), ("eth2", "no_gateway")])
        self.assertEqual(interfaces[1].table, 20003)
        self.assertEqual(interfaces[1].addresses, ["192.168.0.127/24", "192.168.0.200/24"])
        self.assertIsNone(interfaces[2].gateway)
        self.assertTrue(in_sync)
        self.assertEqual(rules, 5)

    def test_empty_output(self):
        self.assertEqual(parse_status(""), ([], False, 0))


class RangeTests(unittest.TestCase):
    def test_tables_and_priorities_do_not_overlap_the_agent(self):
        base = script_constant(HOST_SCRIPT, "TABLE_BASE")
        last = script_constant(HOST_SCRIPT, "TABLE_LAST")
        own_tables = range(base, last + 1)
        for table in (*range(POOL_TABLE_BASE, POOL_TABLE_BASE + MARK_COUNT), *range(GATEWAY_TABLE_MIN, GATEWAY_TABLE_MAX + 1)):
            self.assertNotIn(table, own_tables)
        subnet = script_constant(HOST_SCRIPT, "PRIO_SUBNET")
        suppress = script_constant(HOST_SCRIPT, "PRIO_SUPPRESS")
        source = script_constant(HOST_SCRIPT, "PRIO_SOURCE")
        foreign = {*range(RULE_PRIORITY_BASE, RULE_PRIORITY_BASE + MARK_COUNT),
                   script_constant(EXTRA_IPS_SCRIPT, "GW_SUPPRESS_PRIORITY"),
                   script_constant(EXTRA_IPS_SCRIPT, "GW_RULE_PRIORITY")}
        self.assertFalse({subnet, suppress, source} & foreign)
        # Подсеть — раньше suppress-правила доп. IP, по источнику — после их правил
        self.assertLess(subnet, script_constant(EXTRA_IPS_SCRIPT, "GW_SUPPRESS_PRIORITY"))
        self.assertGreater(suppress, script_constant(EXTRA_IPS_SCRIPT, "GW_RULE_PRIORITY"))
        self.assertLess(suppress, source)


class ManagerTests(unittest.TestCase):
    def test_state_enabled(self):
        instance, _ = manager({"is-enabled": FakeResult(stdout=STATUS)})
        state = run(instance.state())
        self.assertTrue(state.supported)
        self.assertTrue(state.enabled)
        self.assertTrue(state.active)
        self.assertTrue(state.in_sync)
        self.assertEqual(len(state.interfaces), 3)

    def test_disabled_is_in_sync_only_without_leftover_rules(self):
        clean = "ACTIVE=inactive\nNIC eth1 routed 20003 192.168.0.1 192.168.0.127/24\nIN_SYNC=no\nRULES=0\n"
        state = run(manager({"is-enabled": FakeResult(stdout=clean)})[0].state())
        self.assertFalse(state.enabled)
        self.assertTrue(state.in_sync)
        leftover = clean.replace("RULES=0", "RULES=3")
        self.assertFalse(run(manager({"is-enabled": FakeResult(stdout=leftover)})[0].state()).in_sync)

    def test_state_without_status_output_is_unsupported(self):
        state = run(manager({"is-enabled": FakeResult(success=False, exit_code=127, stderr="bash: nic-routing.sh: not found")})[0].state())
        self.assertFalse(state.supported)
        self.assertIn("not found", state.message)

    def test_state_installs_the_script_but_not_the_unit(self):
        instance, executor = manager({"is-enabled": FakeResult(stdout=STATUS)}, installed=False)
        with unittest.mock.patch.object(nic_routing, "write_host_file", return_value=True) as write:
            run(instance.state())
        self.assertEqual([call.args[0] for call in write.call_args_list], [HOST_SCRIPT_PATH])
        self.assertFalse(any(UNIT_PATH in command for command in executor.commands))

    def test_enable_syncs_before_starting_the_service(self):
        instance, executor = manager({"is-enabled": FakeResult(stdout=STATUS)})
        run(instance.set_enabled(True))
        sync = next(i for i, c in enumerate(executor.commands) if c.endswith("nic-routing.sh sync"))
        enable = next(i for i, c in enumerate(executor.commands) if "systemctl enable --now mon-nic-routing.service" in c)
        self.assertLess(sync, enable)

    def test_disable_stops_the_service_before_clearing(self):
        instance, executor = manager({"is-enabled": FakeResult(stdout=STATUS)})
        run(instance.set_enabled(False))
        stop = next(i for i, c in enumerate(executor.commands) if "systemctl disable --now mon-nic-routing.service" in c)
        clear = next(i for i, c in enumerate(executor.commands) if c.endswith("nic-routing.sh clear"))
        self.assertLess(stop, clear)

    def test_failed_systemctl_raises(self):
        instance, _ = manager({"systemctl enable": FakeResult(success=False, exit_code=1, stderr="Failed to enable unit")})
        with self.assertRaises(NicRoutingError):
            run(instance.set_enabled(True))

    def test_start_restarts_an_enabled_service_on_a_new_script(self):
        instance, executor = manager({"is-enabled mon-nic-routing.service": FakeResult(stdout="enabled")}, installed=False)
        with unittest.mock.patch.object(nic_routing, "write_host_file", return_value=True):
            run(instance.start())
        self.assertTrue(any("systemctl daemon-reload" in c for c in executor.commands))
        self.assertTrue(any("systemctl restart mon-nic-routing.service" in c for c in executor.commands))

    def test_start_leaves_a_disabled_or_current_service_alone(self):
        disabled, executor = manager({"is-enabled mon-nic-routing.service": FakeResult(stdout="disabled")}, installed=False)
        run(disabled.start())
        self.assertFalse(any("restart" in c or "sha256sum" in c for c in executor.commands))
        current, executor = manager({"is-enabled mon-nic-routing.service": FakeResult(stdout="enabled")})
        run(current.start())
        self.assertFalse(any("restart" in c for c in executor.commands))


class ScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bash"), "bash not available")
    def test_script_has_valid_bash_syntax(self):
        # Байтами: текстовый stdin на Windows подменил бы LF на CRLF
        check = subprocess.run(["bash", "-n"], input=HOST_SCRIPT.encode("utf-8"), capture_output=True)
        self.assertEqual(check.returncode, 0, check.stderr.decode("utf-8", "replace"))

    def test_script_has_every_verb(self):
        for verb in ("sync", "watch", "clear", "status"):
            self.assertIsNotNone(re.search(rf"^\s+{verb}\)", HOST_SCRIPT, re.MULTILINE), f"verb {verb} missing")
        self.assertIn("set -u", HOST_SCRIPT)

    def test_unit_runs_the_watcher_and_restarts(self):
        for line in (f"ExecStart={HOST_SCRIPT_PATH} watch", "Restart=always", "WantedBy=multi-user.target"):
            self.assertIn(line, UNIT)


if __name__ == "__main__":
    unittest.main()
