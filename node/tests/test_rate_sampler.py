"""Tests for the one-second rate sampler.

The sampler replaces two sources of "averaged over whatever" numbers: per-CPU
busy share used to be measured between two *panel requests* (a 10 s window that
drifted with every live request in between), and network/disk speeds were not
measured on the node at all — the panel derived them from cumulative counters
over its own polling interval. Now a background task ticks every second and
keeps the latest one-second rates; `/api/metrics` only copies them out.
"""

import os
import sys
import unittest
from collections import namedtuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.rate_sampler import (  # noqa: E402
    STALE_AFTER_SEC,
    RateSampler,
    RawCounters,
    per_cpu_percent,
)


CpuTimes = namedtuple(
    "CpuTimes",
    "user nice system idle iowait irq softirq steal guest guest_nice",
)


def times(user=0.0, system=0.0, idle=0.0, softirq=0.0) -> CpuTimes:
    return CpuTimes(user, 0.0, system, idle, 0.0, 0.0, softirq, 0.0, 0.0, 0.0)


def raw(taken_at: float, cpu=None, net=None, disk=None) -> RawCounters:
    return RawCounters(
        taken_at=taken_at,
        wall_time=1_700_000_000.0 + taken_at,
        cpu_times=cpu if cpu is not None else [times(user=1.0, idle=9.0)],
        net=net or {},
        disk=disk or {},
    )


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def sampler(whole_disks=(), clock: FakeClock = None) -> RateSampler:
    return RateSampler(
        read_counters=lambda: None,
        is_whole_disk=lambda name: name in whole_disks,
        # Замеры в тестах датированы 10–12 с; часы стоят рядом, чтобы ничего не протухло
        clock=clock or FakeClock(12.0),
    )


class PerCpuPercentTests(unittest.TestCase):

    def test_full_interval_gives_real_percentages(self):
        before = [times(), times()]
        after = [times(user=2.5, idle=7.5), times(user=0.4, softirq=0.1, idle=9.5)]

        self.assertEqual(per_cpu_percent(before, after), [25.0, 5.0])

    def test_few_ticks_are_rejected_instead_of_reported_as_0_or_100(self):
        # 20 ms window: one core caught a single busy tick, the rest caught none.
        # This is the exact input that used to reach the panel as [100.0, 0.0, 0.0, 0.0].
        before = [times(), times(), times(), times()]
        after = [times(user=0.02), times(), times(), times(idle=0.01)]

        self.assertIsNone(per_cpu_percent(before, after))

    def test_one_lagging_core_rejects_the_whole_sample(self):
        before = [times(), times()]
        after = [times(user=2.5, idle=7.5), times(idle=0.05)]

        self.assertIsNone(per_cpu_percent(before, after))

    def test_fully_busy_core_is_capped_at_100(self):
        before = [times()]
        after = [times(user=6.0, system=4.0)]

        self.assertEqual(per_cpu_percent(before, after), [100.0])


