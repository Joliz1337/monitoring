"""Tests for validating balancer backend servers in a HAProxy rule.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Имя сервера уходит в конфиг как есть: дубль внутри одного бэкенда или имя
с пробелом HAProxy не примет, и раскатка профиля упадёт на всех нодах.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.haproxy_config import (  # noqa: E402
    BackendServer,
    BalancerOptions,
    HAProxyRule,
    get_config_generator,
)


def balancer_rule(*names: str) -> HAProxyRule:
    servers = [BackendServer(name, f"10.0.0.{i}", 8443) for i, name in enumerate(names, start=1)]
    return HAProxyRule(name="lb", rule_type="tcp", listen_port=443, target_ip="10.0.0.1", target_port=8443,
                       is_balancer=True, servers=servers, balancer_options=BalancerOptions())


def validate(rule: HAProxyRule) -> tuple[bool, str]:
    return get_config_generator().validate_rule(rule)


class ServerNameTests(unittest.TestCase):
    def test_unique_names_pass(self):
        ok, _ = validate(balancer_rule("srv3", "srv4", "srv5", "srv6"))
        self.assertTrue(ok)

    def test_duplicate_name_rejected(self):
        ok, message = validate(balancer_rule("srv3", "srv4", "srv5", "srv4"))
        self.assertFalse(ok)
        self.assertIn("srv4", message)

    def test_names_with_spaces_or_empty_rejected(self):
        for bad in ("", "srv 4", "srv#4"):
            with self.subTest(name=bad):
                ok, _ = validate(balancer_rule("srv1", bad))
                self.assertFalse(ok)

    def test_haproxy_safe_punctuation_allowed(self):
        ok, _ = validate(balancer_rule("nl-1", "de_2", "fi.hetzner", "srv:4"))
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
