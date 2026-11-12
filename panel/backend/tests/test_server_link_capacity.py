"""Tests for the server link capacity used by load-based balancer weights.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Канал — знаменатель загрузки: ноль или мусор дали бы деление на ноль или
вечный вес 0. Подсказка со скоростью карты берётся с интерфейса, через
который реально идёт трафик, а не с docker0 со своими фиктивными 10 Гбит/с.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError  # noqa: E402

from app.routers.haproxy_profiles import ServerCapacitiesUpdate  # noqa: E402
from app.services.haproxy_load_weights import nic_speed_mbps  # noqa: E402


class CapacityModelTests(unittest.TestCase):
    def test_accepts_range_and_null(self):
        data = ServerCapacitiesUpdate(capacities={1: 10, 2: 3000, 3: 400_000, 4: None})
        self.assertEqual(data.capacities, {1: 10, 2: 3000, 3: 400_000, 4: None})

    def test_json_keys_become_server_ids(self):
        data = ServerCapacitiesUpdate.model_validate_json('{"capacities": {"7": 3000}}')
        self.assertEqual(data.capacities, {7: 3000})

    def test_rejects_out_of_range(self):
        for value in (9, 0, -1, 400_001):
            with self.assertRaises(ValidationError, msg=value):
                ServerCapacitiesUpdate(capacities={1: value})


class NicSpeedTests(unittest.TestCase):
    @staticmethod
    def metrics(*interfaces) -> dict:
        return {"network": {"interfaces": list(interfaces)}}

    @staticmethod
    def iface(name, speed, traffic, virtual=False) -> dict:
        return {"name": name, "speed_mbps": speed, "rx_bytes": traffic, "tx_bytes": 0, "is_virtual": virtual}

    def test_busiest_physical_interface_wins(self):
        raw = self.metrics(self.iface("eth0", 1000, 10), self.iface("eth1", 10000, 999),
                           self.iface("docker0", 10000, 10**9, virtual=True))
        self.assertEqual(nic_speed_mbps(raw), 10000)

    def test_interface_without_speed_is_ignored(self):
        raw = self.metrics(self.iface("eth0", None, 10**9), self.iface("eth1", 1000, 1))
        self.assertEqual(nic_speed_mbps(raw), 1000)

    def test_unknown_speed_and_garbage(self):
        self.assertIsNone(nic_speed_mbps(self.metrics(self.iface("eth0", None, 10))))
        self.assertIsNone(nic_speed_mbps(self.metrics("eth0")))
        self.assertIsNone(nic_speed_mbps({"network": None}))
        self.assertIsNone(nic_speed_mbps({}))
        self.assertIsNone(nic_speed_mbps(None))


if __name__ == "__main__":
    unittest.main()
