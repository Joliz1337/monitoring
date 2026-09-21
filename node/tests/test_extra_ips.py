"""Дополнительные IP-адреса: разбор `ip -j`, детект бэкенда, рендер конфигов,
guard'ы, файлы состояния, план для host-скрипта и сам скрипт.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"

Инварианты, которые здесь закреплены: определение netplan резолвится по
set-name/MAC, а не только по имени (иначе появится второе определение того же
устройства); удалять можно только свои адреса и никогда — адрес панели или
основной; огороженный блок в /etc/network/interfaces заменяется, не трогая
байты вне него; таймаут nginx на /api/system/network/ покрывает apply.
"""

import asyncio
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.network import AddressSpec, NetworkApplyRequest  # noqa: E402
from app.services import extra_ips  # noqa: E402
from app.services.extra_ips import (  # noqa: E402
    APPLY_TIMEOUT_SEC,
    GUARD_UNIT,
    HOST_SCRIPT,
    IFUPDOWN_BLOCK_BEGIN,
    IFUPDOWN_BLOCK_END,
    PERSIST_UNIT,
    TX_ID_RE,
    Backend,
    BackendKind,
    ExtraIpBusyError,
    ExtraIpValidationError,
    ExtraIpManager,
    PlanFile,
    build_plan,
    check_request,
    choose_backend,
    new_transaction_id,
    parse_apply_output,
    parse_default_routes,
    parse_history,
    parse_ip_addr,
    parse_managed,
    parse_netplan_definitions,
    parse_transaction,
    primary_addresses,
    render_ifupdown_stanzas,
    render_managed,
    render_netplan,
    render_networkd_dropin,
    resolve_netplan_definition,
    splice_ifupdown_block,
)
from app.services.host_executor import ExecuteResult  # noqa: E402
from app.services.net_interfaces import parse_interface_listing  # noqa: E402


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


def execute_result(exit_code: int, stdout: str = "", stderr: str = "", error: str | None = None) -> ExecuteResult:
    return ExecuteResult(success=exit_code == 0, exit_code=exit_code, stdout=stdout, stderr=stderr,
                         execution_time_ms=1, error=error)


IP_ADDR_JSON = """[
 {"ifindex":1,"ifname":"lo","operstate":"UNKNOWN","addr_info":[
   {"family":"inet","local":"127.0.0.1","prefixlen":8,"scope":"host"}]},
 {"ifindex":2,"ifname":"eth0","operstate":"UP","addr_info":[
   {"family":"inet","local":"203.0.113.10","prefixlen":24,"scope":"global","dynamic":true},
   {"family":"inet","local":"203.0.113.11","prefixlen":32,"scope":"global","secondary":true},
   {"family":"inet6","local":"2001:db8::10","prefixlen":64,"scope":"global"},
   {"family":"inet6","local":"2001:db8::11","prefixlen":128,"scope":"global","tentative":true},
   {"family":"inet6","local":"fe80::1","prefixlen":64,"scope":"link"}]},
 {"ifindex":3,"ifname":"eth1","operstate":"UP","addr_info":[
   {"family":"inet","local":"10.0.0.5","prefixlen":24,"scope":"global"}]}
]"""

ROUTE4_JSON = '[{"dst":"default","gateway":"203.0.113.1","dev":"eth0","protocol":"dhcp","prefsrc":"203.0.113.10"}]'
ROUTE6_JSON = '[{"dst":"default","gateway":"fe80::1","dev":"eth0","protocol":"ra"}]'
ROUTE6_MULTIPATH_JSON = ('[{"dst":"default","protocol":"ra","metric":1024,"nexthops":['
                         '{"gateway":"fe80::1","dev":"bond0","weight":1},{"gateway":"fe80::2","dev":"bond0","weight":1}]}]')
LISTING = """lo unknown other no
eth0 up physical no
eth1 up physical yes
eth2 down physical no
bond0 up bond no
bond0.10 up vlan no
docker0 up bridge no
br0 up bridge no
veth1234 up other yes
wg0 unknown other no
"""

