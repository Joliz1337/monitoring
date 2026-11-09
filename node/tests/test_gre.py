"""GRE-туннели DDoS-Guard: схема, разбор `status`, проверка адресов хоста, проверка
с CE, менеджер на FakeExecutor, юнит и сам скрипт.

Запуск из node/:  python -m unittest tests.test_gre

Закреплено: PE и CE — два узла одной /30, GRE SRC не адрес туннеля, пара SRC/DST
уникальна (ядро не даст второй туннель без ключа); интерфейс начинается с `gre` —
иначе панель посчитала бы трафик туннеля второй раз в итогах сервера; таблицы и
приоритеты не пересекаются с пулом исходящих адресов, доп. IP и своей картой;
GRE SRC должен стоять на интерфейсе, CE — не занят чужим интерфейсом; применение
сначала делает проход и только потом включает службу, пустой список сначала
останавливает службу и только потом снимает туннели.
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

from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.gre import INTERFACE_PREFIX, MAX_TUNNELS, GreConfig, GreTunnelSpec, interface_name  # noqa: E402
from app.models.source_pool import MARK_COUNT, RULE_PRIORITY_BASE, TABLE_BASE as POOL_TABLE_BASE  # noqa: E402
from app.services import gre_tunnels  # noqa: E402
from app.services.extra_ips import GATEWAY_TABLE_MAX, GATEWAY_TABLE_MIN  # noqa: E402
from app.services.extra_ips import HOST_SCRIPT as EXTRA_IPS_SCRIPT  # noqa: E402
from app.services.gre_tunnels import (  # noqa: E402
    CONFIG_PATH,
    HOST_SCRIPT,
    HOST_SCRIPT_PATH,
    UNIT,
    UNIT_PATH,
    GreManager,
    GreValidationError,
    check_command,
    host_problems,
    parse_addresses,
    parse_check,
    parse_status,
    render_config,
)
from app.services.nic_routing import HOST_SCRIPT as NIC_ROUTING_SCRIPT  # noqa: E402

TUNNEL = {
    "slot": 1,
    "local": "178.214.103.5",
    "remote": "185.129.101.255",
    "pe": "91.215.40.173",
    "ce": "91.215.40.174",
    "health_net": "198.18.100.0/24",
}

ADDRESSES = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 178.214.103.5/24 brd 178.214.103.255 scope global eth0\\       valid_lft forever
7: gre-ddg1    inet 91.215.40.174/30 scope global gre-ddg1\\       valid_lft forever
"""

STATUS = """ENABLED=enabled
ACTIVE=active
TUN 1 178.214.103.5 185.129.101.255 91.215.40.173 91.215.40.174 198.18.100.0/24 ok 1476 -
TUN 2 10.0.0.5 185.129.101.255 91.215.40.177 91.215.40.178 198.18.100.0/24 conflict - ddosguard1
TUN 3 10.0.0.6 185.129.101.255 91.215.40.181 91.215.40.182 198.18.100.0/24 broken - -
IN_SYNC=yes
"""

