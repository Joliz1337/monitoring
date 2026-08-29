"""Tests for the one-second rate sampler.

The sampler replaces two sources of "averaged over whatever" numbers: per-CPU
busy share used to be measured between two *panel requests* (a 10 s window that
drifted with every live request in between), and network/disk speeds were not
measured on the node at all — the panel derived them from cumulative counters
over its own polling interval. Now a background task ticks every second and
keeps the latest one-second rates; `/api/metrics` only copies them out.

The sampler also keeps a ring buffer of those one-second samples so that
`/api/metrics?window=N` can report averages and peaks over exactly the seconds
between two panel polls instead of one instantaneous second out of ten.
"""

import os
import sys
import unittest
from collections import namedtuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.rate_sampler import (  # noqa: E402
    MAX_WINDOW_SEC,
    STALE_AFTER_SEC,
    RateSample,
    RateSampler,
    RawCounters,
    per_cpu_percent,
    summarize_window,
    weighted_mean,
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
        self.assertFalse(s.snapshot().cpu_measured)

    def test_measured_cpu_percent_is_flagged_as_such(self):
        s = sampler()
        s.advance(raw(10.0, cpu=[times()]))
        s.advance(raw(11.0, cpu=[times(user=0.5, idle=0.5)]))

        self.assertTrue(s.snapshot().cpu_measured)

    def test_inherited_cpu_percent_is_flagged_unmeasured(self):
        # A full second passed by the clock, but the kernel counters barely
        # ticked (VM freeze, heavy steal): the sample keeps the last percentages
        # and says so, so a window summary does not count that second twice.
        s = sampler()
        s.advance(raw(10.0, cpu=[times()]))
        s.advance(raw(11.0, cpu=[times(user=0.5, idle=0.5)]))
        s.advance(raw(12.0, cpu=[times(user=0.52, idle=0.5)]))

        self.assertEqual(s.snapshot().per_cpu_percent, [50.0])
        self.assertFalse(s.snapshot().cpu_measured)

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


def fed_sampler(ticks: int, step: float = 1.0) -> tuple[RateSampler, FakeClock]:
    """Sampler that accepted `ticks` readings `step` seconds apart, clock parked on the last one."""
    clock = FakeClock(0.0)
    s = sampler(clock=clock)
    for i in range(ticks + 1):
        clock.now = i * step
        busy = clock.now / 2
        s.advance(raw(clock.now, cpu=[times(user=busy, idle=busy)]))
    return s, clock


class WindowTests(unittest.TestCase):

    def test_buffer_is_capped_at_max_window_seconds_of_samples(self):
        s, _ = fed_sampler(MAX_WINDOW_SEC + 50)

        self.assertEqual(len(s.window(10 * MAX_WINDOW_SEC)), MAX_WINDOW_SEC)

    def test_window_takes_newest_samples_until_they_cover_the_requested_seconds(self):
        s, _ = fed_sampler(15)

        window = s.window(10)

        self.assertEqual(len(window), 10)
        self.assertEqual(sum(sample.window_sec for sample in window), 10.0)
        self.assertGreater(window[0].sampled_at, window[-1].sampled_at)

    def test_two_second_ticks_cover_the_window_with_half_the_samples(self):
        s, _ = fed_sampler(10, step=2.0)

        self.assertEqual(len(s.window(10)), 5)

    def test_short_buffer_yields_everything_it_has(self):
        s, _ = fed_sampler(3)

        self.assertEqual(len(s.window(10)), 3)

    def test_window_is_withheld_when_the_sampler_is_stale(self):
        s, clock = fed_sampler(15)
        clock.now += STALE_AFTER_SEC + 1

        self.assertIsNone(s.window(10))

    def test_window_is_none_before_the_first_sample(self):
        self.assertIsNone(sampler().window(10))


def sample(cpu, net=None, disk_total=(0.0, 0.0), window=1.0, measured=True, at=0.0) -> RateSample:
    return RateSample(
        sampled_at=at,
        window_sec=window,
        per_cpu_percent=cpu,
        cpu_measured=measured,
        net=net or {},
        disk_total=disk_total,
    )


PHYSICAL = {"eth0", "eth1"}


class WeightedMeanTests(unittest.TestCase):

    def test_values_are_weighted_by_their_window(self):
        self.assertEqual(weighted_mean([(10.0, 1.0), (40.0, 2.0)]), 30.0)

    def test_no_weight_gives_zero(self):
        self.assertEqual(weighted_mean([]), 0.0)


class SummarizeWindowTests(unittest.TestCase):
    """Samples go newest first, as `window()` yields them."""

    def test_window_length_and_sample_count_are_totals(self):
        summary = summarize_window([sample([0.0], window=2.0), sample([0.0], window=1.0)], PHYSICAL)

        self.assertEqual(summary.window_sec, 3.0)
        self.assertEqual(summary.samples, 2)

    def test_cpu_average_is_weighted_by_sample_window(self):
        # A missed tick yields one two-second sample: it stands for two seconds, not one
        summary = summarize_window([sample([40.0], window=2.0), sample([10.0], window=1.0)], PHYSICAL)

        self.assertEqual(summary.cpu_avg, 30.0)

    def test_cpu_max_is_the_peak_of_the_host_average_not_of_a_core(self):
        summary = summarize_window([sample([100.0, 0.0]), sample([80.0, 40.0])], PHYSICAL)

        self.assertEqual(summary.cpu_max, 60.0)

    def test_per_cpu_average_is_computed_per_core(self):
        summary = summarize_window([sample([20.0, 40.0]), sample([40.0, 60.0])], PHYSICAL)

        self.assertEqual(summary.per_cpu_avg, [30.0, 50.0])

    def test_inherited_cpu_sample_is_skipped_for_cpu_but_still_counts_for_the_window(self):
        samples = [
            sample([50.0]),
            sample([50.0], measured=False),
            sample([10.0]),
        ]

        summary = summarize_window(samples, PHYSICAL)

        self.assertEqual(summary.cpu_avg, 30.0)
        self.assertEqual(summary.window_sec, 3.0)
        self.assertEqual(summary.samples, 3)

    def test_core_count_change_inside_the_window_keeps_only_the_current_layout(self):
        summary = summarize_window([sample([10.0, 20.0, 30.0]), sample([90.0])], PHYSICAL)

        self.assertEqual(summary.per_cpu_avg, [10.0, 20.0, 30.0])
        self.assertEqual(summary.cpu_avg, 20.0)

    def test_window_without_a_single_measured_cpu_sample_has_no_cpu(self):
        summary = summarize_window([sample([50.0], measured=False)], PHYSICAL)

        self.assertEqual(summary.cpu_avg, 0.0)
        self.assertEqual(summary.cpu_max, 0.0)
        self.assertEqual(summary.per_cpu_avg, [])

    def test_network_is_summed_over_physical_interfaces_inside_each_sample(self):
        samples = [
            sample([0.0], net={"eth0": (300.0, 30.0)}),
            sample([0.0], net={"eth0": (100.0, 10.0), "eth1": (50.0, 5.0), "veth0": (1_000.0, 1_000.0)}),
        ]

        summary = summarize_window(samples, PHYSICAL)

        self.assertEqual((summary.net_rx_avg, summary.net_tx_avg), (225.0, 22.5))
        self.assertEqual((summary.net_rx_max, summary.net_tx_max), (300.0, 30.0))

    def test_network_peak_is_of_the_sum_not_of_a_single_interface(self):
        samples = [
            sample([0.0], net={"eth0": (150.0, 0.0), "eth1": (0.0, 0.0)}),
            sample([0.0], net={"eth0": (100.0, 0.0), "eth1": (100.0, 0.0)}),
        ]

        self.assertEqual(summarize_window(samples, PHYSICAL).net_rx_max, 200.0)

    def test_disk_averages_come_from_whole_disk_totals(self):
        samples = [
            sample([0.0], disk_total=(400.0, 40.0), window=3.0),
            sample([0.0], disk_total=(100.0, 10.0), window=1.0),
        ]

        summary = summarize_window(samples, PHYSICAL)

        self.assertEqual((summary.disk_read_avg, summary.disk_write_avg), (325.0, 32.5))


if __name__ == "__main__":
    unittest.main()