NETPLAN_GET = """version: 2
ethernets:
  id0:
    dhcp4: true
    match:
      macaddress: "52:54:00:AA:BB:CC"
    set-name: ens3
  eth0:
    dhcp4: true
    addresses:
    - "198.51.100.5/24"
bonds:
  bond0:
    interfaces:
    - eth1
    - eth2
vlans:
  vlan10:
    id: 10
    link: bond0
"""


class ParseIpAddrTests(unittest.TestCase):
    def test_host_and_link_scopes_are_hidden(self):
        interfaces = parse_ip_addr(IP_ADDR_JSON)
        self.assertNotIn("127.0.0.1/8", [a.cidr for a in interfaces["lo"].addresses])
        eth0 = [a.cidr for a in interfaces["eth0"].addresses]
        self.assertEqual(eth0, ["203.0.113.10/24", "203.0.113.11/32", "2001:db8::10/64", "2001:db8::11/128"])

    def test_family_and_dynamic_flags(self):
        eth0 = parse_ip_addr(IP_ADDR_JSON)["eth0"].addresses
        self.assertEqual([a.family for a in eth0], ["ipv4", "ipv4", "ipv6", "ipv6"])
        self.assertTrue(eth0[0].dynamic)
        self.assertFalse(eth0[1].dynamic)

    def test_garbage_is_empty(self):
        self.assertEqual(parse_ip_addr("not json"), {})
        self.assertEqual(parse_ip_addr(""), {})

    def test_default_routes(self):
        routes = parse_default_routes(ROUTE4_JSON, ROUTE6_JSON)
        self.assertEqual(routes["ipv4"], ("eth0", "203.0.113.10"))
        self.assertEqual(routes["ipv6"], ("eth0", ""))
        self.assertEqual(parse_default_routes("", "garbage"), {})

    def test_multipath_route_takes_dev_from_nexthops(self):
        routes = parse_default_routes("", ROUTE6_MULTIPATH_JSON)
        self.assertEqual(routes["ipv6"], ("bond0", ""))

    def test_interface_listing_keeps_only_address_bearing_interfaces(self):
        listing = parse_interface_listing(LISTING)
        self.assertEqual([(i.name, i.is_up, i.kind) for i in listing], [
            ("eth0", True, "physical"), ("eth2", False, "physical"),
            ("bond0", True, "bond"), ("bond0.10", True, "vlan"), ("br0", True, "bridge"),
        ])


class PrimaryAddressTests(unittest.TestCase):
    def test_prefsrc_wins_and_first_unmanaged_global_otherwise(self):
        eth0 = parse_ip_addr(IP_ADDR_JSON)["eth0"]
        routes = parse_default_routes(ROUTE4_JSON, ROUTE6_JSON)
        primary = primary_addresses(eth0, routes, managed={"2001:db8::10/64"})
        # IPv4 — prefsrc маршрута; IPv6 без prefsrc — первый global, не наш
        self.assertEqual(primary, {"203.0.113.10/24", "2001:db8::11/128"})

    def test_route_on_other_interface_is_ignored(self):
        eth1 = parse_ip_addr(IP_ADDR_JSON)["eth1"]
        routes = parse_default_routes(ROUTE4_JSON, "")
        self.assertEqual(primary_addresses(eth1, routes, set()), {"10.0.0.5/24"})


class NetplanDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.defs = parse_netplan_definitions(NETPLAN_GET)

    def test_sections_and_properties(self):
        self.assertEqual(set(self.defs), {"ethernets", "bonds", "vlans"})
        self.assertEqual(self.defs["ethernets"]["id0"], {"macaddress": "52:54:00:aa:bb:cc", "set-name": "ens3"})
        self.assertEqual(self.defs["ethernets"]["eth0"], {})
        self.assertIn("bond0", self.defs["bonds"])
        self.assertIn("vlan10", self.defs["vlans"])

    def test_resolve_by_set_name_name_and_mac(self):
        self.assertEqual(resolve_netplan_definition(self.defs, "ens3", ""), ("ethernets", "id0"))
        self.assertEqual(resolve_netplan_definition(self.defs, "eth0", ""), ("ethernets", "eth0"))
        self.assertEqual(resolve_netplan_definition(self.defs, "enp1s0", "52:54:00:AA:BB:CC"), ("ethernets", "id0"))
        self.assertEqual(resolve_netplan_definition(self.defs, "bond0", ""), ("bonds", "bond0"))
        self.assertIsNone(resolve_netplan_definition(self.defs, "eth9", "00:00:00:00:00:09"))

    def test_network_wrapper_is_tolerated(self):
        wrapped = "network:\n" + "\n".join("  " + line for line in NETPLAN_GET.splitlines())
        self.assertEqual(parse_netplan_definitions(wrapped), self.defs)


class ChooseBackendTests(unittest.TestCase):
    def facts(self, **extra):
        facts = {"NETPLAN": "yes", "NETPLAN_GET_B64": base64.b64encode(NETPLAN_GET.encode()).decode()}
        facts.update(extra)
        return facts

    def test_netplan_when_it_defines_the_interface(self):
        backend = choose_backend(self.facts(NETWORKD_FILE="/run/systemd/network/10-netplan-eth0.network"), "eth0", "")
        self.assertEqual(backend.kind, BackendKind.NETPLAN)
        self.assertEqual(backend.detail, "ethernets/eth0")

    def test_netplan_without_definition_falls_through_to_networkd(self):
        backend = choose_backend(self.facts(NETWORKD_FILE="/etc/systemd/network/20-wired.network"), "eth9", "")
        self.assertEqual(backend.kind, BackendKind.NETWORKD)
        self.assertEqual(backend.networkd_file, "/etc/systemd/network/20-wired.network")

    def test_networkd_run_files_belong_to_netplan(self):
        backend = choose_backend({"NETPLAN": "no", "NETWORKD_FILE": "/run/systemd/network/10-netplan-eth0.network"}, "eth0", "")
        self.assertEqual(backend.kind, BackendKind.FALLBACK)

    def test_networkmanager_requires_keyfile(self):
        with_keyfile = {"NETPLAN": "no", "NM_CONNECTION": "Wired connection 1",
                        "NM_KEYFILE": "/etc/NetworkManager/system-connections/w.nmconnection", "NM_IPV6_METHOD": "auto"}
        self.assertEqual(choose_backend(with_keyfile, "eth0", "").kind, BackendKind.NETWORKMANAGER)
        self.assertEqual(choose_backend({"NETPLAN": "no", "NM_CONNECTION": "x", "NM_KEYFILE": ""}, "eth0", "").kind,
                         BackendKind.FALLBACK)

    def test_ifupdown_sourced_flag(self):
        backend = choose_backend({"NETPLAN": "no", "IFUPDOWN": "yes", "IFUPDOWN_SOURCED": "yes"}, "eth0", "")
        self.assertEqual(backend.kind, BackendKind.IFUPDOWN)
        self.assertTrue(backend.ifupdown_sourced)
        self.assertEqual(backend.detail, extra_ips.IFUPDOWN_DROPIN)
        plain = choose_backend({"NETPLAN": "no", "IFUPDOWN": "yes", "IFUPDOWN_SOURCED": "no"}, "eth0", "")
        self.assertEqual(plain.detail, extra_ips.IFUPDOWN_FILE)


