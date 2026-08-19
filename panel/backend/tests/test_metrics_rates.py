"""Скорости в метриках: нода считает сама или панель по дельте счётчиков.

Нода с посекундным семплером присылает готовые байт/с по каждому интерфейсу и
диску и маркер `live_rates`. Такие цифры панель берёт как есть — в снапшот и в
ответы API. Нода без маркера (старый агент, только что стартовала, семплер
замолчал) получает прежнее поведение: скорость из снапшота, размазанная по
интерфейсам и дискам пропорционально накопленным байтам.
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.metrics_rates import NodeRates, enrich_metrics_with_speeds, node_live_rates  # noqa: E402


def node_metrics(with_marker: bool = True) -> dict:
    metrics = {
        "network": {
            "total": {"rx_bytes": 900, "tx_bytes": 100, "rx_bytes_per_sec": 1_000.0, "tx_bytes_per_sec": 2_000.0},
            "interfaces": [
                {"name": "eth0", "is_virtual": False, "rx_bytes": 900, "tx_bytes": 100,
                 "rx_bytes_per_sec": 1_000.0, "tx_bytes_per_sec": 2_000.0},
                {"name": "veth1", "is_virtual": True, "rx_bytes": 900, "tx_bytes": 100,
                 "rx_bytes_per_sec": 1_000.0, "tx_bytes_per_sec": 2_000.0},
            ],
        },
        "disk": {
            "io": {"sda": {"read_bytes": 10, "write_bytes": 90, "read_bytes_per_sec": 300.0, "write_bytes_per_sec": 400.0}},
            "io_total": {"read_bytes_per_sec": 300.0, "write_bytes_per_sec": 400.0},
        },
    }
    if with_marker:
        metrics["live_rates"] = {"window_sec": 1.0, "sampled_at": 1_700_000_000.0}
    return metrics


SNAPSHOT = SimpleNamespace(
    net_rx_bytes_per_sec=50.0, net_tx_bytes_per_sec=60.0,
    disk_read_bytes_per_sec=7.0, disk_write_bytes_per_sec=8.0,
)


class NodeLiveRatesTests(unittest.TestCase):

    def test_rates_are_read_from_node_totals(self):
        self.assertEqual(
            node_live_rates(node_metrics()),
            NodeRates(net_rx=1_000.0, net_tx=2_000.0, disk_read=300.0, disk_write=400.0),
        )

    def test_no_marker_means_no_node_rates(self):
        self.assertIsNone(node_live_rates(node_metrics(with_marker=False)))

    def test_marker_without_totals_still_yields_zeros_not_crash(self):
        self.assertEqual(
            node_live_rates({"live_rates": {"window_sec": 1.0, "sampled_at": 1.0}}),
            NodeRates(net_rx=0.0, net_tx=0.0, disk_read=0.0, disk_write=0.0),
        )


class EnrichTests(unittest.TestCase):

    def test_node_rates_are_left_untouched(self):
        metrics = enrich_metrics_with_speeds(node_metrics(), SNAPSHOT)

        self.assertEqual(metrics["network"]["total"]["rx_bytes_per_sec"], 1_000.0)
        self.assertEqual(metrics["network"]["interfaces"][0]["rx_bytes_per_sec"], 1_000.0)
        self.assertEqual(metrics["disk"]["io"]["sda"]["write_bytes_per_sec"], 400.0)

    def test_legacy_node_gets_snapshot_speed_spread_over_physical_interfaces(self):
        metrics = enrich_metrics_with_speeds(node_metrics(with_marker=False), SNAPSHOT)

        self.assertEqual(metrics["network"]["total"]["rx_bytes_per_sec"], 50.0)
        eth0, veth1 = metrics["network"]["interfaces"]
        self.assertEqual((eth0["rx_bytes_per_sec"], eth0["tx_bytes_per_sec"]), (50.0, 60.0))
        self.assertEqual((veth1["rx_bytes_per_sec"], veth1["tx_bytes_per_sec"]), (0.0, 0.0))

    def test_legacy_node_gets_snapshot_disk_speed_spread_by_bytes(self):
        metrics = enrich_metrics_with_speeds(node_metrics(with_marker=False), SNAPSHOT)

        sda = metrics["disk"]["io"]["sda"]
        self.assertEqual((sda["read_bytes_per_sec"], sda["write_bytes_per_sec"]), (7.0, 8.0))

    def test_legacy_node_without_snapshot_is_returned_as_is(self):
        metrics = node_metrics(with_marker=False)
        self.assertIs(enrich_metrics_with_speeds(metrics, None), metrics)


if __name__ == "__main__":
    unittest.main()
