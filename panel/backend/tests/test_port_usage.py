"""Порты HAProxy и DNAT по серверам: что считается занятым.

Голый unittest, без PostgreSQL и без сети.
Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services.haproxy_config import HAProxyRule, ProfileOptions, get_config_generator  # noqa: E402
    from app.services.port_usage import dnat_tcp_ranges, profile_haproxy_ports, running_haproxy_ports  # noqa: E402
except ImportError as e:  # рантайм панели не установлен
    raise unittest.SkipTest(f"port usage requires the panel runtime: {e}")


def dnat_rule(port: int, end: int | None = None, protocol: str = "tcp", enabled: bool = True) -> dict:
    return {"name": f"r{port}", "protocol": protocol, "listen_port": port, "listen_port_end": end, "enabled": enabled}


def haproxy_cache(running: bool, ports: list[int]) -> str:
    return json.dumps({
        "status": {"running": running, "config_valid": True, "config_message": ""},
        "rules": {"count": len(ports), "rules": [{"name": f"r{p}", "listen_port": p} for p in ports]},
    })


class DnatRangesTests(unittest.TestCase):
    def test_single_port_and_range(self):
        self.assertEqual(dnat_tcp_ranges([dnat_rule(8443), dnat_rule(20000, 30000)]), [(8443, 8443), (20000, 30000)])

    def test_both_counts_as_tcp(self):
        self.assertEqual(dnat_tcp_ranges([dnat_rule(443, protocol="both")]), [(443, 443)])

    def test_udp_and_disabled_rules_do_not_take_tcp_port(self):
        self.assertEqual(dnat_tcp_ranges([dnat_rule(443, protocol="udp"), dnat_rule(8443, enabled=False)]), [])


class RunningHaproxyPortsTests(unittest.TestCase):
    def test_running_haproxy_ports(self):
        self.assertEqual(running_haproxy_ports(haproxy_cache(True, [443, 8443])), [443, 8443])

    def test_stopped_haproxy_takes_nothing(self):
        self.assertEqual(running_haproxy_ports(haproxy_cache(False, [443])), [])

    def test_unparsed_port_is_skipped(self):
        self.assertEqual(running_haproxy_ports(haproxy_cache(True, [0, 443])), [443])

    def test_broken_or_empty_cache(self):
        self.assertEqual(running_haproxy_ports(None), [])
        self.assertEqual(running_haproxy_ports("not json"), [])
        self.assertEqual(running_haproxy_ports(json.dumps({"cached_at": "x"})), [])


class ProfileHaproxyPortsTests(unittest.TestCase):
    def test_ports_from_generated_profile(self):
        rules = [
            HAProxyRule(name="a", rule_type="tcp", listen_port=443, target_ip="10.0.0.5", target_port=443),
            HAProxyRule(name="b", rule_type="tcp", listen_port=8443, target_ip="10.0.0.6", target_port=443),
        ]
        config = get_config_generator().generate_full_config(rules, ProfileOptions())
        self.assertEqual(sorted(profile_haproxy_ports(config)), [443, 8443])

    def test_empty_profile(self):
        self.assertEqual(profile_haproxy_ports(None), [])


if __name__ == "__main__":
    unittest.main()