class RenderTests(unittest.TestCase):
    def test_netplan_groups_by_section_and_quotes_addresses(self):
        text = render_netplan({("ethernets", "eth0"): ["203.0.113.11/32", "2001:db8::11/64"], ("bonds", "bond0"): ["10.0.0.9/32"]})
        self.assertIn("network:\n  version: 2\n  ethernets:\n    eth0:\n      addresses:\n        - \"203.0.113.11/32\"\n        - \"2001:db8::11/64\"\n  bonds:\n    bond0:\n", text)
        self.assertNotIn("vlans", text)
        self.assertTrue(text.endswith("\n"))

    def test_networkd_dropin(self):
        text = render_networkd_dropin(["203.0.113.11/32", "2001:db8::11/64"])
        self.assertIn("[Network]\nAddress=203.0.113.11/32\nAddress=2001:db8::11/64\n", text)
        self.assertEqual(extra_ips.networkd_dropin_path("/etc/systemd/network/20-wired.network"),
                         "/etc/systemd/network/20-wired.network.d/monitoring-extra-ips.conf")

    def test_ifupdown_stanzas(self):
        text = render_ifupdown_stanzas({"eth0": ["203.0.113.11/32", "2001:db8::11/64"]})
        self.assertEqual(text, "iface eth0 inet static\n    address 203.0.113.11/32\niface eth0 inet6 static\n    address 2001:db8::11/64\n")
        self.assertEqual(render_ifupdown_stanzas({}), "")


class SpliceTests(unittest.TestCase):
    ORIGINAL = "auto lo\niface lo inet loopback\n\nauto eth0\niface eth0 inet dhcp\n"
    STANZAS = "iface eth0 inet static\n    address 203.0.113.11/32\n"

    def test_insert_replace_remove(self):
        inserted = splice_ifupdown_block(self.ORIGINAL, self.STANZAS)
        self.assertTrue(inserted.startswith(self.ORIGINAL))
        self.assertIn(f"\n\n{IFUPDOWN_BLOCK_BEGIN}\n{self.STANZAS}{IFUPDOWN_BLOCK_END}\n", inserted)
        replaced = splice_ifupdown_block(inserted, "iface eth0 inet static\n    address 203.0.113.12/32\n")
        self.assertEqual(replaced.count(IFUPDOWN_BLOCK_BEGIN), 1)
        self.assertNotIn("203.0.113.11", replaced)
        self.assertTrue(replaced.startswith(self.ORIGINAL))
        removed = splice_ifupdown_block(replaced, "")
        self.assertEqual(removed, self.ORIGINAL)

    def test_empty_file_gets_only_the_block(self):
        text = splice_ifupdown_block("", self.STANZAS)
        self.assertTrue(text.startswith(IFUPDOWN_BLOCK_BEGIN))
        self.assertEqual(splice_ifupdown_block("", ""), "")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.interfaces = parse_ip_addr(IP_ADDR_JSON)
        self.physical = {"eth0": True, "eth1": True, "eth2": False}
        self.managed = [("eth0", "203.0.113.11/32"), ("eth0", "2001:db8::10/64")]
        self.primary = {"203.0.113.10/24", "2001:db8::11/128"}

    def request(self, **kwargs):
        base = {"interface": "eth0", "protected": ["203.0.113.10"]}
        base.update(kwargs)
        return NetworkApplyRequest(**base)

    def check(self, request):
        return check_request(request, self.interfaces, self.physical, self.managed, self.primary)

    def test_add_new_address(self):
        add, remove = self.check(self.request(add=[{"address": "203.0.113.12", "prefix": 32}]))
        self.assertEqual([s.cidr for s in add], ["203.0.113.12/32"])
        self.assertEqual(remove, [])

    def test_add_of_managed_present_address_is_skipped(self):
        with self.assertRaises(ExtraIpValidationError):
            self.check(self.request(add=[{"address": "203.0.113.11", "prefix": 32}]))

    def test_add_of_hoster_address_is_refused(self):
        with self.assertRaises(ExtraIpValidationError):
            self.check(self.request(add=[{"address": "203.0.113.10", "prefix": 32}]))

    def test_add_of_address_used_on_other_interface_is_refused(self):
        with self.assertRaises(ExtraIpValidationError):
            self.check(self.request(add=[{"address": "10.0.0.5", "prefix": 32}]))

    def test_remove_only_managed(self):
        with self.assertRaises(ExtraIpValidationError):
            self.check(self.request(remove=[{"address": "203.0.113.10", "prefix": 24}]))
        add, remove = self.check(self.request(remove=[{"address": "203.0.113.11", "prefix": 32}]))
        self.assertEqual([s.cidr for s in remove], ["203.0.113.11/32"])

    def test_remove_protected_and_primary_refused(self):
        managed = self.managed + [("eth0", "203.0.113.10/24"), ("eth0", "2001:db8::11/128")]
        with self.assertRaises(ExtraIpValidationError):
            check_request(self.request(remove=[{"address": "203.0.113.10", "prefix": 24}]),
                          self.interfaces, self.physical, managed, self.primary)
        with self.assertRaises(ExtraIpValidationError):
            check_request(self.request(protected=[], remove=[{"address": "2001:db8::11", "prefix": 128}]),
                          self.interfaces, self.physical, managed, self.primary)

    def test_interface_must_be_physical_and_up(self):
        with self.assertRaises(ExtraIpValidationError):
            self.check(self.request(interface="docker0", add=[{"address": "203.0.113.12", "prefix": 32}]))
        with self.assertRaises(ExtraIpValidationError):
            self.check(self.request(interface="eth2", add=[{"address": "203.0.113.12", "prefix": 32}]))

    def test_model_rejects_overlap_empty_and_bad_addresses(self):
        with self.assertRaises(ValueError):
            NetworkApplyRequest(interface="eth0")
        with self.assertRaises(ValueError):
            NetworkApplyRequest(interface="eth0", add=[{"address": "1.2.3.4", "prefix": 32}],
                                remove=[{"address": "1.2.3.4", "prefix": 32}])
        with self.assertRaises(ValueError):
            AddressSpec(address="127.0.0.1", prefix=8)
        with self.assertRaises(ValueError):
            AddressSpec(address="fe80::1", prefix=64)
        with self.assertRaises(ValueError):
            AddressSpec(address="1.2.3.4", prefix=33)
        self.assertEqual(AddressSpec(address="2001:DB8:0:0::2", prefix=64).cidr, "2001:db8::2/64")
        request = NetworkApplyRequest(interface="eth0", add=[{"address": "1.2.3.4", "prefix": 32}] * 2,
                                      protected=["not-an-ip", "1.2.3.1"])
        self.assertEqual(len(request.add), 1)
        self.assertEqual(request.protected, ["1.2.3.1"])


