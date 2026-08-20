"""Тесты чистых функций резервации портов (app/services/reserved_ports_sync.py).

Голый unittest, без PostgreSQL и без сети: разбор и нормализация списков портов,
слияние глобального и серверного значений, гейт по версии агента.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services.reserved_ports_sync import (  # noqa: E402
        MAX_ENTRIES,
        MIN_NODE_VERSION_RESERVED_PORTS,
        merged_entries,
        node_supports_reserved_ports,
        parse_ports_value,
    )
except ImportError as e:  # рантайм панели (sqlalchemy, httpx) не установлен
    raise unittest.SkipTest(f"reserved_ports_sync requires the panel runtime: {e}")


class ParsePortsValueTest(unittest.TestCase):
    def test_empty_and_none_are_empty(self):
        self.assertEqual(parse_ports_value(""), [])
        self.assertEqual(parse_ports_value(None), [])
        self.assertEqual(parse_ports_value("  ,; "), [])

    def test_normalizes_separators_and_order(self):
        self.assertEqual(
            parse_ports_value("8443-8450, 5201;9200 5201"),
            ["5201", "8443-8450", "9200"],
        )

    def test_single_port_range_collapsed(self):
        self.assertEqual(parse_ports_value("5201-5201"), ["5201"])

    def test_rejects_garbage_and_bounds(self):
        for bad in ["musor", "0", "65536", "10-5", "1-70000", "80a"]:
            with self.assertRaises(ValueError, msg=bad):
                parse_ports_value(bad)

    def test_rejects_too_many_entries(self):
        value = ",".join(str(1000 + i) for i in range(MAX_ENTRIES + 1))
        with self.assertRaises(ValueError):
            parse_ports_value(value)

    def test_rejects_range_eating_ephemeral_space(self):
        with self.assertRaises(ValueError):
            parse_ports_value("1024-65535")


class MergedEntriesTest(unittest.TestCase):
    def test_merges_global_and_server(self):
        self.assertEqual(
            merged_entries("5201,8443-8450", "9200"),
            ["5201", "8443-8450", "9200"],
        )

    def test_deduplicates_across_sources(self):
        self.assertEqual(merged_entries("5201", "5201"), ["5201"])

    def test_handles_missing_sides(self):
        self.assertEqual(merged_entries(None, "5201"), ["5201"])
        self.assertEqual(merged_entries("5201", None), ["5201"])
        self.assertEqual(merged_entries(None, None), [])


class NodeSupportsTest(unittest.TestCase):
    def test_minimum_version_supported(self):
        self.assertTrue(node_supports_reserved_ports(MIN_NODE_VERSION_RESERVED_PORTS))

    def test_newer_supported_older_not(self):
        self.assertTrue(node_supports_reserved_ports("11.0.0"))
        self.assertFalse(node_supports_reserved_ports("10.27.0"))

    def test_unknown_version_not_supported(self):
        self.assertFalse(node_supports_reserved_ports(None))
        self.assertFalse(node_supports_reserved_ports(""))


if __name__ == "__main__":
    unittest.main()
