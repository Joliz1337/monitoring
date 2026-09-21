"""Транзакции доп. IP: гейт версии ноды, адрес и порт ноды из URL, дедлайн с
запасом на расхождение часов, вычитание уже стоящих адресов, снимок задачи.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services.network_addresses import AddressSpec
    from app.services.network_transactions import (
        DEADLINE_GRACE_SECONDS,
        MIN_NODE_VERSION_NETWORK,
        ROLLBACK_TIMEOUT_SEC,
        JobPhase,
        NetworkJob,
        TransactionStatus,
        deadline_passed,
        managed_on_interface,
        missing_on_interface,
        node_api_port,
        node_host,
        node_supports_network,
        parse_deadline,
    )
except ImportError as e:  # pragma: no cover
    raise unittest.SkipTest(f"network_transactions requires the panel runtime: {e}")


class VersionGateTests(unittest.TestCase):
    def test_gate(self):
        self.assertTrue(node_supports_network(MIN_NODE_VERSION_NETWORK))
        self.assertTrue(node_supports_network("10.30.1"))
        self.assertFalse(node_supports_network("10.28.9"))
        self.assertFalse(node_supports_network(None))
        self.assertFalse(node_supports_network(""))


class UrlTests(unittest.TestCase):
    def test_host_and_port(self):
        self.assertEqual(node_host("https://1.2.3.4:9100"), "1.2.3.4")
        self.assertEqual(node_api_port("https://1.2.3.4:9100"), 9100)
        self.assertEqual(node_host("https://node.example.com"), "node.example.com")
        self.assertEqual(node_api_port("https://node.example.com"), 443)
        self.assertEqual(node_host("http://[2001:db8::1]:9100"), "2001:db8::1")
        self.assertEqual(node_api_port("http://node.example.com"), 80)
        self.assertIsNone(node_host(""))


class DeadlineTests(unittest.TestCase):
    NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)

    def test_parse_forms(self):
        self.assertEqual(parse_deadline("2026-09-02T12:02:00Z", self.NOW), self.NOW + timedelta(minutes=2))
        self.assertEqual(parse_deadline("2026-09-02T12:02:00", self.NOW), self.NOW + timedelta(minutes=2))
        self.assertEqual(parse_deadline("2026-09-02T15:02:00+03:00", self.NOW), self.NOW + timedelta(minutes=2))
        self.assertEqual(parse_deadline(None, self.NOW), self.NOW + timedelta(seconds=ROLLBACK_TIMEOUT_SEC))
        self.assertEqual(parse_deadline("garbage", self.NOW), self.NOW + timedelta(seconds=ROLLBACK_TIMEOUT_SEC))

    def test_passed_with_grace(self):
        deadline = self.NOW
        self.assertFalse(deadline_passed(deadline, self.NOW + timedelta(seconds=DEADLINE_GRACE_SECONDS - 1)))
        self.assertTrue(deadline_passed(deadline, self.NOW + timedelta(seconds=DEADLINE_GRACE_SECONDS + 1)))
        self.assertFalse(deadline_passed(None, self.NOW))


class InterfaceFilterTests(unittest.TestCase):
    IFACE = {"name": "eth0", "addresses": [
        {"address": "1.2.3.4", "prefix": 24, "managed": False},
        {"address": "1.2.3.5", "prefix": 32, "managed": True},
    ]}

    def test_missing_and_managed(self):
        specs = [AddressSpec("1.2.3.4", 24), AddressSpec("1.2.3.5", 32), AddressSpec("1.2.3.6", 32)]
        self.assertEqual([s.cidr for s in missing_on_interface(specs, self.IFACE)], ["1.2.3.6/32"])
        self.assertEqual([s.cidr for s in managed_on_interface(specs, self.IFACE)], ["1.2.3.5/32"])
        self.assertEqual(missing_on_interface(specs, {"name": "eth0"}), specs)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_serialisable(self):
        job = NetworkJob(id="1-1", server_id=1, interface="eth0", add=[AddressSpec("1.2.3.6", 32)], remove=[],
                         started_at=1788000000.0)
        job.deadline_at = datetime(2026, 9, 2, 12, 2, tzinfo=timezone.utc)
        snapshot = job.snapshot()
        self.assertEqual(snapshot["phase"], JobPhase.APPLYING.value)
        self.assertEqual(snapshot["status"], TransactionStatus.PENDING.value)
        self.assertEqual(snapshot["added"], [{"address": "1.2.3.6", "prefix": 32}])
        self.assertEqual(snapshot["deadline_at"], "2026-09-02T12:02:00Z")
        self.assertTrue(snapshot["started_at"].endswith("Z"))
        self.assertIsNone(snapshot["reachability"])
        import json
        json.dumps(snapshot)


if __name__ == "__main__":
    unittest.main()