class StateFileTests(unittest.TestCase):
    def test_managed_round_trip(self):
        entries = [("eth0", "203.0.113.11/32"), ("eth0", "2001:db8::10/64"), ("eth1", "10.0.0.9/32")]
        self.assertEqual(parse_managed(render_managed(entries)), entries)
        self.assertEqual(parse_managed("eth0 1.2.3.4/32\neth0 1.2.3.4/32\n\nbroken\n"), [("eth0", "1.2.3.4/32")])

    def test_transaction_parse(self):
        text = ("TX_ID=20260902-101500-ab12\nTX_STATUS=pending\nTX_IFACE=eth0\nTX_BACKEND=netplan\n"
                "TX_ADD=203.0.113.11/32 2001:db8::10/64\nTX_REMOVE=\nTX_STARTED_AT=1788000000\n"
                "TX_DEADLINE_AT=1788000120\nTX_MESSAGE=\nTX_WARNINGS=runtime address a re-added; other\n")
        tx = parse_transaction(text)
        self.assertEqual(tx.id, "20260902-101500-ab12")
        self.assertEqual(tx.added, ["203.0.113.11/32", "2001:db8::10/64"])
        self.assertEqual(tx.removed, [])
        self.assertEqual(tx.warnings, ["runtime address a re-added", "other"])
        info = tx.to_info()
        self.assertEqual(info.deadline_at, "2026-08-29T10:42:00Z")
        self.assertIsNone(info.finished_at)
        self.assertIsNone(parse_transaction(""))

    def test_history_newest_first_with_limit(self):
        lines = [f"1788000{i:03d}\t1788001{i:03d}\ttx{i}\tconfirmed\teth0\tnetplan\t1.2.3.{i}/32\t-\tok\n" for i in range(30)]
        history = parse_history("".join(lines) + "broken line\n", limit=20)
        self.assertEqual(len(history), 20)
        self.assertEqual(history[0].id, "tx29")
        self.assertEqual(history[0].added, ["1.2.3.29/32"])
        self.assertEqual(history[0].removed, [])
        self.assertEqual(history[-1].id, "tx10")


