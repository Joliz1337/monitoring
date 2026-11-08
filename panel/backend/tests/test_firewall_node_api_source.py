"""Firewall-профили: порт API ноды по умолчанию открыт только панели, а профиль с
ограничением по источнику не раскатывается на ноду за прокси, которую он отрезал бы.

Голый unittest, без PostgreSQL и без сети.
Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.routers import firewall_profiles  # noqa: E402
    from app.services import firewall_proxy_guard  # noqa: E402
    from app.services.firewall_proxy_guard import proxy_lockout_message  # noqa: E402
except ImportError as e:  # рантайм панели не установлен
    raise unittest.SkipTest(f"firewall profiles require the panel runtime: {e}")

PANEL_IP = "203.0.113.10"
PROXY_IP = "198.51.100.7"


def run(coro):
    return asyncio.run(coro)


def api_rule(from_ip=None, **overrides) -> dict:
    return {"port": 9100, "protocol": "tcp", "action": "allow",
            "from_ip": from_ip, "direction": "in", "comment": "", **overrides}


def server(proxy_url=None, url="https://198.51.100.50:9100") -> SimpleNamespace:
    return SimpleNamespace(proxy_url=proxy_url, url=url)


class DefaultRulesTest(unittest.TestCase):
    def test_api_rule_limited_to_panel_ip(self):
        with mock.patch.object(firewall_profiles, "resolve_panel_ip", mock.AsyncMock(return_value=PANEL_IP)):
            rules = run(firewall_profiles.build_default_rules())
        by_port = {r["port"]: r for r in rules}
        self.assertEqual(by_port[9100]["from_ip"], PANEL_IP)
        self.assertIsNone(by_port[22]["from_ip"])

    def test_unresolved_panel_ip_keeps_open_rule(self):
        with mock.patch.object(firewall_profiles, "resolve_panel_ip", mock.AsyncMock(return_value=None)):
            rules = run(firewall_profiles.build_default_rules())
        self.assertIsNone({r["port"]: r for r in rules}[9100]["from_ip"])

    def test_template_constant_untouched(self):
        with mock.patch.object(firewall_profiles, "resolve_panel_ip", mock.AsyncMock(return_value=PANEL_IP)):
            run(firewall_profiles.build_default_rules())
        self.assertTrue(all(r["from_ip"] is None for r in firewall_profiles.DEFAULT_RULES))


class ProxyLockoutGuardTest(unittest.TestCase):
    def check(self, srv, rules, default_in="deny"):
        return run(proxy_lockout_message(srv, rules, default_in))

    def test_refuses_proxied_server_when_api_limited_to_panel(self):
        message = self.check(server(f"{PROXY_IP}:1080@user:secret"), [api_rule(PANEL_IP)])
        self.assertIn(PROXY_IP, message)
        self.assertNotIn("secret", message)

    def test_allows_when_source_is_proxy_ip(self):
        self.assertIsNone(self.check(server(f"{PROXY_IP}:1080"), [api_rule(PANEL_IP), api_rule(PROXY_IP)]))

    def test_allows_when_cidr_contains_proxy(self):
        self.assertIsNone(self.check(server(f"{PROXY_IP}:1080"), [api_rule("198.51.100.0/24")]))

    def test_refuses_when_cidr_misses_proxy(self):
        self.assertIsNotNone(self.check(server(f"{PROXY_IP}:1080"), [api_rule("192.0.2.0/24")]))

    def test_allows_when_default_incoming_allow(self):
        self.assertIsNone(self.check(server(f"{PROXY_IP}:1080"), [api_rule(PANEL_IP)], default_in="allow"))

    def test_allows_unrestricted_rule(self):
        self.assertIsNone(self.check(server(f"{PROXY_IP}:1080"), [api_rule(PANEL_IP), api_rule(None)]))

    def test_allows_server_without_proxy(self):
        self.assertIsNone(self.check(server(None), [api_rule(PANEL_IP)]))

    def test_ignores_rules_for_other_ports_and_denies(self):
        rules = [api_rule(PANEL_IP), api_rule(PROXY_IP, action="deny"), api_rule(PROXY_IP, port=22)]
        self.assertIsNotNone(self.check(server(f"{PROXY_IP}:1080"), rules))

    def test_custom_api_port_from_server_url(self):
        srv = server(f"{PROXY_IP}:1080", url="https://198.51.100.50:9443")
        self.assertIsNotNone(self.check(srv, [api_rule(PANEL_IP, port=9443)]))
        self.assertIsNone(self.check(srv, [api_rule(PANEL_IP)]))

    def test_hostname_proxy_is_resolved(self):
        resolve = mock.AsyncMock(return_value=PROXY_IP)
        with mock.patch.object(firewall_proxy_guard, "host_to_ip", resolve):
            self.assertIsNone(self.check(server("proxy.example:1080"), [api_rule(PROXY_IP)]))

    def test_unresolved_hostname_counts_as_not_covered(self):
        with mock.patch.object(firewall_proxy_guard, "host_to_ip", mock.AsyncMock(return_value=None)):
            self.assertIsNotNone(self.check(server("proxy.example:1080"), [api_rule(PANEL_IP)]))


if __name__ == "__main__":
    unittest.main()
