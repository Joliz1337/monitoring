"""Metrics collector copies one-second rates from the sampler into /api/metrics.

Without a fresh sample the speed fields stay zero and no `live_rates` marker is
emitted — the panel then falls back to its own counter deltas, as it did for
nodes that had no sampler at all.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.metrics_collector import MetricsCollector  # noqa: E402
from app.services.rate_sampler import RateSample  # noqa: E402


def bare_collector() -> MetricsCollector:
    collector = MetricsCollector.__new__(MetricsCollector)
    collector.settings = SimpleNamespace(host_proc="/host/proc", node_name="test")
    return collector


NET_DEV = {
    "eth0": dict(rx_bytes=10, rx_packets=1, rx_errors=0, rx_drops=0,
                 tx_bytes=20, tx_packets=1, tx_errors=0, tx_drops=0),
    "veth1": dict(rx_bytes=10, rx_packets=1, rx_errors=0, rx_drops=0,
                  tx_bytes=20, tx_packets=1, tx_errors=0, tx_drops=0),
}

SAMPLE = RateSample(
    sampled_at=1_700_000_000.0,
    window_sec=1.0,
    per_cpu_percent=[12.5, 37.5],
    net={"eth0": (1_000.0, 2_000.0), "veth1": (1_000.0, 2_000.0)},
    disk={"sda": (300.0, 400.0), "sda1": (300.0, 400.0)},
    disk_total=(300.0, 400.0),
)


class NetworkRatesTests(unittest.TestCase):

    def test_interface_speed_comes_from_the_sample(self):
        with mock.patch("app.services.metrics_collector.read_net_dev", return_value=NET_DEV):
            network = bare_collector().get_network_info(SAMPLE)

        eth0 = next(i for i in network["interfaces"] if i["name"] == "eth0")
        self.assertEqual((eth0["rx_bytes_per_sec"], eth0["tx_bytes_per_sec"]), (1_000.0, 2_000.0))

    def test_total_speed_sums_physical_interfaces_only(self):
        with mock.patch("app.services.metrics_collector.read_net_dev", return_value=NET_DEV):
            network = bare_collector().get_network_info(SAMPLE)

        # veth1 mirrors eth0's traffic — counting it would double the total
        self.assertEqual(network["total"]["rx_bytes_per_sec"], 1_000.0)
        self.assertEqual(network["total"]["tx_bytes_per_sec"], 2_000.0)

    def test_without_sample_speeds_are_zero(self):
        with mock.patch("app.services.metrics_collector.read_net_dev", return_value=NET_DEV):
            network = bare_collector().get_network_info(None)

        self.assertEqual(network["total"]["rx_bytes_per_sec"], 0.0)
        eth0 = next(i for i in network["interfaces"] if i["name"] == "eth0")
        self.assertEqual(eth0["rx_bytes_per_sec"], 0.0)


class DiskRatesTests(unittest.TestCase):

    def test_per_disk_and_total_speed_come_from_the_sample(self):
        counters = {"sda": SimpleNamespace(read_bytes=1, write_bytes=2, read_count=0, write_count=0,
                                           read_time=0, write_time=0)}
        with mock.patch("app.services.metrics_collector.psutil.disk_io_counters", return_value=counters), \
             mock.patch("app.services.metrics_collector.psutil.disk_partitions", return_value=[]):
            disk = bare_collector().get_disk_info(SAMPLE)

        self.assertEqual(disk["io"]["sda"]["read_bytes_per_sec"], 300.0)
        self.assertEqual(disk["io"]["sda"]["write_bytes_per_sec"], 400.0)
        self.assertEqual(disk["io_total"], {"read_bytes_per_sec": 300.0, "write_bytes_per_sec": 400.0})


class LiveRatesMarkerTests(unittest.TestCase):

    def test_marker_carries_window_and_sample_time(self):
        self.assertEqual(
            MetricsCollector.live_rates(SAMPLE),
            {"window_sec": 1.0, "sampled_at": 1_700_000_000.0},
        )

    def test_no_marker_without_a_fresh_sample(self):
        self.assertIsNone(MetricsCollector.live_rates(None))


class ResponseModelTests(unittest.TestCase):
    """FastAPI silently drops anything the response_model does not declare.

    This is exactly how live_rates and disk.io_total got lost in production:
    the collector emitted them, AllMetrics filtered them out, the panel saw an
    up-to-date node without the marker and kept averaging speeds itself.
    """

    def test_live_rates_and_io_total_survive_the_response_model(self):
        from app.models.metrics import AllMetrics

        payload = {
            "timestamp": "2026-08-19T00:00:00",
            "server_name": "test",
            "cpu": {
                "cores_physical": 1, "cores_logical": 1, "model": "x",
                "usage_percent": 25.0, "per_cpu_percent": [25.0],
                "load_avg_1": 0, "load_avg_5": 0, "load_avg_15": 0,
                "frequency": {"current": 0, "min": 0, "max": 0},
            },
            "memory": {
                "ram": {"total": 1, "used": 1, "free": 0, "available": 0, "percent": 100.0},
                "swap": {"total": 0, "used": 0, "free": 0, "percent": 0.0},
            },
            "disk": {
                "partitions": [],
                "io": {},
                "io_total": {"read_bytes_per_sec": 300.0, "write_bytes_per_sec": 400.0},
            },
            "network": {
                "interfaces": [],
                "total": {"rx_bytes": 0, "tx_bytes": 0, "rx_packets": 0, "tx_packets": 0,
                          "rx_bytes_per_sec": 1_000.0, "tx_bytes_per_sec": 2_000.0},
            },
            "processes": {"total": 0, "running": 0, "sleeping": 0, "top_by_cpu": [], "top_by_memory": []},
            "system": {
                "hostname": "h", "os": "linux", "kernel": "6", "architecture": "x86_64",
                "boot_time": "2026-08-19T00:00:00", "uptime_seconds": 1, "uptime_human": "1m",
                "open_files": 0,
                "connections": {"established": 0, "listen": 0, "time_wait": 0, "other": 0},
                "server_name": "test",
            },
            "live_rates": {"window_sec": 1.0, "sampled_at": 1_700_000_000.0},
        }

        serialized = AllMetrics.model_validate(payload).model_dump()

        self.assertEqual(serialized["live_rates"], {"window_sec": 1.0, "sampled_at": 1_700_000_000.0})
        self.assertEqual(
            serialized["disk"]["io_total"],
            {"read_bytes_per_sec": 300.0, "write_bytes_per_sec": 400.0},
        )

    def test_window_block_survives_the_response_model(self):
        from app.models.metrics import AllMetrics

        serialized = AllMetrics.model_validate({**MINIMAL_PAYLOAD, "window": WINDOW_BLOCK}).model_dump()

        self.assertEqual(serialized["window"], WINDOW_BLOCK)

    def test_window_is_null_unless_requested(self):
        from app.models.metrics import AllMetrics

        self.assertIsNone(AllMetrics.model_validate(MINIMAL_PAYLOAD).model_dump()["window"])


MINIMAL_PAYLOAD = {
    "timestamp": "2026-08-26T00:00:00",
    "server_name": "test",
    "cpu": {
        "cores_physical": 1, "cores_logical": 1, "model": "x",
        "usage_percent": 25.0, "per_cpu_percent": [25.0],
        "load_avg_1": 0, "load_avg_5": 0, "load_avg_15": 0,
        "frequency": {"current": 0, "min": 0, "max": 0},
    },
    "memory": {
        "ram": {"total": 1, "used": 1, "free": 0, "available": 0, "percent": 100.0},
        "swap": {"total": 0, "used": 0, "free": 0, "percent": 0.0},
    },
    "disk": {"partitions": [], "io": {}},
    "network": {
        "interfaces": [],
        "total": {"rx_bytes": 0, "tx_bytes": 0, "rx_packets": 0, "tx_packets": 0},
    },
    "processes": {"total": 0, "running": 0, "sleeping": 0, "top_by_cpu": [], "top_by_memory": []},
    "system": {
        "hostname": "h", "os": "linux", "kernel": "6", "architecture": "x86_64",
        "boot_time": "2026-08-26T00:00:00", "uptime_seconds": 1, "uptime_human": "1m",
        "open_files": 0,
        "connections": {"established": 0, "listen": 0, "time_wait": 0, "other": 0},
        "server_name": "test",
    },
}

WINDOW_BLOCK = {
    "window_sec": 10.2, "samples": 10,
    "cpu_avg": 12.3, "cpu_max": 41.0, "per_cpu_avg": [10.0, 14.6],
    "net_rx_avg": 1234.5, "net_tx_avg": 234.5, "net_rx_max": 9876.0, "net_tx_max": 876.0,
    "disk_read_avg": 0.0, "disk_write_avg": 0.0,
}


class FakeSampler:
    def __init__(self, samples):
        self.samples = samples
        self.requested = None

    def window(self, seconds):
        self.requested = seconds
        return self.samples


def window_sample(cpu, net, disk_total=(0.0, 0.0), window=1.0) -> RateSample:
    return RateSample(sampled_at=0.0, window_sec=window, per_cpu_percent=cpu, net=net, disk_total=disk_total)


class WindowStatsTests(unittest.TestCase):

    def collector_with(self, samples) -> tuple[MetricsCollector, FakeSampler]:
        collector = bare_collector()
        collector._rate_sampler = FakeSampler(samples)
        return collector, collector._rate_sampler

    def test_no_block_without_the_window_parameter(self):
        collector, fake = self.collector_with(())

        self.assertIsNone(collector.window_stats(None))
        self.assertIsNone(fake.requested)

    def test_no_block_when_the_sampler_window_is_stale(self):
        collector, _ = self.collector_with(None)

        self.assertIsNone(collector.window_stats(10))

    def test_block_carries_every_window_rates_field(self):
        from app.models.metrics import WindowRates

        samples = (
            window_sample([40.0, 20.0], {"eth0": (300.0, 30.0), "veth1": (999.0, 999.0)},
                          disk_total=(400.0, 40.0), window=2.0),
            window_sample([10.0, 30.0], {"eth0": (100.0, 10.0), "veth1": (999.0, 999.0)},
                          disk_total=(100.0, 10.0)),
        )
        collector, fake = self.collector_with(samples)

        with mock.patch.object(MetricsCollector, "_get_bond_slaves", return_value=set()):
            block = collector.window_stats(10)

        self.assertEqual(fake.requested, 10)
        self.assertEqual(set(block), set(WindowRates.model_fields))
        self.assertEqual(block["window_sec"], 3.0)
        self.assertEqual(block["samples"], 2)
        self.assertEqual((block["cpu_avg"], block["cpu_max"]), (26.7, 30.0))
        self.assertEqual(block["per_cpu_avg"], [30.0, 23.3])
        # veth1 mirrors eth0 and must not inflate the sums
        self.assertEqual((block["net_rx_avg"], block["net_rx_max"]), (233.3, 300.0))
        self.assertEqual((block["net_tx_avg"], block["net_tx_max"]), (23.3, 30.0))
        self.assertEqual((block["disk_read_avg"], block["disk_write_avg"]), (300.0, 30.0))


class PhysicalInterfacesTests(unittest.TestCase):

    def test_virtual_prefixes_and_bond_slaves_are_excluded(self):
        with mock.patch.object(MetricsCollector, "_get_bond_slaves", return_value={"eth1"}):
            physical = bare_collector().physical_interfaces(["eth0", "eth1", "veth0", "bond0", "wg0"])

        self.assertEqual(physical, ["eth0", "bond0"])


class CpuRatesTests(unittest.TestCase):

    def test_per_cpu_percent_comes_from_the_sample(self):
        with mock.patch.object(MetricsCollector, "_read_host_file", return_value=""):
            cpu = bare_collector().get_cpu_info(SAMPLE)

        self.assertEqual(cpu["per_cpu_percent"], [12.5, 37.5])
        self.assertEqual(cpu["usage_percent"], 25.0)

    def test_without_sample_cpu_is_zero(self):
        with mock.patch.object(MetricsCollector, "_read_host_file", return_value=""):
            cpu = bare_collector().get_cpu_info(None)

        self.assertEqual(cpu["per_cpu_percent"], [])
        self.assertEqual(cpu["usage_percent"], 0)


if __name__ == "__main__":
    unittest.main()
