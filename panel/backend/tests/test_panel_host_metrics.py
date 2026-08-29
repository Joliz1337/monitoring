"""Чистые функции истории нагрузки хоста панели.

Запуск: python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from collections import namedtuple
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.panel_host_metrics import (  # noqa: E402
    HOST_PERIODS,
    HostSample,
    cpu_percent_between,
    detect_gaps,
    summarize_samples,
)

CpuTimes = namedtuple("CpuTimes", "user nice system idle iowait irq softirq steal guest guest_nice")


def sample(cpu: float, mem: float = 50.0, load: float = 1.0) -> HostSample:
    return HostSample(cpu_percent=cpu, memory_percent=mem, memory_used=1000, memory_available=1000, load_avg_1=load)


class SummarizeSamplesTest(unittest.TestCase):
    def test_average_and_peak(self):
        row = summarize_samples([sample(10, mem=40, load=0.5), sample(30, mem=60, load=1.5), sample(20, mem=50, load=1.0)])
        self.assertAlmostEqual(row["cpu_usage"], 20.0)
        self.assertEqual(row["cpu_usage_max"], 30)
        self.assertAlmostEqual(row["memory_percent"], 50.0)
        self.assertEqual(row["memory_percent_max"], 60)
        self.assertAlmostEqual(row["load_avg_1"], 1.0)
        self.assertEqual(row["load_avg_1_max"], 1.5)
        self.assertEqual(row["memory_used"], 1000)

    def test_single_sample_is_its_own_peak(self):
        row = summarize_samples([sample(42)])
        self.assertEqual(row["cpu_usage"], row["cpu_usage_max"])


class CpuPercentBetweenTest(unittest.TestCase):
    def test_guest_is_not_double_counted_and_iowait_is_idle(self):
        before = CpuTimes(user=100, nice=0, system=50, idle=800, iowait=50, irq=0, softirq=0, steal=0, guest=20, guest_nice=0)
        # +100 busy (user), +100 idle, +100 iowait → 100 / 300 = 33.3%
        after = CpuTimes(user=200, nice=0, system=50, idle=900, iowait=150, irq=0, softirq=0, steal=0, guest=20, guest_nice=0)
        self.assertAlmostEqual(cpu_percent_between(before, after), 100 / 300 * 100, places=3)

    def test_no_delta_is_zero(self):
        times = CpuTimes(1, 0, 1, 1, 0, 0, 0, 0, 0, 0)
        self.assertEqual(cpu_percent_between(times, times), 0.0)


class DetectGapsTest(unittest.TestCase):
    def test_only_holes_longer_than_threshold(self):
        base = datetime(2026, 8, 26, 12, 0, 0)
        stamps = [base, base + timedelta(seconds=10), base + timedelta(seconds=120), base + timedelta(seconds=130)]
        self.assertEqual(detect_gaps(stamps, 30), [(stamps[1], stamps[2])])

    def test_no_points_no_gaps(self):
        self.assertEqual(detect_gaps([], 30), [])


class HostPeriodsTest(unittest.TestCase):
    def test_raw_hour_and_bucketed_rest(self):
        self.assertIsNone(HOST_PERIODS["1h"].bucket_sec)
        self.assertEqual(HOST_PERIODS["24h"].bucket_sec, 300)
        self.assertEqual(HOST_PERIODS["30d"].bucket_sec, 3600)


if __name__ == "__main__":
    unittest.main()
