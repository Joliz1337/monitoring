"""Окно опроса: что просить у ноды и чьи цифры класть в снапшот.

Панель просит у ноды `window=N`, где N — секунды с прошлого удачного опроса
этой ноды по monotonic-часам, в пределах [интервал, глубина буфера ноды].
В снапшот идут средние и пики из блока `window`; без него — секундные скорости
ноды; без них — дельты счётчиков, посчитанные панелью. Цикл сбора идёт по
фиксированному такту и при опоздании перескакивает на будущую отметку.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.metrics_collector import next_collection_tick  # noqa: E402
from app.services.metrics_rates import (  # noqa: E402
    MAX_WINDOW_SEC,
    NodeRates,
    NodeWindow,
    node_window_rates,
    poll_window_seconds,
    snapshot_rates,
)

WINDOW_BLOCK = {
    "window_sec": 10.2, "samples": 10,
    "cpu_avg": 12.3, "cpu_max": 41.0,
    "per_cpu_avg": [10.0, 14.6],
    "net_rx_avg": 1234.5, "net_tx_avg": 234.5, "net_rx_max": 9876.0, "net_tx_max": 876.0,
    "disk_read_avg": 5.0, "disk_write_avg": 6.0,
}

PANEL_DELTAS = NodeRates(net_rx=50.0, net_tx=60.0, disk_read=7.0, disk_write=8.0)


def node_metrics(window: bool = False, live: bool = False) -> dict:
    metrics = {
        "cpu": {"usage_percent": 77.0, "per_cpu_percent": [70.0, 84.0]},
        "network": {"total": {"rx_bytes_per_sec": 1_000.0, "tx_bytes_per_sec": 2_000.0}},
        "disk": {"io_total": {"read_bytes_per_sec": 300.0, "write_bytes_per_sec": 400.0}},
    }
    if live:
        metrics["live_rates"] = {"window_sec": 1.0, "sampled_at": 1_700_000_000.0}
    if window:
        metrics["window"] = dict(WINDOW_BLOCK)
    return metrics


class NodeWindowRatesTests(unittest.TestCase):

    def test_block_is_parsed_into_averages_and_peaks(self):
        self.assertEqual(
            node_window_rates(node_metrics(window=True)),
            NodeWindow(
                window_sec=10.2, samples=10, cpu_avg=12.3, cpu_max=41.0, per_cpu_avg=(10.0, 14.6),
                net_rx=1234.5, net_tx=234.5, net_rx_max=9876.0, net_tx_max=876.0,
                disk_read=5.0, disk_write=6.0,
            ),
        )

    def test_missing_block_means_no_window(self):
        self.assertIsNone(node_window_rates(node_metrics()))

    def test_null_block_from_node_without_parameter_means_no_window(self):
        self.assertIsNone(node_window_rates({"window": None}))

    def test_block_without_samples_means_no_window(self):
        self.assertIsNone(node_window_rates({"window": {**WINDOW_BLOCK, "samples": 0}}))


class SnapshotRatesTests(unittest.TestCase):

    def test_window_wins_over_live_rates_and_instant_cpu(self):
        rates = snapshot_rates(node_metrics(window=True, live=True), PANEL_DELTAS)

        self.assertEqual((rates.cpu_usage, rates.cpu_usage_max), (12.3, 41.0))
        self.assertEqual(rates.per_cpu_percent, [10.0, 14.6])
        self.assertEqual((rates.net_rx, rates.net_tx), (1234.5, 234.5))
        self.assertEqual((rates.net_rx_max, rates.net_tx_max), (9876.0, 876.0))
        self.assertEqual((rates.disk_read, rates.disk_write), (5.0, 6.0))

    def test_live_rates_without_window_keep_instant_cpu_and_no_peaks(self):
        rates = snapshot_rates(node_metrics(live=True), PANEL_DELTAS)

        self.assertEqual(rates.cpu_usage, 77.0)
        self.assertEqual(rates.per_cpu_percent, [70.0, 84.0])
        self.assertEqual((rates.net_rx, rates.net_tx), (1_000.0, 2_000.0))
        self.assertEqual((rates.disk_read, rates.disk_write), (300.0, 400.0))
        self.assertEqual((rates.cpu_usage_max, rates.net_rx_max, rates.net_tx_max), (None, None, None))

    def test_legacy_node_falls_back_to_panel_deltas(self):
        rates = snapshot_rates(node_metrics(), PANEL_DELTAS)

        self.assertEqual(rates.cpu_usage, 77.0)
        self.assertEqual((rates.net_rx, rates.net_tx), (50.0, 60.0))
        self.assertEqual((rates.disk_read, rates.disk_write), (7.0, 8.0))
        self.assertEqual((rates.cpu_usage_max, rates.net_rx_max, rates.net_tx_max), (None, None, None))

    def test_missing_cpu_block_yields_zero_and_empty_cores(self):
        rates = snapshot_rates({}, PANEL_DELTAS)

        self.assertEqual((rates.cpu_usage, rates.per_cpu_percent), (0, []))

    def test_window_without_measured_cpu_keeps_instant_cpu_but_window_rates(self):
        metrics = node_metrics(window=True)
        metrics["window"].update({"cpu_avg": 0.0, "cpu_max": 0.0, "per_cpu_avg": []})

        rates = snapshot_rates(metrics, PANEL_DELTAS)

        self.assertEqual(rates.cpu_usage, 77.0)
        self.assertEqual(rates.per_cpu_percent, [70.0, 84.0])
        self.assertIsNone(rates.cpu_usage_max)
        self.assertEqual((rates.net_rx, rates.net_rx_max), (1234.5, 9876.0))


class PollWindowSecondsTests(unittest.TestCase):

    def test_first_poll_after_start_asks_for_one_interval(self):
        self.assertEqual(poll_window_seconds(None, 10), 10)

    def test_gap_between_polls_is_rounded_to_whole_seconds(self):
        self.assertEqual(poll_window_seconds(10.2, 10), 10)
        self.assertEqual(poll_window_seconds(47.6, 10), 48)

    def test_gap_shorter_than_interval_is_clamped_up(self):
        self.assertEqual(poll_window_seconds(3.0, 10), 10)

    def test_gap_longer_than_node_buffer_is_clamped_down(self):
        self.assertEqual(poll_window_seconds(1_000.0, 10), MAX_WINDOW_SEC)
        self.assertEqual(MAX_WINDOW_SEC, 330)


class NextCollectionTickTests(unittest.TestCase):

    def test_on_time_cycle_keeps_the_grid(self):
        self.assertEqual(next_collection_tick(100.0, 10, 103.0), 110.0)

    def test_small_delay_shortens_the_sleep_not_the_grid(self):
        self.assertEqual(next_collection_tick(100.0, 10, 109.9), 110.0)

    def test_delay_of_a_full_period_skips_to_the_next_future_tick(self):
        self.assertEqual(next_collection_tick(100.0, 10, 110.0), 120.0)
        self.assertEqual(next_collection_tick(100.0, 10, 125.0), 130.0)
        self.assertEqual(next_collection_tick(100.0, 10, 400.0), 410.0)


if __name__ == "__main__":
    unittest.main()