class AdvanceTests(unittest.TestCase):

    def test_no_sample_until_two_readings_exist(self):
        s = sampler()
        s.advance(raw(10.0))

        self.assertIsNone(s.snapshot())

    def test_network_rate_is_bytes_delta_over_elapsed_seconds(self):
        s = sampler()
        s.advance(raw(10.0, net={"eth0": (1_000, 500)}))
        s.advance(raw(12.0, net={"eth0": (5_000, 1_500)}))

        sample = s.snapshot()
        self.assertEqual(sample.window_sec, 2.0)
        self.assertEqual(sample.net["eth0"], (2_000.0, 500.0))

    def test_disk_rate_is_bytes_delta_over_elapsed_seconds(self):
        s = sampler()
        s.advance(raw(10.0, disk={"sda": (100, 200)}))
        s.advance(raw(11.0, disk={"sda": (1_100, 2_200)}))

        self.assertEqual(s.snapshot().disk["sda"], (1_000.0, 2_000.0))

    def test_counter_going_backwards_reports_zero_not_negative(self):
        s = sampler()
        s.advance(raw(10.0, net={"eth0": (9_000, 9_000)}))
        s.advance(raw(11.0, net={"eth0": (100, 50)}))

        self.assertEqual(s.snapshot().net["eth0"], (0.0, 0.0))

    def test_interface_seen_only_once_has_no_rate_yet(self):
        s = sampler()
        s.advance(raw(10.0, net={"eth0": (0, 0)}))
        s.advance(raw(11.0, net={"eth0": (10, 10), "wg0": (10, 10)}))

        self.assertNotIn("wg0", s.snapshot().net)

    def test_vanished_interface_is_dropped_from_the_sample(self):
        s = sampler()
        s.advance(raw(10.0, net={"eth0": (0, 0), "wg0": (0, 0)}))
        s.advance(raw(11.0, net={"eth0": (10, 10)}))

        self.assertNotIn("wg0", s.snapshot().net)

    def test_cpu_percent_comes_from_the_tick_window(self):
        s = sampler()
        s.advance(raw(10.0, cpu=[times(), times()]))
        s.advance(raw(11.0, cpu=[times(user=0.2, idle=0.8), times(user=1.0)]))

        self.assertEqual(s.snapshot().per_cpu_percent, [20.0, 100.0])

    def test_reading_too_close_to_the_previous_one_is_ignored(self):
        # A 20 ms window has a handful of kernel ticks and a few packets: the
        # rates it yields are noise. Keep the baseline; the next tick measures
        # from it over a full window.
        s = sampler()
        s.advance(raw(10.0, cpu=[times()], net={"eth0": (0, 0)}))
        s.advance(raw(11.0, cpu=[times(user=0.5, idle=0.5)], net={"eth0": (1_000, 0)}))
        s.advance(raw(11.02, cpu=[times(user=0.51, idle=0.5)], net={"eth0": (1_500, 0)}))

        self.assertEqual(s.snapshot().per_cpu_percent, [50.0])
        self.assertEqual(s.snapshot().net["eth0"], (1_000.0, 0.0))

        s.advance(raw(12.0, cpu=[times(user=1.0, idle=1.0)], net={"eth0": (3_000, 0)}))
        self.assertEqual(s.snapshot().window_sec, 1.0)
        self.assertEqual(s.snapshot().net["eth0"], (2_000.0, 0.0))

    def test_core_count_change_resets_percent_to_zeros_of_new_length(self):
        s = sampler()
        s.advance(raw(10.0, cpu=[times()]))
        s.advance(raw(11.0, cpu=[times(user=0.5, idle=0.5)]))
        s.advance(raw(12.0, cpu=[times(user=1.0), times(idle=1.0), times(idle=1.0)]))

        self.assertEqual(s.snapshot().per_cpu_percent, [0.0, 0.0, 0.0])

    def test_disk_total_counts_whole_disks_only(self):
        # sda1 is a partition of sda: its bytes are already inside sda's counter
        s = sampler(whole_disks={"sda", "nvme0n1"})
        s.advance(raw(10.0, disk={"sda": (0, 0), "sda1": (0, 0), "nvme0n1": (0, 0)}))
        s.advance(raw(11.0, disk={"sda": (100, 10), "sda1": (100, 10), "nvme0n1": (50, 5)}))

        self.assertEqual(s.snapshot().disk_total, (150.0, 15.0))

    def test_disk_total_falls_back_to_all_devices_when_none_is_known_whole(self):
        s = sampler(whole_disks=set())
        s.advance(raw(10.0, disk={"vda": (0, 0)}))
        s.advance(raw(11.0, disk={"vda": (100, 10)}))

        self.assertEqual(s.snapshot().disk_total, (100.0, 10.0))

    def test_sampled_at_is_wall_time_of_the_latest_reading(self):
        s = sampler()
        s.advance(raw(10.0))
        s.advance(raw(11.0))

        self.assertEqual(s.snapshot().sampled_at, 1_700_000_011.0)


class StalenessTests(unittest.TestCase):

    def test_stale_sample_is_withheld(self):
        clock = FakeClock(11.5)
        s = sampler(clock=clock)
        s.advance(raw(10.0))
        s.advance(raw(11.0))

        clock.now = 11.0 + STALE_AFTER_SEC + 1
        self.assertIsNone(s.snapshot())

    def test_fresh_sample_is_served(self):
        s = sampler(clock=FakeClock(11.5))
        s.advance(raw(10.0))
        s.advance(raw(11.0))

        self.assertIsNotNone(s.snapshot())


if __name__ == "__main__":
    unittest.main()