class PlanTests(unittest.TestCase):
    def test_transaction_id_matches_script_pattern(self):
        self.assertRegex(new_transaction_id(), TX_ID_RE)

    def test_plan_text(self):
        backend = Backend(BackendKind.NETWORKMANAGER, detail="Wired", nm_connection="Wired", nm_keyfile="/etc/NetworkManager/system-connections/w.nmconnection")
        plan = build_plan(
            "20260902-101500-ab12", "eth0", backend,
            [AddressSpec(address="203.0.113.11", prefix=32)], [AddressSpec(address="203.0.113.12", prefix=32)],
            ["203.0.113.10"], 120, "eth0 203.0.113.11/32\n",
            [PlanFile("/etc/netplan/60-monitoring-extra-ips.yaml", "600", "network:\n"), PlanFile("/etc/systemd/network/x.d/m.conf", "644", None)],
        )
        self.assertIn("TX_ID=20260902-101500-ab12\nIFACE=eth0\nBACKEND=networkmanager\nDETAIL=Wired\nTIMEOUT=120\n", plan)
        self.assertIn("ADD=203.0.113.11/32\nREMOVE=203.0.113.12/32\nPROTECTED=203.0.113.10\n", plan)
        self.assertIn("MANAGED_B64=" + base64.b64encode(b"eth0 203.0.113.11/32\n").decode(), plan)
        self.assertIn("NM_CONNECTION=Wired\nNM_KEYFILE=/etc/NetworkManager/system-connections/w.nmconnection\n", plan)
        self.assertIn("FILE=600 /etc/netplan/60-monitoring-extra-ips.yaml " + base64.b64encode(b"network:\n").decode(), plan)
        self.assertIn("ABSENT=/etc/systemd/network/x.d/m.conf\n", plan)

    def test_apply_output_by_exit_code(self):
        ok = parse_apply_output(execute_result(0, "TX_ID=t\nTX_STATUS=pending\nTX_DEADLINE_AT=1788000120\nTX_WARNINGS=a; b\n"), BackendKind.NETPLAN)
        self.assertTrue(ok.success)
        self.assertEqual((ok.transaction_id, ok.status, ok.warnings), ("t", "pending", ["a", "b"]))
        self.assertEqual(ok.deadline_at, "2026-08-29T10:42:00Z")
        rolled = parse_apply_output(execute_result(4, "TX_ID=t\nTX_STATUS=failed\nTX_MESSAGE=verify failed\n", "extra-ips: verify failed"), BackendKind.NETPLAN)
        self.assertFalse(rolled.success)
        self.assertTrue(rolled.rolled_back)
        self.assertEqual(rolled.message, "verify failed")
        broken = parse_apply_output(execute_result(5, "TX_ID=t\nTX_STATUS=failed\n", "extra-ips: rollback incomplete"), BackendKind.NETPLAN)
        self.assertFalse(broken.rolled_back)
        self.assertIn("rollback incomplete", broken.message)
        timeout = parse_apply_output(execute_result(-1, error="Command timed out after 150 seconds"), BackendKind.NETPLAN)
        self.assertFalse(timeout.success)
        self.assertIn("timed out", timeout.message)


class ScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bash"), "bash not available")
    def test_script_has_valid_bash_syntax(self):
        check = subprocess.run(["bash", "-n"], input=HOST_SCRIPT, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_script_has_every_verb_and_accurate_timer(self):
        for verb in ("detect", "apply", "confirm", "rollback", "rollback-unconfirmed", "boot-guard", "restore-runtime", "self-test"):
            self.assertIsNotNone(re.search(rf"^\s+{re.escape(verb)}\)", HOST_SCRIPT, re.MULTILINE), f"verb {verb} missing")
        self.assertIn("--timer-property=AccuracySec=1s", HOST_SCRIPT)
        self.assertIn("set -u", HOST_SCRIPT)

    def test_units(self):
        for line in ("DefaultDependencies=no", "Before=network-pre.target", "Wants=network-pre.target",
                     "ConditionPathExists=/opt/monitoring/network/transaction.env", "extra-ips.sh boot-guard"):
            self.assertIn(line, GUARD_UNIT)
        for line in ("After=network-online.target", "extra-ips.sh restore-runtime", "WantedBy=multi-user.target"):
            self.assertIn(line, PERSIST_UNIT)


class NginxTimeoutTests(unittest.TestCase):
    TEMPLATE = Path(__file__).resolve().parents[1] / "nginx" / "templates" / "api.conf.template"

    def test_network_location_covers_apply_timeout(self):
        config = self.TEMPLATE.read_text(encoding="utf-8")
        header = re.search(r"location\s+/api/system/network/\s*\{", config)
        self.assertIsNotNone(header, "location /api/system/network/ is missing")
        block = config[header.end():config.index("}", header.end())]
        for directive in ("proxy_read_timeout", "proxy_send_timeout"):
            match = re.search(rf"{directive}\s+(\d+)s;", block)
            self.assertIsNotNone(match, directive)
            self.assertGreaterEqual(int(match.group(1)), APPLY_TIMEOUT_SEC)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_dir = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def manager(self, answers: dict[str, FakeResult]) -> tuple[ExtraIpManager, FakeExecutor]:
        executor = FakeExecutor(answers)
        manager = ExtraIpManager(executor, state_dir=self.state_dir)
        manager._installed_hash = extra_ips.hashlib.sha256(HOST_SCRIPT.encode()).hexdigest()
        return manager, executor

    def live_answers(self) -> dict[str, FakeResult]:
        return {
            "ip -j addr show": FakeResult(stdout=IP_ADDR_JSON),
            # Хост без IPv6: вторая команда падает, но код выхода блока всегда 0
            "route show default": FakeResult(stdout=ROUTE4_JSON + "\n@@\n"),
            "for d in /sys/class/net/*": FakeResult(stdout="eth0 up physical no\neth1 up physical no\n"),
            "extra-ips.sh detect": FakeResult(stdout="NETPLAN=yes\nNETPLAN_GET_B64=" + base64.b64encode(NETPLAN_GET.encode()).decode()),
        }

    def test_state_marks_default_managed_and_primary(self):
        (self.state_dir / "managed.list").write_text("eth0 203.0.113.11/32\n")
        manager, _ = self.manager(self.live_answers())
        with unittest.mock.patch.object(extra_ips, "default_interface", return_value="eth0"):
            state = run(manager.state())
        self.assertEqual([i.name for i in state.interfaces], ["eth0", "eth1"])
        self.assertTrue(state.interfaces[0].is_default)
        by_cidr = {f"{a.address}/{a.prefix}": a for a in state.interfaces[0].addresses}
        self.assertTrue(by_cidr["203.0.113.11/32"].managed)
        self.assertTrue(by_cidr["203.0.113.10/24"].primary)
        self.assertEqual(state.backend, "netplan")
        self.assertEqual(state.managed[0].address, "203.0.113.11")
        self.assertIsNone(state.transaction)

    def test_addr_read_failure_is_reported_not_hidden(self):
        answers = self.live_answers()
        answers["ip -j addr show"] = FakeResult(success=False, exit_code=2, stderr="Cannot open netlink socket")
        manager, _ = self.manager(answers)
        state = run(manager.state())
        self.assertFalse(state.supported)
        self.assertIn("Cannot open netlink socket", state.message)
        self.assertEqual(state.interfaces, [])

    def test_apply_builds_plan_and_calls_script(self):
        answers = self.live_answers()
        answers["extra-ips.sh apply"] = FakeResult(stdout="TX_ID=20260902-101500-ab12\nTX_STATUS=pending\nTX_DEADLINE_AT=1788000120\n")
        manager, executor = self.manager(answers)
        request = NetworkApplyRequest(interface="eth0", add=[{"address": "203.0.113.12", "prefix": 32}], protected=["203.0.113.10"])
        with unittest.mock.patch.object(ExtraIpManager, "_mac", return_value=""):
            response = run(manager.apply(request))
        self.assertTrue(response.success)
        self.assertEqual(response.status, "pending")
        apply_command = next(c for c in executor.commands if "extra-ips.sh apply" in c)
        encoded = re.search(r"printf '%s' '([A-Za-z0-9+/=]+)'", apply_command).group(1)
        plan = base64.b64decode(encoded).decode()
        self.assertIn("BACKEND=netplan\nDETAIL=ethernets/eth0\n", plan)
        self.assertIn("ADD=203.0.113.12/32\nREMOVE=\nPROTECTED=203.0.113.10\n", plan)
        file_line = next(line for line in plan.splitlines() if line.startswith("FILE=600 /etc/netplan/60-monitoring-extra-ips.yaml "))
        yaml = base64.b64decode(file_line.split()[2]).decode()
        self.assertIn("    eth0:\n      addresses:\n        - \"203.0.113.12/32\"\n", yaml)

    def test_pending_transaction_blocks_apply(self):
        (self.state_dir / "transaction.env").write_text("TX_ID=20260902-101500-ab12\nTX_STATUS=pending\n")
        manager, _ = self.manager(self.live_answers())
        request = NetworkApplyRequest(interface="eth0", add=[{"address": "203.0.113.12", "prefix": 32}])
        with self.assertRaises(ExtraIpBusyError):
            run(manager.apply(request))

    def test_confirm_and_rollback_use_verbs(self):
        manager, executor = self.manager({
            "extra-ips.sh confirm": FakeResult(stdout="TX_ID=t\nTX_STATUS=confirmed\nTX_MESSAGE=confirmed by the panel\n"),
            "extra-ips.sh rollback": FakeResult(exit_code=2, success=False, stderr="extra-ips: transaction t is not pending"),
        })
        confirmed = run(manager.confirm("20260902-101500-ab12"))
        self.assertEqual((confirmed.success, confirmed.status), (True, "confirmed"))
        self.assertIn("extra-ips.sh confirm 20260902-101500-ab12", executor.commands[-1])
        with self.assertRaises(ExtraIpValidationError):
            run(manager.rollback("20260902-101500-ab12"))

    def test_start_rolls_back_stale_transactions(self):
        (self.state_dir / "transaction.env").write_text("TX_ID=20260902-101500-ab12\nTX_STATUS=pending\nTX_DEADLINE_AT=1\n")
        manager, executor = self.manager({"rollback-unconfirmed": FakeResult()})
        run(manager.start())
        self.assertTrue(any("rollback-unconfirmed 20260902-101500-ab12" in c for c in executor.commands))
        (self.state_dir / "transaction.env").write_text("TX_ID=20260902-101500-ab12\nTX_STATUS=confirmed\n")
        manager, executor = self.manager({})
        run(manager.start())
        self.assertEqual(executor.commands, [])


if __name__ == "__main__":
    unittest.main()
