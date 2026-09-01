"""Firewall-профили: предупреждение о потере SSH по фактическим портам серверов.

Голый unittest, без PostgreSQL и без сети.
Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.models import FirewallProfile  # noqa: E402
    from app.routers.firewall_profiles import (  # noqa: E402
        SSH_DEFAULT_PORT,
        _effective_ssh_port,
        _profile_to_dict,
    )
except ImportError as e:  # рантайм панели не установлен
    raise unittest.SkipTest(f"firewall profiles require the panel runtime: {e}")


def make_profile(rules: list[dict], default_incoming: str = "deny") -> FirewallProfile:
    return FirewallProfile(
        id=1,
        name="test",
        description=None,
        rules_json=json.dumps(rules),
        default_incoming=default_incoming,
        default_outgoing="allow",
        position=0,
    )


def allow_rule(port: int, protocol: str = "tcp") -> dict:
    return {"port": port, "protocol": protocol, "action": "allow",
            "from_ip": None, "direction": "in", "comment": ""}


class EffectiveSshPortTest(unittest.TestCase):
    def test_chain(self):
        self.assertEqual(_effective_ssh_port(2222, 2200), 2222)  # кэш с ноды приоритетнее
        self.assertEqual(_effective_ssh_port(None, 2200), 2200)
        self.assertEqual(_effective_ssh_port(None, None), SSH_DEFAULT_PORT)


class SshWarningTest(unittest.TestCase):
    def test_custom_port_covered(self):
        data = _profile_to_dict(make_profile([allow_rule(2222)]), ssh_ports=[2222])
        self.assertTrue(data["ssh_port_allowed"])
        self.assertEqual(data["ssh_ports"], [2222])
        self.assertEqual(data["ssh_ports_blocked"], [])

    def test_mixed_ports_partially_covered(self):
        data = _profile_to_dict(make_profile([allow_rule(2222)]), ssh_ports=[22, 2222])
        self.assertFalse(data["ssh_port_allowed"])
        self.assertEqual(data["ssh_ports"], [22, 2222])
        self.assertEqual(data["ssh_ports_blocked"], [22])

    def test_no_linked_servers_falls_back_to_default(self):
        data = _profile_to_dict(make_profile([allow_rule(2222)]), ssh_ports=None)
        self.assertFalse(data["ssh_port_allowed"])
        self.assertEqual(data["ssh_ports"], [SSH_DEFAULT_PORT])
        self.assertEqual(data["ssh_ports_blocked"], [SSH_DEFAULT_PORT])

    def test_duplicate_ports_deduplicated(self):
        data = _profile_to_dict(make_profile([]), ssh_ports=[2222, 2222, 22])
        self.assertEqual(data["ssh_ports"], [22, 2222])

    def test_default_incoming_allow(self):
        data = _profile_to_dict(make_profile([], default_incoming="allow"), ssh_ports=[22, 2222])
        self.assertTrue(data["ssh_port_allowed"])
        self.assertEqual(data["ssh_ports_blocked"], [])

    def test_udp_rule_does_not_cover(self):
        data = _profile_to_dict(make_profile([allow_rule(2222, protocol="udp")]), ssh_ports=[2222])
        self.assertFalse(data["ssh_port_allowed"])

    def test_any_protocol_covers(self):
        data = _profile_to_dict(make_profile([allow_rule(2222, protocol="any")]), ssh_ports=[2222])
        self.assertTrue(data["ssh_port_allowed"])


if __name__ == "__main__":
    unittest.main()
