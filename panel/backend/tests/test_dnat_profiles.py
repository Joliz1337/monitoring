"""DNAT-профили: хэш набора правил (зеркало ноды) и проверка набора перед сохранением.

Голый unittest, без PostgreSQL и без сети.
Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.routers.dnat_profiles import DnatRuleData, validate_rule_set  # noqa: E402
    from app.services.dnat_profile_sync import (  # noqa: E402
        assigned_targets,
        compute_rules_hash,
        render_rules_for_server,
        split_targets,
    )
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
GOLDEN_HASH = "281199e72b412545ccd7fc9967b02812e5123c266df98e1f97e25d53de7f0327"


def rule(**overrides) -> dict:
    base = {"name": "vless", "protocol": "tcp", "listen_port": 443, "target_ip": "10.0.0.2", "target_port": 8443}
    base.update(overrides)
    return DnatRuleData(**base).model_dump()


class HashTest(unittest.TestCase):
    def test_golden_vector_matches_node(self):
        self.assertEqual(compute_rules_hash(GOLDEN_RULES), GOLDEN_HASH)

    def test_comment_ignored(self):
        self.assertEqual(compute_rules_hash([rule(comment="a")]), compute_rules_hash([rule(comment="b")]))


class BalancerTest(unittest.TestCase):
    """Несколько IP назначения раздаются серверам по кругу в порядке привязки."""

    def test_split_and_validation(self):
        self.assertEqual(split_targets(" 10.0.0.2 ,10.0.0.3, "), ["10.0.0.2", "10.0.0.3"])
        self.assertEqual(rule(target_ip="10.0.0.2, 10.0.0.3, 10.0.0.2")["target_ip"], "10.0.0.2,10.0.0.3")
        with self.assertRaises(ValueError):
            rule(target_ip="10.0.0.2, example.com")

    def test_round_robin_by_server_index(self):
        rules = [rule(name="a", target_ip="10.0.0.2,10.0.0.3,10.0.0.4"), rule(name="b", listen_port=444, target_ip="10.9.9.9")]
        for index, expected in enumerate(["10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.2", "10.0.0.3"]):
            rendered = render_rules_for_server(rules, index)
            self.assertEqual(rendered[0]["target_ip"], expected)
            # одиночный адрес не меняется, остальные поля переносятся как есть
            self.assertEqual(rendered[1]["target_ip"], "10.9.9.9")
            self.assertEqual(rendered[0]["target_port"], 8443)
        self.assertEqual(assigned_targets(rules, 1), {"a": "10.0.0.3"})

    def test_node_side_modes_keep_full_list(self):
        for mode in ("random", "round_robin", "client_hash"):
            rules = [rule(target_ip="10.0.0.2,10.0.0.3", distribution=mode)]
            for index in range(3):
                self.assertEqual(render_rules_for_server(rules, index)[0]["target_ip"], "10.0.0.2,10.0.0.3")
            self.assertEqual(assigned_targets(rules, 1), {})
        # без режима панель раздаёт по серверам и хэши разных серверов различаются
        self.assertEqual(rule()["distribution"], "per_server")

    def test_hash_differs_per_server(self):
        rules = [rule(target_ip="10.0.0.2,10.0.0.3")]
        self.assertNotEqual(
            compute_rules_hash(render_rules_for_server(rules, 0)),
            compute_rules_hash(render_rules_for_server(rules, 1)),
        )
        self.assertEqual(
            compute_rules_hash(render_rules_for_server(rules, 0)),
            compute_rules_hash(render_rules_for_server(rules, 2)),
        )


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
