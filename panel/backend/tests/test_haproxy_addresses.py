"""Tests for per-server HAProxy addresses (listen / outgoing IPs).

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Главное свойство — сервер без адресов получает профиль байт-в-байт: иначе после
обновления панели хэш у всех привязанных серверов разъехался бы и весь парк
получил бы лишний reload.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.haproxy_addresses import (  # noqa: E402
    MAX_ADDRESSES,
    InvalidAddressError,
    ServerAddresses,
    dump_ips,
    load_ips,
    missing_on_node,
    normalize_ips,
    render_for_server,
)
from app.services.haproxy_config import (  # noqa: E402
    BackendServer,
    BalancerOptions,
    HAProxyRule,
    ProfileOptions,
    get_config_generator,
)


def profile_config(*rules: HAProxyRule) -> str:
    return get_config_generator().generate_full_config(list(rules), ProfileOptions())


SINGLE_RULE = HAProxyRule(
    name="vless", rule_type="tcp", listen_port=443,
    target_ip="10.0.0.5", target_port=8443, send_proxy=True,
)

BALANCER_RULE = HAProxyRule(
    name="lb", rule_type="tcp", listen_port=8443, target_ip="", target_port=0,
    is_balancer=True,
    servers=[
        BackendServer(name="a", address="10.0.0.5", port=443, maxconn=60000),
        BackendServer(name="b", address="10.0.0.6", port=443, maxconn=60000),
    ],
    balancer_options=BalancerOptions(sticky_type="cookie", cookie_name="SRV"),
)


def lines_with(config: str, needle: str) -> list[str]:
    return [line.strip() for line in config.splitlines() if needle in line]


class EmptyAddressesTests(unittest.TestCase):
    def test_config_is_returned_unchanged(self):
        config = profile_config(SINGLE_RULE, BALANCER_RULE)
        self.assertEqual(render_for_server(config, ServerAddresses()), config)


class ListenAddressesTests(unittest.TestCase):
    def test_single_listen_ip_replaces_wildcard(self):
        config = profile_config(SINGLE_RULE)
        rendered = render_for_server(config, ServerAddresses(listen=("1.1.1.1",)))
        self.assertEqual(lines_with(rendered, "bind "), ["bind 1.1.1.1:443"])

    def test_several_listen_ips_share_one_bind_line(self):
        config = profile_config(SINGLE_RULE)
        rendered = render_for_server(config, ServerAddresses(listen=("1.1.1.1", "1.1.1.2")))
        self.assertEqual(lines_with(rendered, "bind "), ["bind 1.1.1.1:443,1.1.1.2:443"])

    def test_bind_parameters_are_kept(self):
        rule = HAProxyRule(
            name="web", rule_type="https", listen_port=443, target_ip="10.0.0.5",
            target_port=80, cert_domain="example.com", accept_proxy=True,
        )
        rendered = render_for_server(profile_config(rule), ServerAddresses(listen=("1.1.1.1",)))
        self.assertEqual(
            lines_with(rendered, "bind "),
            ["bind 1.1.1.1:443 ssl crt /etc/letsencrypt/live/example.com/combined.pem accept-proxy"],
        )

    def test_explicit_bind_address_is_left_alone(self):
        config = "frontend f\n    bind 9.9.9.9:443\n    bind /run/x.sock\n"
        rendered = render_for_server(config, ServerAddresses(listen=("1.1.1.1",)))
        self.assertEqual(rendered, config)

    def test_listen_only_does_not_touch_servers(self):
        config = profile_config(SINGLE_RULE)
        rendered = render_for_server(config, ServerAddresses(listen=("1.1.1.1",)))
        self.assertNotIn("source", rendered)


class SourceAddressesTests(unittest.TestCase):
    def test_single_source_ip_is_appended_without_copies(self):
        config = profile_config(SINGLE_RULE)
        rendered = render_for_server(config, ServerAddresses(source=("2.2.2.1",)))
        servers = lines_with(rendered, "server srv")
        self.assertEqual(len(servers), 1)
        self.assertTrue(servers[0].startswith("server srv1 10.0.0.5:8443 "))
        self.assertTrue(servers[0].endswith(" source 2.2.2.1"))

    def test_each_source_ip_gets_its_own_copy(self):
        config = profile_config(SINGLE_RULE)
        rendered = render_for_server(config, ServerAddresses(source=("2.2.2.1", "2.2.2.2", "2.2.2.3")))
        servers = lines_with(rendered, "server srv")
        self.assertEqual([s.split()[1] for s in servers], ["srv1", "srv1_o2", "srv1_o3"])
        self.assertEqual([s.split()[-1] for s in servers], ["2.2.2.1", "2.2.2.2", "2.2.2.3"])
        self.assertTrue(all("10.0.0.5:8443" in s and "send-proxy-v2" in s for s in servers))

    def test_copies_get_unique_cookie_values(self):
        config = profile_config(BALANCER_RULE)
        rendered = render_for_server(config, ServerAddresses(source=("2.2.2.1", "2.2.2.2")))
        cookies = [line.split("cookie ")[1].split()[0] for line in lines_with(rendered, "    server ")]
        self.assertEqual(sorted(cookies), ["a", "a_o2", "b", "b_o2"])

    def test_manual_server_source_is_left_alone(self):
        config = "backend b\n    server s1 10.0.0.5:443 check source 3.3.3.3\n"
        rendered = render_for_server(config, ServerAddresses(source=("2.2.2.1", "2.2.2.2")))
        self.assertEqual(rendered, config)

    def test_backend_level_source_is_left_alone(self):
        config = "backend b\n    source 3.3.3.3\n    server s1 10.0.0.5:443 check\n"
        rendered = render_for_server(config, ServerAddresses(source=("2.2.2.1",)))
        self.assertEqual(rendered, config)

    def test_default_server_and_frontends_are_not_copied(self):
        config = "frontend f\n    bind *:443\nbackend b\n    default-server check\n    server s1 10.0.0.5:443\n"
        rendered = render_for_server(config, ServerAddresses(source=("2.2.2.1", "2.2.2.2")))
        self.assertEqual(lines_with(rendered, "default-server"), ["default-server check"])
        self.assertEqual(lines_with(rendered, "bind"), ["bind *:443"])


class RulesAreaTests(unittest.TestCase):
    def test_sections_outside_rule_markers_are_untouched(self):
        config = profile_config(SINGLE_RULE).replace(
            "resolvers mydns", "listen stats\n    bind *:8404\n\nresolvers mydns", 1,
        )
        rendered = render_for_server(config, ServerAddresses(listen=("1.1.1.1",)))
        self.assertIn("bind *:8404", rendered)
        self.assertIn("bind 1.1.1.1:443", rendered)

    def test_config_without_markers_is_rendered_whole(self):
        config = "frontend f\n    bind *:443\n    default_backend b\nbackend b\n    server s1 10.0.0.5:443\n"
        rendered = render_for_server(config, ServerAddresses(listen=("1.1.1.1",), source=("2.2.2.1",)))
        self.assertIn("bind 1.1.1.1:443", rendered)
        self.assertIn("server s1 10.0.0.5:443 source 2.2.2.1", rendered)

    def test_crlf_line_endings_survive(self):
        config = "frontend f\r\n    bind *:443\r\nbackend b\r\n    server s1 10.0.0.5:443\r\n"
        rendered = render_for_server(config, ServerAddresses(listen=("1.1.1.1",), source=("2.2.2.1", "2.2.2.2")))
        self.assertEqual(
            rendered,
            "frontend f\r\n    bind 1.1.1.1:443\r\nbackend b\r\n"
            "    server s1 10.0.0.5:443 source 2.2.2.1\r\n    server s1_o2 10.0.0.5:443 source 2.2.2.2\r\n",
        )


class NormalizeTests(unittest.TestCase):
    def test_trims_and_deduplicates_keeping_order(self):
        self.assertEqual(normalize_ips([" 1.1.1.2", "1.1.1.1", "1.1.1.2 "]), ("1.1.1.2", "1.1.1.1"))

    def test_rejects_non_ipv4(self):
        for bad in ("2001:db8::1", "example.com", "1.1.1.1/32", ""):
            with self.subTest(value=bad):
                with self.assertRaises(InvalidAddressError):
                    normalize_ips([bad])

    def test_rejects_too_many(self):
        with self.assertRaises(InvalidAddressError):
            normalize_ips([f"10.0.0.{i}" for i in range(1, MAX_ADDRESSES + 2)])


class StorageTests(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(load_ips(dump_ips(("1.1.1.1", "1.1.1.2"))), ("1.1.1.1", "1.1.1.2"))

    def test_empty_is_stored_as_null(self):
        self.assertIsNone(dump_ips(()))
        self.assertEqual(load_ips(None), ())

    def test_broken_value_reads_as_empty(self):
        for raw in ("not json", '{"a": 1}', "[1, 2]"):
            with self.subTest(raw=raw):
                self.assertEqual(load_ips(raw), ())


class MissingOnNodeTests(unittest.TestCase):
    STATE = {
        "interfaces": [
            {"name": "eth0", "addresses": [
                {"address": "1.1.1.1", "family": "ipv4"},
                {"address": "2001:db8::1", "family": "ipv6"},
            ]},
            {"name": "eth1", "addresses": [{"address": "2.2.2.1", "family": "ipv4"}]},
        ],
    }

    def test_present_addresses_pass(self):
        addresses = ServerAddresses(listen=("1.1.1.1",), source=("2.2.2.1",))
        self.assertEqual(missing_on_node(addresses, self.STATE), [])

    def test_absent_addresses_are_reported_once(self):
        addresses = ServerAddresses(listen=("1.1.1.1", "9.9.9.9"), source=("9.9.9.9", "8.8.8.8"))
        self.assertEqual(missing_on_node(addresses, self.STATE), ["9.9.9.9", "8.8.8.8"])


if __name__ == "__main__":
    unittest.main()
