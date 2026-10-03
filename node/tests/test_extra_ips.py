"""Дополнительные IP-адреса: разбор `ip -j`, детект бэкенда, рендер конфигов,
guard'ы, файлы состояния, план для host-скрипта и сам скрипт.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"

Инварианты, которые здесь закреплены: определение netplan резолвится по
set-name/MAC, а не только по имени (иначе появится второе определение того же
устройства); свои адреса удаляются из конфига, адреса хостера снимаются поверх
него, и никогда — адрес панели, основной или DHCP; огороженный блок в /etc/network/interfaces заменяется, не трогая
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
    GATEWAY_TABLE_MAX,
    GATEWAY_TABLE_MIN,
    GUARD_UNIT,
    HOST_SCRIPT,
    IFUPDOWN_BLOCK_BEGIN,
    IFUPDOWN_BLOCK_END,
    PERSIST_UNIT,
    TX_ID_RE,
    AddressChange,
    Backend,
    BackendKind,
    DefaultRoute,
    ExtraIpBusyError,
    ExtraIpManager,
    ExtraIpUnsupportedError,
    ExtraIpValidationError,
    GatewayRoute,
    LiveAddr,
    LiveInterface,
    PlanFile,
    build_plan,
    check_request,
    choose_backend,
    new_transaction_id,
    parse_apply_output,
    parse_default_routes,
    parse_history,
    parse_address_list,
    parse_ip_addr,
    parse_link_list,
    parse_netplan_definitions,
    parse_routes,
    parse_transaction,
    plan_link,
    plan_routes,
    primary_addresses,
    render_address_list,
    render_ifupdown_stanzas,
    render_link_list,
    render_netplan,
    render_networkd_dropin,
    render_routes,
    resolve_netplan_definition,
    splice_ifupdown_block,
    without_default_gateways,
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
        self.assertEqual(routes["ipv4"], DefaultRoute("eth0", "203.0.113.10", "203.0.113.1"))
        self.assertEqual(routes["ipv6"], DefaultRoute("eth0", "", "fe80::1"))
        self.assertEqual(parse_default_routes("", "garbage"), {})

    def test_multipath_route_takes_dev_from_nexthops(self):
        routes = parse_default_routes("", ROUTE6_MULTIPATH_JSON)
        self.assertEqual(routes["ipv6"], DefaultRoute("bond0", "", "fe80::1"))

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

    def check(self, request, routes=(), suppressed=(), primary=None):
        return check_request(request, self.interfaces, self.physical, self.managed, list(suppressed),
                             self.primary if primary is None else primary, list(routes))

    def test_add_new_address(self):
        change = self.check(self.request(add=[{"address": "203.0.113.12", "prefix": 32}]))
        self.assertEqual([s.cidr for s in change.add], ["203.0.113.12/32"])
        self.assertEqual(change.disappearing, [])

    def test_add_of_managed_present_address_is_skipped(self):
        with self.assertRaises(ExtraIpValidationError):
            self.check(self.request(add=[{"address": "203.0.113.11", "prefix": 32}]))

    def test_add_of_hoster_address_is_refused(self):
        with self.assertRaises(ExtraIpValidationError):
            self.check(self.request(add=[{"address": "203.0.113.10", "prefix": 32}]))

    def test_add_of_address_used_on_other_interface_is_refused(self):
        with self.assertRaises(ExtraIpValidationError):
            self.check(self.request(add=[{"address": "10.0.0.5", "prefix": 32}]))

    def test_remove_splits_own_and_hoster_addresses(self):
        change = self.check(self.request(remove=[{"address": "203.0.113.11", "prefix": 32},
                                                 {"address": "2001:db8::11", "prefix": 128}]),
                            primary={"203.0.113.10/24"})
        self.assertEqual([s.cidr for s in change.remove], ["203.0.113.11/32"])
        self.assertEqual([s.cidr for s in change.suppress], ["2001:db8::11/128"])
        self.assertEqual([s.cidr for s in change.disappearing], ["203.0.113.11/32", "2001:db8::11/128"])

    def test_hoster_address_that_is_dhcp_or_absent_is_refused(self):
        with self.assertRaises(ExtraIpValidationError) as ctx:
            self.check(self.request(protected=[], remove=[{"address": "203.0.113.10", "prefix": 24}]), primary=set())
        self.assertIn("DHCP", str(ctx.exception))
        with self.assertRaises(ExtraIpValidationError):
            self.check(self.request(remove=[{"address": "198.51.100.9", "prefix": 32}]))

    def test_remove_protected_and_primary_refused(self):
        managed = self.managed + [("eth0", "203.0.113.10/24"), ("eth0", "2001:db8::11/128")]
        with self.assertRaises(ExtraIpValidationError):
            check_request(self.request(remove=[{"address": "203.0.113.10", "prefix": 24}]),
                          self.interfaces, self.physical, managed, [], self.primary, [])
        with self.assertRaises(ExtraIpValidationError):
            check_request(self.request(protected=[], remove=[{"address": "2001:db8::11", "prefix": 128}]),
                          self.interfaces, self.physical, managed, [], self.primary, [])
        # Адрес панели не снимается, даже если он настроен хостером и не основной
        with self.assertRaises(ExtraIpValidationError):
            self.check(self.request(protected=["2001:db8::11"], remove=[{"address": "2001:db8::11", "prefix": 128}]),
                       primary=set())

    def test_restore_only_what_the_panel_removed(self):
        suppressed = [("eth0", "198.51.100.9/32"), ("eth1", "10.0.0.9/32")]
        change = self.check(self.request(restore=[{"address": "198.51.100.9", "prefix": 32}]), suppressed=suppressed)
        self.assertEqual([s.cidr for s in change.restore], ["198.51.100.9/32"])
        self.assertEqual([s.cidr for s in change.appearing], ["198.51.100.9/32"])
        for spec in ({"address": "203.0.113.12", "prefix": 32}, {"address": "10.0.0.9", "prefix": 32}):
            with self.assertRaises(ExtraIpValidationError):
                self.check(self.request(restore=[spec]), suppressed=suppressed)

    def test_add_of_removed_hoster_address_points_to_restore(self):
        with self.assertRaises(ExtraIpValidationError) as ctx:
            self.check(self.request(add=[{"address": "198.51.100.9", "prefix": 32}]),
                       suppressed=[("eth0", "198.51.100.9/32")])
        self.assertIn("restore it instead", str(ctx.exception))

    def test_interface_must_carry_addresses(self):
        with self.assertRaises(ExtraIpValidationError):
            self.check(self.request(interface="docker0", add=[{"address": "203.0.113.12", "prefix": 32}]))

    def test_down_interface_takes_only_new_addresses(self):
        change = self.check(self.request(interface="eth2", add=[{"address": "198.51.100.7", "prefix": 32}]))
        self.assertEqual([s.cidr for s in change.add], ["198.51.100.7/32"])
        with self.assertRaises(ExtraIpValidationError) as ctx:
            self.check(self.request(interface="eth2", restore=[{"address": "198.51.100.8", "prefix": 32}]),
                       suppressed=[("eth2", "198.51.100.8/32")])
        self.assertIn("is down", str(ctx.exception))

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
        self.assertEqual(len(NetworkApplyRequest(interface="eth0", restore=[{"address": "1.2.3.4", "prefix": 32}]).restore), 1)
        with self.assertRaises(ValueError):
            NetworkApplyRequest(interface="eth0", restore=[{"address": "1.2.3.4", "prefix": 32}],
                                remove=[{"address": "1.2.3.4", "prefix": 32}])
        with self.assertRaises(ValueError):
            NetworkApplyRequest(interface="eth0", restore=[{"address": "1.2.3.4", "prefix": 32, "gateway": "1.2.3.1"}])

    def test_model_validates_gateway(self):
        self.assertEqual(AddressSpec(address="2001:db8::2", prefix=64, gateway="FE80::1").gateway, "fe80::1")
        self.assertIsNone(AddressSpec(address="1.2.3.4", prefix=32, gateway="").gateway)
        for bad in ("2001:db8::1", "not-ip", "224.0.0.1", "127.0.0.1", "0.0.0.0", "1.2.3.4"):
            with self.assertRaises(ValueError, msg=bad):
                AddressSpec(address="1.2.3.4", prefix=32, gateway=bad)
        with self.assertRaises(ValueError):
            NetworkApplyRequest(interface="eth0", add=[{"address": "1.2.3.4", "prefix": 32, "gateway": "1.2.3.5"},
                                                       {"address": "1.2.3.5", "prefix": 32}])

    def test_gateway_must_not_be_a_host_address(self):
        with self.assertRaises(ExtraIpValidationError):
            self.check(self.request(add=[{"address": "198.51.100.5", "prefix": 32, "gateway": "10.0.0.5"}]))

    def test_present_address_with_other_gateway_is_refused(self):
        routes = [GatewayRoute("eth0", "203.0.113.11", "198.51.100.1", 1001)]
        same = self.request(add=[{"address": "203.0.113.11", "prefix": 32, "gateway": "198.51.100.1"},
                                 {"address": "203.0.113.12", "prefix": 32}])
        change = self.check(same, routes)
        self.assertEqual([s.cidr for s in change.add], ["203.0.113.12/32"])
        for gateway in ("198.51.100.9", None):
            with self.assertRaises(ExtraIpValidationError) as ctx:
                self.check(self.request(add=[{"address": "203.0.113.11", "prefix": 32, "gateway": gateway}]), routes)
            self.assertIn("remove it and add it again", str(ctx.exception))


class LinkPlanTests(unittest.TestCase):
    def spec(self, address: str) -> AddressSpec:
        return AddressSpec(address=address, prefix=32)

    def addr(self, cidr: str, dynamic: bool = False) -> LiveAddr:
        address, prefix = cidr.split("/")
        return LiveAddr(address=address, prefix=int(prefix), family="ipv4", scope="global", dynamic=dynamic)

    def test_first_addresses_bring_a_down_card_up(self):
        change = AddressChange(add=[self.spec("198.51.100.7")])
        live = LiveInterface("eth2")
        self.assertEqual(plan_link("eth2", False, change, ["eth3"], [("eth2", "198.51.100.7/32")], live),
                         ("up", ["eth3", "eth2"]))
        # Уже в списке (линк уронили руками) — поднимаем, список не дублируем
        self.assertEqual(plan_link("eth2", False, change, ["eth2"], [], live), ("up", ["eth2"]))

    def test_cards_the_panel_did_not_bring_up_stay_as_they_are(self):
        change = AddressChange(remove=[self.spec("203.0.113.11")])
        self.assertEqual(plan_link("eth0", True, change, [], [], LiveInterface("eth0")), ("", []))

    def test_card_with_panel_addresses_left_stays_up(self):
        change = AddressChange(remove=[self.spec("198.51.100.7")])
        live = LiveInterface("eth2", [self.addr("198.51.100.7/32"), self.addr("198.51.100.8/32")])
        self.assertEqual(plan_link("eth2", True, change, ["eth2"], [("eth2", "198.51.100.8/32")], live),
                         ("", ["eth2"]))

    def test_last_panel_address_brings_the_card_down(self):
        change = AddressChange(remove=[self.spec("198.51.100.7")])
        live = LiveInterface("eth2", [self.addr("198.51.100.7/32")])
        self.assertEqual(plan_link("eth2", True, change, ["eth2", "eth3"], [], live), ("down", ["eth3"]))

    def test_foreign_address_keeps_the_link_up_but_not_ours(self):
        change = AddressChange(remove=[self.spec("198.51.100.7")])
        live = LiveInterface("eth2", [self.addr("198.51.100.7/32"), self.addr("10.0.0.20/24", dynamic=True)])
        self.assertEqual(plan_link("eth2", True, change, ["eth2"], [], live), ("", []))


class GatewayRouteTests(unittest.TestCase):
    def spec(self, address: str, gateway: str | None = None) -> AddressSpec:
        return AddressSpec(address=address, prefix=128 if ":" in address else 32, gateway=gateway)

    def test_round_trip_skips_broken_lines(self):
        routes = [GatewayRoute("eth0", "198.51.100.5", "198.51.100.1", 1001),
                  GatewayRoute("eth0", "2001:db8:9::5", "fe80::9", 1002)]
        self.assertEqual(parse_routes(render_routes(routes) + "broken\neth0 1.2.3.4 1.2.3.1 x\n"), routes)
        self.assertEqual(render_routes([]), "")

    def test_same_gateway_shares_a_table_other_gateway_gets_the_next(self):
        routes = plan_routes([], "eth0", [self.spec("198.51.100.5", "198.51.100.1"), self.spec("198.51.100.6", "198.51.100.1"),
                                          self.spec("192.0.2.7", "192.0.2.1"), self.spec("203.0.113.12")], [])
        self.assertEqual([(r.address, r.table) for r in routes],
                         [("198.51.100.5", GATEWAY_TABLE_MIN), ("198.51.100.6", GATEWAY_TABLE_MIN), ("192.0.2.7", GATEWAY_TABLE_MIN + 1)])

    def test_removed_address_takes_its_route_and_frees_the_table(self):
        current = [GatewayRoute("eth0", "198.51.100.5", "198.51.100.1", 1001), GatewayRoute("eth0", "192.0.2.7", "192.0.2.1", 1002)]
        routes = plan_routes(current, "eth0", [self.spec("203.0.113.9", "203.0.113.1")], [self.spec("198.51.100.5")])
        self.assertEqual([(r.address, r.table) for r in routes], [("192.0.2.7", 1002), ("203.0.113.9", 1001)])
        other_iface = plan_routes(current, "eth1", [], [self.spec("198.51.100.5")])
        self.assertEqual(other_iface, current)

    def test_tables_run_out(self):
        full = [GatewayRoute("eth0", f"10.1.{t // 256}.{t % 256}", f"10.2.{t // 256}.{t % 256}", t)
                for t in range(GATEWAY_TABLE_MIN, GATEWAY_TABLE_MAX + 1)]
        with self.assertRaises(ExtraIpValidationError):
            plan_routes(full, "eth0", [self.spec("198.51.100.5", "198.51.100.1")], [])
        shared = plan_routes(full, "eth0", [self.spec("198.51.100.5", full[0].gateway)], [])
        self.assertEqual(shared[-1].table, GATEWAY_TABLE_MIN)

    def test_main_gateway_means_no_own_route(self):
        defaults = parse_default_routes(ROUTE4_JSON, ROUTE6_JSON)
        specs = without_default_gateways(
            [self.spec("198.51.100.5", "203.0.113.1"), self.spec("198.51.100.6", "198.51.100.1"),
             self.spec("2001:db8:9::5", "fe80::1")], defaults,
        )
        self.assertEqual([s.gateway for s in specs], [None, "198.51.100.1", None])


class StateFileTests(unittest.TestCase):
    def test_address_list_round_trip(self):
        entries = [("eth0", "203.0.113.11/32"), ("eth0", "2001:db8::10/64"), ("eth1", "10.0.0.9/32")]
        self.assertEqual(parse_address_list(render_address_list(entries)), entries)
        self.assertEqual(parse_address_list("eth0 1.2.3.4/32\neth0 1.2.3.4/32\n\nbroken\n"), [("eth0", "1.2.3.4/32")])

    def test_link_list_round_trip(self):
        self.assertEqual(parse_link_list(render_link_list(["eth2", "ens4"])), ["eth2", "ens4"])
        self.assertEqual(parse_link_list("eth2\n\n eth2 \nens4\n"), ["eth2", "ens4"])
        self.assertEqual(render_link_list([]), "")

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
        spec = lambda address: AddressSpec(address=address, prefix=32)  # noqa: E731
        change = AddressChange(add=[spec("203.0.113.11")], remove=[spec("203.0.113.12")],
                               suppress=[spec("203.0.113.13")], restore=[spec("203.0.113.14")])
        plan = build_plan(
            "20260902-101500-ab12", "eth0", backend, change,
            ["203.0.113.10"], 120, "eth0 203.0.113.11/32\n", "eth0 203.0.113.13/32\n", "eth0 203.0.113.11 198.51.100.1 1001\n",
            "eth2\n", "up",
            [PlanFile("/etc/netplan/60-monitoring-extra-ips.yaml", "600", "network:\n"), PlanFile("/etc/systemd/network/x.d/m.conf", "644", None)],
        )
        self.assertIn("TX_ID=20260902-101500-ab12\nIFACE=eth0\nBACKEND=networkmanager\nDETAIL=Wired\nTIMEOUT=120\n", plan)
        self.assertIn("ADD=203.0.113.11/32 203.0.113.14/32\nREMOVE=203.0.113.12/32 203.0.113.13/32\nPROTECTED=203.0.113.10\n", plan)
        self.assertIn("MANAGED_B64=" + base64.b64encode(b"eth0 203.0.113.11/32\n").decode(), plan)
        self.assertIn("SUPPRESSED_B64=" + base64.b64encode(b"eth0 203.0.113.13/32\n").decode(), plan)
        self.assertIn("ROUTES_B64=" + base64.b64encode(b"eth0 203.0.113.11 198.51.100.1 1001\n").decode(), plan)
        self.assertIn("LINKS_B64=" + base64.b64encode(b"eth2\n").decode() + "\nLINK=up\n", plan)
        self.assertIn("NM_CONNECTION=Wired\nNM_KEYFILE=/etc/NetworkManager/system-connections/w.nmconnection\n", plan)
        # В соединение NetworkManager уходят только свои адреса, адрес хостера в нём остаётся
        self.assertIn("NM_ADD=203.0.113.11/32\nNM_REMOVE=203.0.113.12/32\n", plan)
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
        # Скрипт уходит байтами: текстовый stdin на Windows подменил бы LF на CRLF
        check = subprocess.run(["bash", "-n"], input=HOST_SCRIPT.encode("utf-8"), capture_output=True)
        self.assertEqual(check.returncode, 0, check.stderr.decode("utf-8", "replace"))

    def test_script_has_every_verb_and_accurate_timer(self):
        for verb in ("detect", "apply", "confirm", "rollback", "rollback-unconfirmed", "boot-guard", "restore-runtime", "sync-runtime", "self-test"):
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
        self.assertEqual(state.default_gateway, {"ipv4": "203.0.113.1"})
        self.assertIsNone(by_cidr["203.0.113.11/32"].gateway)

    def test_state_shows_own_gateway_of_managed_address(self):
        (self.state_dir / "managed.list").write_text("eth0 203.0.113.11/32\n")
        (self.state_dir / "routes.list").write_text("eth0 203.0.113.11 198.51.100.1 1001\n")
        manager, _ = self.manager(self.live_answers())
        with unittest.mock.patch.object(extra_ips, "default_interface", return_value="eth0"):
            state = run(manager.state())
        by_cidr = {f"{a.address}/{a.prefix}": a for a in state.interfaces[0].addresses}
        self.assertEqual(by_cidr["203.0.113.11/32"].gateway, "198.51.100.1")
        self.assertIsNone(by_cidr["203.0.113.10/24"].gateway)
        self.assertEqual(state.managed[0].gateway, "198.51.100.1")

    def test_apply_puts_gateway_routes_into_the_plan(self):
        (self.state_dir / "routes.list").write_text("eth1 10.0.0.9 10.0.0.1 1001\n")
        answers = self.live_answers()
        answers["extra-ips.sh apply"] = FakeResult(stdout="TX_ID=20260902-101500-ab12\nTX_STATUS=pending\n")
        manager, executor = self.manager(answers)
        request = NetworkApplyRequest(interface="eth0", protected=["203.0.113.10"], add=[
            {"address": "198.51.100.5", "prefix": 32, "gateway": "198.51.100.1"},
            {"address": "198.51.100.6", "prefix": 32, "gateway": "203.0.113.1"},
        ])
        with unittest.mock.patch.object(ExtraIpManager, "_mac", return_value=""):
            response = run(manager.apply(request))
        self.assertTrue(response.success)
        apply_command = next(c for c in executor.commands if "extra-ips.sh apply" in c)
        plan = base64.b64decode(re.search(r"printf '%s' '([A-Za-z0-9+/=]+)'", apply_command).group(1)).decode()
        routes_line = next(line for line in plan.splitlines() if line.startswith("ROUTES_B64="))
        # Шлюз основного адреса — это «как у основного», своя таблица не нужна
        self.assertEqual(base64.b64decode(routes_line.split("=", 1)[1]).decode(),
                         "eth1 10.0.0.9 10.0.0.1 1001\neth0 198.51.100.5 198.51.100.1 1002\n")

    def test_sync_runs_only_when_there_are_routes_or_removed_hoster_addresses(self):
        answer = {"extra-ips.sh sync-runtime": FakeResult(stdout="ROUTES_CHANGED=0\nADDRS_DROPPED=1\n")}
        manager, executor = self.manager(answer)
        run(manager.sync_runtime())
        self.assertEqual(executor.commands, [])
        for name, line in (("routes.list", "eth0 198.51.100.5 198.51.100.1 1001\n"), ("suppressed.list", "eth0 198.51.100.9/32\n")):
            for other in ("routes.list", "suppressed.list"):
                (self.state_dir / other).unlink(missing_ok=True)
            (self.state_dir / name).write_text(line)
            manager, executor = self.manager(answer)
            run(manager.sync_runtime())
            self.assertTrue(any("extra-ips.sh sync-runtime" in c for c in executor.commands), name)

    def plan_of(self, executor: FakeExecutor) -> dict[str, str]:
        apply_command = next(c for c in executor.commands if "extra-ips.sh apply" in c)
        plan = base64.b64decode(re.search(r"printf '%s' '([A-Za-z0-9+/=]+)'", apply_command).group(1)).decode()
        return dict(line.split("=", 1) for line in plan.splitlines() if "=" in line and not line.startswith("FILE="))

    def test_apply_removes_hoster_address_without_touching_own_config(self):
        (self.state_dir / "managed.list").write_text("eth0 203.0.113.11/32\n")
        answers = self.live_answers()
        answers["extra-ips.sh apply"] = FakeResult(stdout="TX_ID=20260902-101500-ab12\nTX_STATUS=pending\n")
        manager, executor = self.manager(answers)
        # Основной IPv6 — 2001:db8::10/64 (первый не наш), 2001:db8::11/128 — адрес хостера
        request = NetworkApplyRequest(interface="eth0", protected=["203.0.113.10"],
                                      remove=[{"address": "2001:db8::11", "prefix": 128}])
        with unittest.mock.patch.object(ExtraIpManager, "_mac", return_value=""):
            self.assertTrue(run(manager.apply(request)).success)
        plan = self.plan_of(executor)
        self.assertEqual((plan["ADD"], plan["REMOVE"]), ("", "2001:db8::11/128"))
        self.assertEqual(base64.b64decode(plan["MANAGED_B64"]).decode(), "eth0 203.0.113.11/32\n")
        self.assertEqual(base64.b64decode(plan["SUPPRESSED_B64"]).decode(), "eth0 2001:db8::11/128\n")

    def test_apply_restores_removed_hoster_address(self):
        (self.state_dir / "suppressed.list").write_text("eth0 198.51.100.9/32\neth1 10.0.0.9/32\n")
        answers = self.live_answers()
        answers["extra-ips.sh apply"] = FakeResult(stdout="TX_ID=20260902-101500-ab12\nTX_STATUS=pending\n")
        manager, executor = self.manager(answers)
        request = NetworkApplyRequest(interface="eth0", restore=[{"address": "198.51.100.9", "prefix": 32}])
        with unittest.mock.patch.object(ExtraIpManager, "_mac", return_value=""):
            self.assertTrue(run(manager.apply(request)).success)
        plan = self.plan_of(executor)
        self.assertEqual((plan["ADD"], plan["REMOVE"]), ("198.51.100.9/32", ""))
        self.assertEqual(base64.b64decode(plan["SUPPRESSED_B64"]).decode(), "eth1 10.0.0.9/32\n")
        self.assertEqual(base64.b64decode(plan["MANAGED_B64"]).decode(), "")

    def with_down_card(self) -> dict[str, FakeResult]:
        answers = self.live_answers()
        answers["for d in /sys/class/net/*"] = FakeResult(stdout="eth0 up physical no\neth2 down physical no\n")
        answers["extra-ips.sh apply"] = FakeResult(stdout="TX_ID=20260902-101500-ab12\nTX_STATUS=pending\n")
        return answers

    def test_apply_brings_a_down_card_up_with_its_first_address(self):
        manager, executor = self.manager(self.with_down_card())
        request = NetworkApplyRequest(interface="eth2", protected=["203.0.113.10"], add=[
            {"address": "198.51.100.7", "prefix": 32, "gateway": "198.51.100.1"},
        ])
        with unittest.mock.patch.object(ExtraIpManager, "_mac", return_value=""):
            self.assertTrue(run(manager.apply(request)).success)
        plan = self.plan_of(executor)
        # Карту не описывает ни один конфиг: адреса держит fallback, линк — links.list
        self.assertEqual((plan["BACKEND"], plan["LINK"]), ("fallback", "up"))
        self.assertEqual(base64.b64decode(plan["LINKS_B64"]).decode(), "eth2\n")
        self.assertEqual(base64.b64decode(plan["ROUTES_B64"]).decode(), "eth2 198.51.100.7 198.51.100.1 1001\n")

    def test_apply_brings_the_card_down_with_its_last_address(self):
        (self.state_dir / "managed.list").write_text("eth2 198.51.100.7/32\n")
        (self.state_dir / "links.list").write_text("eth2\n")
        answers = self.with_down_card()
        answers["for d in /sys/class/net/*"] = FakeResult(stdout="eth0 up physical no\neth2 up physical no\n")
        manager, executor = self.manager(answers)
        request = NetworkApplyRequest(interface="eth2", protected=["203.0.113.10"],
                                      remove=[{"address": "198.51.100.7", "prefix": 32}])
        with unittest.mock.patch.object(ExtraIpManager, "_mac", return_value=""):
            self.assertTrue(run(manager.apply(request)).success)
        plan = self.plan_of(executor)
        self.assertEqual((plan["LINK"], plan["REMOVE"]), ("down", "198.51.100.7/32"))
        self.assertEqual(base64.b64decode(plan["LINKS_B64"]).decode(), "")

    def test_state_marks_cards_the_panel_brought_up(self):
        (self.state_dir / "links.list").write_text("eth2\n")
        manager, _ = self.manager(self.with_down_card())
        with unittest.mock.patch.object(extra_ips, "default_interface", return_value="eth0"):
            state = run(manager.state())
        self.assertEqual({i.name: i.brought_up for i in state.interfaces}, {"eth0": False, "eth2": True})

    def test_state_lists_removed_hoster_addresses(self):
        (self.state_dir / "suppressed.list").write_text("eth0 198.51.100.9/32\n")
        manager, _ = self.manager(self.live_answers())
        with unittest.mock.patch.object(extra_ips, "default_interface", return_value="eth0"):
            state = run(manager.state())
        self.assertEqual([(s.interface, s.address, s.prefix) for s in state.suppressed], [("eth0", "198.51.100.9", 32)])

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

    def test_apply_detects_backend_afresh_after_state_saw_no_script(self):
        # Скрипт уезжает на хост лениво, в apply(); опрос state() до этого
        # получает от bash «No such file» — этот провал не должен доживать в кэше до apply
        answers = self.live_answers()
        answers["extra-ips.sh detect"] = FakeResult(
            success=False, exit_code=127, stderr="bash: /opt/monitoring/scripts/extra-ips.sh: No such file or directory",
        )
        answers["extra-ips.sh apply"] = FakeResult(stdout="TX_ID=20260902-101500-ab12\nTX_STATUS=pending\n")
        manager, _ = self.manager(answers)
        with unittest.mock.patch.object(extra_ips, "default_interface", return_value="eth0"):
            state = run(manager.state())
        self.assertIsNone(state.backend)
        answers["extra-ips.sh detect"] = self.live_answers()["extra-ips.sh detect"]
        request = NetworkApplyRequest(interface="eth0", add=[{"address": "203.0.113.12", "prefix": 32}], protected=["203.0.113.10"])
        with unittest.mock.patch.object(ExtraIpManager, "_mac", return_value=""):
            response = run(manager.apply(request))
        self.assertTrue(response.success)

    def test_detect_failure_reason_reaches_the_error(self):
        answers = self.live_answers()
        answers["extra-ips.sh detect"] = FakeResult(success=False, exit_code=127, stderr="bash: extra-ips.sh: No such file or directory")
        manager, _ = self.manager(answers)
        request = NetworkApplyRequest(interface="eth0", add=[{"address": "203.0.113.12", "prefix": 32}])
        with self.assertRaises(ExtraIpUnsupportedError) as ctx:
            run(manager.apply(request))
        self.assertIn("No such file or directory", str(ctx.exception))

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
        run(manager._rollback_stale_transaction())
        self.assertTrue(any("rollback-unconfirmed 20260902-101500-ab12" in c for c in executor.commands))
        (self.state_dir / "transaction.env").write_text("TX_ID=20260902-101500-ab12\nTX_STATUS=confirmed\n")
        manager, executor = self.manager({})
        run(manager._rollback_stale_transaction())
        self.assertEqual(executor.commands, [])


if __name__ == "__main__":
    unittest.main()
