"""Tests for per-CPU sampling in the metrics collector.

The bug these exist for: the sampler used to gate re-sampling on a wall clock
(`time.monotonic()`) read *before* the `/proc/stat` read. Any delay between the
two — a thread preempted in the pool, two metrics requests queued on the same
lock — left the stored timestamp stale, so the next request passed the gate and
measured over a window of a few kernel ticks. A tick is 10 ms, so the busy share
degenerated into ratios of tiny integers: exactly 0, 33.3, 50, 66.7 or 100 per
core. The gate now reads the counters themselves, which cannot lie about how
much time they cover.
"""

import os
import sys
import unittest
from collections import namedtuple
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.metrics_collector import MetricsCollector  # noqa: E402


CpuTimes = namedtuple(
    "CpuTimes",
    "user nice system idle iowait irq softirq steal guest guest_nice",
)


def times(user=0.0, system=0.0, idle=0.0, softirq=0.0) -> CpuTimes:
    return CpuTimes(user, 0.0, system, idle, 0.0, 0.0, softirq, 0.0, 0.0, 0.0)


def bare_collector(prev: list) -> MetricsCollector:
    """Collector without __init__ — the sampler needs three fields, and the real
    constructor pulls in settings and the agent version file."""
    import threading

    collector = MetricsCollector.__new__(MetricsCollector)
    collector._prev_cpu_times = prev
    collector._last_cpu_percent = [0.0] * len(prev)
    collector._cpu_lock = threading.Lock()
    return collector


class PerCpuFromTests(unittest.TestCase):

    def test_full_interval_gives_real_percentages(self):
        before = [times(), times()]
        after = [times(user=2.5, idle=7.5), times(user=0.4, softirq=0.1, idle=9.5)]

        self.assertEqual(MetricsCollector._per_cpu_from(before, after), [25.0, 5.0])

    def test_few_ticks_are_rejected_instead_of_reported_as_0_or_100(self):
        # 20 ms window: one core caught a single busy tick, the rest caught none.
        # This is the exact input that used to reach the panel as [100.0, 0.0, 0.0, 0.0].
        before = [times(), times(), times(), times()]
        after = [times(user=0.02), times(), times(), times(idle=0.01)]

        self.assertIsNone(MetricsCollector._per_cpu_from(before, after))

    def test_one_lagging_core_rejects_the_whole_sample(self):
        before = [times(), times()]
        after = [times(user=2.5, idle=7.5), times(idle=0.05)]

        self.assertIsNone(MetricsCollector._per_cpu_from(before, after))

    def test_fully_busy_core_is_capped_at_100(self):
        before = [times()]
        after = [times(user=6.0, system=4.0)]

        self.assertEqual(MetricsCollector._per_cpu_from(before, after), [100.0])


class SamplePerCpuTests(unittest.TestCase):

    def test_rejected_sample_keeps_baseline_so_the_next_one_measures_it_all(self):
        start = [times(), times()]
        collector = bare_collector(start)

        degenerate = [times(user=0.02), times(idle=0.02)]
        full = [times(user=2.0, idle=8.0), times(user=1.0, idle=9.0)]

        with mock.patch(
            "app.services.metrics_collector.psutil.cpu_times",
            side_effect=[degenerate, full],
        ):
            self.assertEqual(collector._sample_per_cpu(), [0.0, 0.0])
            # Measured against `start`, not against the rejected snapshot
            self.assertEqual(collector._sample_per_cpu(), [20.0, 10.0])

    def test_last_valid_value_is_served_while_ticks_are_short(self):
        collector = bare_collector([times(), times()])
        collector._last_cpu_percent = [12.0, 34.0]

        with mock.patch(
            "app.services.metrics_collector.psutil.cpu_times",
            return_value=[times(user=0.01), times(idle=0.01)],
        ):
            self.assertEqual(collector._sample_per_cpu(), [12.0, 34.0])

    def test_core_count_change_resets_the_baseline(self):
        collector = bare_collector([times(), times()])
        collector._last_cpu_percent = [12.0, 34.0]
        resized = [times(), times(), times(), times()]

        with mock.patch(
            "app.services.metrics_collector.psutil.cpu_times",
            return_value=resized,
        ):
            self.assertEqual(collector._sample_per_cpu(), [0.0, 0.0, 0.0, 0.0])
            self.assertEqual(collector._prev_cpu_times, resized)


if __name__ == "__main__":
    unittest.main()
