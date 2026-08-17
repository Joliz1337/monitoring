"""DNAT-профили: хэш набора правил (зеркало ноды) и проверка набора перед сохранением.

Голый unittest, без PostgreSQL и без сети.
Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.routers.dnat_profiles import DnatRuleData, validate_rule_set  # noqa: E402
    from app.services.dnat_profile_sync import compute_rules_hash  # noqa: E402
except ImportError as e:  # рантайм панели не установлен
    raise unittest.SkipTest(f"dnat profiles require the panel runtime: {e}")


# Тот же вектор и хэш лежат в node/tests/test_dnat.py: формула на двух
# сторонах обязана совпадать, иначе панель увидит вечный drift
GOLDEN_RULES = [
    {"name": "hop", "protocol": "udp", "listen_port": 20000, "listen_port_end": 30000, "target_ip": "10.0.0.3",
     "target_port": 0, "masquerade": True, "enabled": True, "comment": "hysteria"},
    {"name": "vless", "protocol": "tcp", "listen_port": 443, "listen_port_end": 443, "target_ip": "10.0.0.2",
     "target_port": 8443, "masquerade": False, "enabled": False, "comment": ""},
]
GOLDEN_HASH = "f5ccd7baf1f7bdc84a6b8897bb2c23ddd790b1f3ff9076597a6a67dc9e803ce3"


def rule(**overrides) -> dict:
    base = {"name": "vless", "protocol": "tcp", "listen_port": 443, "target_ip": "10.0.0.2", "target_port": 8443}
    base.update(overrides)
    return DnatRuleData(**base).model_dump()


class HashTest(unittest.TestCase):
    def test_golden_vector_matches_node(self):
        self.assertEqual(compute_rules_hash(json.dumps(GOLDEN_RULES)), GOLDEN_HASH)

    def test_comment_ignored_and_broken_json_is_empty_set(self):
        self.assertEqual(
            compute_rules_hash(json.dumps([rule(comment="a")])),
            compute_rules_hash(json.dumps([rule(comment="b")])),
        )
        self.assertEqual(compute_rules_hash("not json"), compute_rules_hash("[]"))


class ValidateRuleSetTest(unittest.TestCase):
    def test_ok(self):
        self.assertIsNone(validate_rule_set([rule(), rule(name="u", protocol="udp")]))

    def test_duplicate_name(self):
        self.assertIn("уже есть", validate_rule_set([rule(), rule(listen_port=444)]))

    def test_overlap_same_protocol(self):
        self.assertIn("пересекаются", validate_rule_set([
            rule(name="a", listen_port=1000, listen_port_end=2000), rule(name="b", listen_port=1500),
        ]))

    def test_disabled_rule_ignored(self):
        self.assertIsNone(validate_rule_set([rule(name="a"), rule(name="b", enabled=False)]))

    def test_node_api_port_guarded(self):
        self.assertIn("9100", validate_rule_set([rule(listen_port=9000, listen_port_end=9200)]))
        self.assertIsNone(validate_rule_set([rule(protocol="udp", listen_port=9100)]))


class RuleSchemaTest(unittest.TestCase):
    def test_target_ipv4_only(self):
        for bad in ("example.com", "::1", "0.0.0.0", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                DnatRuleData(name="x", listen_port=1, target_ip=bad)

    def test_range_collapse_and_order(self):
        self.assertIsNone(DnatRuleData(name="x", listen_port=5, listen_port_end=5, target_ip="1.1.1.1").listen_port_end)
        with self.assertRaises(ValueError):
            DnatRuleData(name="x", listen_port=5, listen_port_end=4, target_ip="1.1.1.1")


if __name__ == "__main__":
    unittest.main()
