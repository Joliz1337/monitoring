"""Guard файрвола: профиль без allow на порт API ноды отрезал бы панель.

Порт больше не константа 9100 — нода может слушать кастомный порт
(NODE_API_PORT в .env), и guard обязан требовать allow именно на него.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firewall_manager import FirewallManager  # noqa: E402


def allow_in(port: int) -> dict:
    return {"port": port, "protocol": "tcp", "action": "allow", "direction": "in", "from_ip": None}


class NodePortAllowTests(unittest.TestCase):
    def test_default_port_rule_passes(self):
        self.assertTrue(FirewallManager._has_node_port_allow([allow_in(9100)], "deny", 9100))

    def test_missing_rule_fails(self):
        self.assertFalse(FirewallManager._has_node_port_allow([allow_in(22)], "deny", 9100))

    def test_custom_port_requires_allow_on_that_port(self):
        # Правило на дефолтный 9100 не спасает ноду, которая слушает 12345
        self.assertFalse(FirewallManager._has_node_port_allow([allow_in(9100)], "deny", 12345))
        self.assertTrue(FirewallManager._has_node_port_allow([allow_in(12345)], "deny", 12345))

    def test_default_allow_passes_without_rules(self):
        self.assertTrue(FirewallManager._has_node_port_allow([], "allow", 12345))


if __name__ == "__main__":
    unittest.main()