PING_OK = """PING 91.215.40.173 (91.215.40.173) from 91.215.40.174 : 56(84) bytes of data.
64 bytes from 91.215.40.173: icmp_seq=1 ttl=64 time=11.9 ms

--- 91.215.40.173 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 1002ms
rtt min/avg/max/mdev = 11.214/12.430/13.902/1.101 ms
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


def tunnel(**changes) -> GreTunnelSpec:
    return GreTunnelSpec(**{**TUNNEL, **changes})


def manager(answers: dict[str, FakeResult]) -> tuple[GreManager, FakeExecutor]:
    executor = FakeExecutor(answers)
    instance = GreManager(executor)
    instance._installed = {
        HOST_SCRIPT_PATH: gre_tunnels.hashlib.sha256(HOST_SCRIPT.encode()).hexdigest(),
        UNIT_PATH: gre_tunnels.hashlib.sha256(UNIT.encode()).hexdigest(),
    }
    return instance, executor


class SpecTests(unittest.TestCase):
    def test_valid_tunnel(self):
        spec = tunnel(health_net="198.18.100.7/24")
        self.assertEqual(spec.health_net, "198.18.100.0/24")

    def test_pe_may_be_either_host_of_the_ptp_network(self):
        self.assertEqual(tunnel(pe="91.215.40.174", ce="91.215.40.173").pe, "91.215.40.174")

    def test_pe_and_ce_must_share_a_ptp_network(self):
        for pe, ce in (("91.215.40.172", "91.215.40.174"),   # адрес сети
                       ("91.215.40.175", "91.215.40.174"),   # broadcast
                       ("91.215.40.177", "91.215.40.174"),   # соседняя /30
                       ("91.215.40.174", "91.215.40.174")):
            with self.assertRaises(ValidationError, msg=f"{pe} {ce}"):
                tunnel(pe=pe, ce=ce)

    def test_local_is_not_a_tunnel_address(self):
        for local in ("91.215.40.173", "91.215.40.174", "185.129.101.255"):
            with self.assertRaises(ValidationError):
                tunnel(local=local)

    def test_not_ipv4(self):
        for field, value in (("ce", "2001:db8::1"), ("local", "0.0.0.0"), ("remote", "224.0.0.1"), ("health_net", "x")):
            with self.assertRaises(ValidationError, msg=field):
                tunnel(**{field: value})

    def test_config_uniqueness(self):
        second = {**TUNNEL, "slot": 2, "local": "178.214.103.6", "pe": "91.215.40.177", "ce": "91.215.40.178"}
        GreConfig(tunnels=[TUNNEL, second])
        for clash in ({"slot": 1}, {"ce": "91.215.40.174", "pe": "91.215.40.173"}, {"local": "178.214.103.5"}):
            with self.assertRaises(ValidationError, msg=str(clash)):
                GreConfig(tunnels=[TUNNEL, {**second, **clash}])

    def test_tunnel_limit(self):
        with self.assertRaises(ValidationError):
            tunnel(slot=MAX_TUNNELS + 1)

    def test_interface_is_left_out_of_traffic_totals(self):
        from app.services.metrics_collector import MetricsCollector
        self.assertTrue(interface_name(1).startswith(MetricsCollector.VIRTUAL_IFACE_PREFIXES))
        # IFNAMSIZ: 15 символов
        self.assertLessEqual(len(interface_name(MAX_TUNNELS)), 15)


class RenderAndParseTests(unittest.TestCase):
    def test_render_config_sorted_by_slot(self):
        second = {**TUNNEL, "slot": 2, "local": "178.214.103.6", "pe": "91.215.40.177", "ce": "91.215.40.178"}
        text = render_config(GreConfig(tunnels=[second, TUNNEL]))
        self.assertEqual(text.splitlines()[0], "1 178.214.103.5 185.129.101.255 91.215.40.173 91.215.40.174 198.18.100.0/24")
        self.assertTrue(text.splitlines()[1].startswith("2 "))
        self.assertEqual(render_config(GreConfig()), "")

    def test_parse_status_with_counters(self):
        counters = {"gre-ddg1": {"rx_bytes": 900, "rx_packets": 12, "tx_bytes": 700, "tx_packets": 10}}
        tunnels, in_sync = parse_status(STATUS, counters)
        self.assertTrue(in_sync)
        self.assertEqual([(t.slot, t.link) for t in tunnels], [(1, "ok"), (2, "conflict")])
        self.assertEqual(tunnels[0].name, "gre-ddg1")
        self.assertEqual(tunnels[0].mtu, 1476)
        self.assertEqual(tunnels[0].rx_packets, 12)
        self.assertIsNone(tunnels[1].mtu)
        self.assertEqual(tunnels[1].conflict_with, "ddosguard1")
        self.assertEqual(tunnels[1].rx_packets, 0)

    def test_parse_status_empty(self):
        self.assertEqual(parse_status("", {}), ([], False))

    def test_parse_addresses(self):
        addresses = parse_addresses(ADDRESSES)
        self.assertEqual(addresses["178.214.103.5"], "eth0")
        self.assertEqual(addresses["91.215.40.174"], "gre-ddg1")

    def test_host_problems(self):
        addresses = parse_addresses(ADDRESSES)
        self.assertEqual(host_problems(GreConfig(tunnels=[TUNNEL]), addresses), [])
        nat = host_problems(GreConfig(tunnels=[tunnel(local="203.0.113.9")]), addresses)
        self.assertIn("NAT", nat[0])
        taken = host_problems(GreConfig(tunnels=[TUNNEL]), {**addresses, "91.215.40.174": "eth0"})
        self.assertIn("already assigned to eth0", taken[0])

    def test_check_parsing(self):
        result = parse_check(1, PING_OK + "@@HTTPS\n91.215.40.174\n")
        self.assertEqual((result.ping_sent, result.ping_received), (3, 3))
        self.assertAlmostEqual(result.rtt_ms, 12.43)
        self.assertTrue(result.https_ok)
        self.assertEqual(result.external_ip, "91.215.40.174")
        failed = parse_check(1, "3 packets transmitted, 0 received, 100% packet loss\n@@HTTPS\ncurl: (28) Connection timed out\n")
        self.assertEqual(failed.ping_received, 0)
        self.assertIsNone(failed.rtt_ms)
        self.assertFalse(failed.https_ok)
        self.assertIn("timed out", failed.https_error)

    def test_check_command_binds_to_ce(self):
        command = check_command("91.215.40.174", "91.215.40.173")
        self.assertIn("-I 91.215.40.174 91.215.40.173", command)
        self.assertIn("--interface 91.215.40.174", command)
        self.assertIn("@@HTTPS", command)


class RangeTests(unittest.TestCase):
    def test_tables_and_priorities_do_not_overlap_the_agent(self):
        base = script_constant(HOST_SCRIPT, "TABLE_BASE")
        last = script_constant(HOST_SCRIPT, "TABLE_LAST")
        own_tables = range(base + 1, last + 1)
        self.assertEqual(len(own_tables), MAX_TUNNELS)
        foreign_tables = {
            *range(POOL_TABLE_BASE, POOL_TABLE_BASE + MARK_COUNT),
            *range(GATEWAY_TABLE_MIN, GATEWAY_TABLE_MAX + 1),
            *range(script_constant(NIC_ROUTING_SCRIPT, "TABLE_BASE"), script_constant(NIC_ROUTING_SCRIPT, "TABLE_LAST") + 1),
        }
        self.assertFalse(set(own_tables) & foreign_tables)
        own_priorities = {script_constant(HOST_SCRIPT, "PRIO_SUPPRESS"), script_constant(HOST_SCRIPT, "PRIO_SOURCE")}
        foreign_priorities = {
            *range(RULE_PRIORITY_BASE, RULE_PRIORITY_BASE + MARK_COUNT),
            script_constant(EXTRA_IPS_SCRIPT, "GW_SUPPRESS_PRIORITY"),
            script_constant(EXTRA_IPS_SCRIPT, "GW_RULE_PRIORITY"),
            script_constant(NIC_ROUTING_SCRIPT, "PRIO_SUBNET"),
            script_constant(NIC_ROUTING_SCRIPT, "PRIO_SUPPRESS"),
            script_constant(NIC_ROUTING_SCRIPT, "PRIO_SOURCE"),
        }
        self.assertFalse(own_priorities & foreign_priorities)
        # main без default раньше правила по источнику: docker и свои подсети идут по main
        self.assertLess(script_constant(HOST_SCRIPT, "PRIO_SUPPRESS"), script_constant(HOST_SCRIPT, "PRIO_SOURCE"))

    def test_interface_prefix_matches_the_script(self):
        self.assertIn(f'IF_PREFIX="{INTERFACE_PREFIX}"', HOST_SCRIPT)


class ManagerTests(unittest.TestCase):
    def answers(self, **extra: FakeResult) -> dict[str, FakeResult]:
        return {"ip -4 -o addr show": FakeResult(stdout=ADDRESSES), "is-enabled": FakeResult(stdout=STATUS), **extra}

    def test_state(self):
        instance, _ = manager(self.answers())
        with unittest.mock.patch.object(gre_tunnels, "read_net_dev", return_value={}):
            state = run(instance.state())
        self.assertTrue(state.supported)
        self.assertTrue(state.enabled)
        self.assertTrue(state.active)
        self.assertEqual(len(state.tunnels), 2)

    def test_state_without_status_output_is_unsupported(self):
        instance, _ = manager({"is-enabled": FakeResult(success=False, exit_code=127, stderr="gre-tunnels.sh: not found")})
        state = run(instance.state())
        self.assertFalse(state.supported)
        self.assertIn("not found", state.message)

    def test_apply_writes_the_list_and_syncs_before_enabling(self):
        instance, executor = manager(self.answers())
        with unittest.mock.patch.object(gre_tunnels, "write_host_file", return_value=True) as write, \
                unittest.mock.patch.object(gre_tunnels, "read_net_dev", return_value={}):
            run(instance.apply(GreConfig(tunnels=[TUNNEL])))
        write.assert_called_once()
        self.assertEqual(write.call_args.args[0], CONFIG_PATH)
        self.assertIn("91.215.40.174", write.call_args.args[1])
        sync = next(i for i, c in enumerate(executor.commands) if c.endswith("gre-tunnels.sh sync"))
        enable = next(i for i, c in enumerate(executor.commands) if "systemctl enable --now mon-gre.service" in c)
        self.assertLess(sync, enable)

    def test_empty_list_stops_the_service_before_clearing(self):
        instance, executor = manager(self.answers())
        with unittest.mock.patch.object(gre_tunnels, "write_host_file", return_value=True), \
                unittest.mock.patch.object(gre_tunnels, "read_net_dev", return_value={}):
            run(instance.apply(GreConfig()))
        stop = next(i for i, c in enumerate(executor.commands) if "systemctl disable --now mon-gre.service" in c)
        clear = next(i for i, c in enumerate(executor.commands) if c.endswith("gre-tunnels.sh clear"))
        self.assertLess(stop, clear)

    def test_apply_refuses_an_address_missing_on_the_host(self):
        instance, executor = manager(self.answers())
        with unittest.mock.patch.object(gre_tunnels, "write_host_file", return_value=True) as write:
            with self.assertRaises(GreValidationError):
                run(instance.apply(GreConfig(tunnels=[tunnel(local="203.0.113.9")])))
        write.assert_not_called()
        self.assertFalse(any("systemctl" in c for c in executor.commands))

    def test_sync_errors_land_in_the_state(self):
        instance, _ = manager(self.answers(**{"gre-tunnels.sh sync": FakeResult(stderr="ip -4 link add: Operation not supported")}))
        with unittest.mock.patch.object(gre_tunnels, "write_host_file", return_value=True), \
                unittest.mock.patch.object(gre_tunnels, "read_net_dev", return_value={}):
            state = run(instance.apply(GreConfig(tunnels=[TUNNEL])))
        self.assertIn("not supported", state.last_error)

    def test_check_uses_the_configured_tunnel(self):
        instance, executor = manager(self.answers(**{"ping -n": FakeResult(stdout=PING_OK + "@@HTTPS\n91.215.40.174\n")}))
        with unittest.mock.patch.object(gre_tunnels, "read_net_dev", return_value={}):
            result = run(instance.check(1))
            self.assertTrue(result.https_ok)
            self.assertTrue(any("-I 91.215.40.174 91.215.40.173" in c for c in executor.commands))
            with self.assertRaises(GreValidationError):
                run(instance.check(5))

    def test_start_restarts_an_enabled_service_on_a_new_script(self):
        executor = FakeExecutor({"is-enabled mon-gre.service": FakeResult(stdout="enabled")})
        instance = GreManager(executor)
        with unittest.mock.patch.object(gre_tunnels, "write_host_file", return_value=True):
            run(instance.start())
        self.assertTrue(any("systemctl restart mon-gre.service" in c for c in executor.commands))

    def test_start_leaves_a_disabled_service_alone(self):
        executor = FakeExecutor({"is-enabled mon-gre.service": FakeResult(stdout="disabled")})
        run(GreManager(executor).start())
        self.assertFalse(any("restart" in c or "sha256sum" in c for c in executor.commands))


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
        self.assertIn(CONFIG_PATH, HOST_SCRIPT)

    def test_unit_runs_the_watcher_and_restarts(self):
        for line in (f"ExecStart={HOST_SCRIPT_PATH} watch", "Restart=always", "WantedBy=multi-user.target"):
            self.assertIn(line, UNIT)


if __name__ == "__main__":
    unittest.main()
