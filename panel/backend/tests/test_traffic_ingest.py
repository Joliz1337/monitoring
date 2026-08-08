"""Тесты арифметики учёта трафика (app/services/traffic_ingest.py).

Голый unittest, без PostgreSQL и без сети: `TrafficIngest.observe()` синхронна и трогает
только память, а раскладка дельты, детект сброса счётчика и отсечка мусорных скоростей —
чистые функции уровня модуля.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"

Аккумулятор бакетов читается напрямую (`_accum`): единственный публичный выход из него —
`flush()` в PostgreSQL, а именно от базы тесты и должны быть свободны.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services.traffic_ingest import (  # noqa: E402
        MAX_BACKFILL_DAYS,
        MBIT_IN_BYTES_PER_SEC,
        SPEED_TOLERANCE,
        UNKNOWN_LINK_BYTES_PER_SEC,
        CounterState,
        ObservedSpeeds,
        ResetKind,
        TrafficIngest,
        clamp_backfill_start,
        detect_reset,
        floor_day,
        floor_hour,
        rate_is_sane,
        split_delta,
    )
except ImportError as e:  # рантайм панели (sqlalchemy, asyncpg) не установлен
    raise unittest.SkipTest(f"traffic_ingest requires the panel runtime: {e}")

SERVER_ID = 1
LINK_MBPS = 1000
IFACE = "eth0"


def naive(month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, second)


def epoch_of(moment: datetime) -> float:
    return moment.replace(tzinfo=timezone.utc).timestamp()


def iface(name: str, rx: int, tx: int, speed_mbps=LINK_MBPS, is_virtual: bool = False) -> dict:
    return {
        "name": name,
        "is_virtual": is_virtual,
        "speed_mbps": speed_mbps,
        "rx_bytes": rx,
        "tx_bytes": tx,
    }


def node_metrics(interfaces: list[dict], uptime: int, boot_id=None,
                 ports=None, sampled_at=None, total=None) -> dict:
    network = {
        "interfaces": interfaces,
        "total": total or {
            "rx_bytes": sum(i["rx_bytes"] for i in interfaces),
            "tx_bytes": sum(i["tx_bytes"] for i in interfaces),
        },
    }
    if ports is not None:
        network["ports"] = ports
        network["ports_available"] = True
        network["ports_sampled_at"] = sampled_at
    return {
        "network": network,
        "system": {"uptime_seconds": uptime, "boot_id": boot_id},
        "agent_version": "10.13.0",
    }


def one_iface(rx: int, tx: int, uptime: int, **kwargs) -> dict:
    speed = kwargs.pop("speed_mbps", LINK_MBPS)
    return node_metrics([iface(IFACE, rx, tx, speed_mbps=speed)], uptime, **kwargs)


def buckets(ingest: TrafficIngest, scope: str, period: str = "hour", scope_key=None) -> dict:
    return {
        key[4]: value
        for key, value in ingest._accum.items()
        if key[1] == period and key[2] == scope and (scope_key is None or key[3] == scope_key)
    }


class FirstObservationTests(unittest.TestCase):
    def test_no_delta_only_baseline(self):
        ingest = TrafficIngest()
        speeds = ingest.observe(
            SERVER_ID, one_iface(1_000, 2_000, uptime=3600), epoch_of(naive(3, 1, 12)),
        )

        self.assertEqual(speeds, ObservedSpeeds())
        self.assertEqual(ingest._accum, {})

        baseline = ingest._counters[(SERVER_ID, "total", "")]
        self.assertEqual((baseline.rx_value, baseline.tx_value), (1_000, 2_000))
        self.assertEqual(baseline.observed_at, naive(3, 1, 12))
        self.assertEqual(baseline.boot_at, naive(3, 1, 11))
        self.assertIn((SERVER_ID, "iface", IFACE), ingest._counters)


class SingleHourStepTests(unittest.TestCase):
    def test_delta_equals_difference_in_one_bucket(self):
        ingest = TrafficIngest()
        ingest.observe(SERVER_ID, one_iface(1_000, 2_000, uptime=3600), epoch_of(naive(3, 1, 12)))
        speeds = ingest.observe(
            SERVER_ID,
            one_iface(601_000, 302_000, uptime=3630),
            epoch_of(naive(3, 1, 12, 0, 30)),
        )

        hourly = buckets(ingest, "total")
        self.assertEqual(list(hourly), [naive(3, 1, 12)])
        slot = hourly[naive(3, 1, 12)]
        self.assertEqual((slot.rx_bytes, slot.tx_bytes), (600_000, 300_000))
        self.assertAlmostEqual(slot.covered_seconds, 30.0)

        daily = buckets(ingest, "total", period="day")
        self.assertEqual(list(daily), [naive(3, 1, 0)])
        self.assertEqual(daily[naive(3, 1, 0)].rx_bytes, 600_000)

        self.assertAlmostEqual(speeds.rx_bytes_per_sec, 20_000.0)
        self.assertAlmostEqual(speeds.tx_bytes_per_sec, 10_000.0)

    def test_virtual_interfaces_stay_out_of_total(self):
        ingest = TrafficIngest()
        before = [
            iface(IFACE, 1_000, 1_000),
            iface("docker0", 500, 500, speed_mbps=None, is_virtual=True),
            iface("veth7f2a", 300, 300, speed_mbps=None),
            iface("br-abc123", 200, 200, speed_mbps=None),
        ]
        after = [
            iface(IFACE, 61_000, 31_000),
            iface("docker0", 40_500, 20_500, speed_mbps=None, is_virtual=True),
            iface("veth7f2a", 30_300, 15_300, speed_mbps=None),
            iface("br-abc123", 20_200, 10_200, speed_mbps=None),
        ]
        ingest.observe(SERVER_ID, node_metrics(before, uptime=3600), epoch_of(naive(3, 1, 12)))
        ingest.observe(
            SERVER_ID, node_metrics(after, uptime=3630), epoch_of(naive(3, 1, 12, 0, 30)),
        )

        self.assertEqual(buckets(ingest, "total")[naive(3, 1, 12)].rx_bytes, 60_000)
        self.assertEqual(
            {key[3] for key in ingest._accum if key[2] == "iface"}, {IFACE},
        )


class HourBoundaryTests(unittest.TestCase):
    """Раскладка по границе часа — место, где легче всего потерять байты на делении."""

    def test_parts_sum_exactly_to_delta(self):
        ingest = TrafficIngest()
        rx_delta, tx_delta = 1_000_003, 7_777_777
        ingest.observe(
            SERVER_ID, one_iface(10, 20, uptime=7_200), epoch_of(naive(3, 1, 12, 59, 37)),
        )
        ingest.observe(
            SERVER_ID,
            one_iface(10 + rx_delta, 20 + tx_delta, uptime=7_474),
            epoch_of(naive(3, 1, 13, 4, 11)),
        )

        hourly = buckets(ingest, "total")
        self.assertEqual(sorted(hourly), [naive(3, 1, 12), naive(3, 1, 13)])
        self.assertEqual(sum(slot.rx_bytes for slot in hourly.values()), rx_delta)
        self.assertEqual(sum(slot.tx_bytes for slot in hourly.values()), tx_delta)
        self.assertTrue(all(slot.rx_bytes < rx_delta for slot in hourly.values()))
        self.assertAlmostEqual(sum(slot.covered_seconds for slot in hourly.values()), 274.0)

        daily = buckets(ingest, "total", period="day")
        self.assertEqual(daily[naive(3, 1, 0)].rx_bytes, rx_delta)

    def test_split_delta_conserves_every_byte(self):
        cases = [
            (naive(3, 1, 12, 0), naive(3, 1, 12, 0, 10), 1, 0),
            (naive(3, 1, 12, 59, 59), naive(3, 1, 13, 0, 1), 999_983, 7),
            (naive(3, 1, 12, 30), naive(3, 1, 13, 30), 123_456_789, 987_654_321),
            (naive(3, 1, 23, 17, 3), naive(3, 2, 2, 41, 59), 1_000_000_007, 3),
            (naive(3, 1, 0, 0), naive(3, 3, 0, 0), 8_675_309, 42),
        ]
        for prev_at, now_at, rx, tx in cases:
            with self.subTest(prev_at=prev_at, now_at=now_at):
                parts = split_delta(prev_at, now_at, rx, tx)
                self.assertEqual(sum(part[1] for part in parts), rx)
                self.assertEqual(sum(part[2] for part in parts), tx)
                self.assertAlmostEqual(
                    sum(part[3] for part in parts), (now_at - prev_at).total_seconds(),
                )
                self.assertEqual([part[0] for part in parts], sorted({p[0] for p in parts}))
                self.assertTrue(all(part[0] == floor_hour(part[0]) for part in parts))

    def test_non_positive_span_lands_in_current_hour(self):
        parts = split_delta(naive(3, 1, 12, 30), naive(3, 1, 12, 30), 500, 600)
        self.assertEqual(parts, [(naive(3, 1, 12), 500, 600, 0.0)])


class GapTests(unittest.TestCase):
    def test_three_hour_gap_spreads_proportionally(self):
        ingest = TrafficIngest()
        gap_seconds = 11_640                       # 10:17 → 13:31
        rx_delta = 999_999_991
        ingest.observe(
            SERVER_ID, one_iface(1_000, 1_000, uptime=100_000), epoch_of(naive(3, 1, 10, 17)),
        )
        ingest.observe(
            SERVER_ID,
            one_iface(1_000 + rx_delta, 1_000, uptime=100_000 + gap_seconds),
            epoch_of(naive(3, 1, 13, 31)),
        )

        hourly = buckets(ingest, "total")
        self.assertEqual(
            sorted(hourly),
            [naive(3, 1, 10), naive(3, 1, 11), naive(3, 1, 12), naive(3, 1, 13)],
        )
        self.assertEqual(sum(slot.rx_bytes for slot in hourly.values()), rx_delta)
        self.assertTrue(all(slot.rx_bytes < rx_delta for slot in hourly.values()))
        self.assertEqual(
            [round(hourly[bucket].covered_seconds) for bucket in sorted(hourly)],
            [2_580, 3_600, 3_600, 1_860],
        )
        self.assertEqual(
            hourly[naive(3, 1, 11)].rx_bytes, rx_delta * 3_600 // gap_seconds,
        )

    def test_reboot_leaves_the_gap_before_boot_empty(self):
        ingest = TrafficIngest()
        ingest.observe(
            SERVER_ID,
            one_iface(5_000_000_000, 1_000_000_000, uptime=100_000, boot_id="old-boot"),
            epoch_of(naive(3, 1, 10)),
        )
        speeds = ingest.observe(
            SERVER_ID,
            one_iface(300_000_000, 60_000_000, uptime=1_800, boot_id="new-boot"),
            epoch_of(naive(3, 1, 13, 30)),
        )

        hourly = buckets(ingest, "total")
        self.assertEqual(list(hourly), [naive(3, 1, 13)])
        self.assertEqual(hourly[naive(3, 1, 13)].rx_bytes, 300_000_000)
        self.assertEqual(hourly[naive(3, 1, 13)].tx_bytes, 60_000_000)
        self.assertAlmostEqual(hourly[naive(3, 1, 13)].covered_seconds, 1_800.0)
        self.assertEqual(speeds, ObservedSpeeds())

    def test_gap_beyond_backfill_window_is_clamped(self):
        ingest = TrafficIngest()
        started = naive(3, 1, 12)
        finished = started + timedelta(days=40)
        rx_delta = 3_024_000_000
        ingest.observe(
            SERVER_ID, one_iface(0, 0, uptime=4_320_000), epoch_of(started),
        )
        ingest.observe(
            SERVER_ID,
            one_iface(rx_delta, 0, uptime=4_320_000 + 40 * 86_400),
            epoch_of(finished),
        )

        hourly = buckets(ingest, "total")
        earliest = finished - timedelta(days=MAX_BACKFILL_DAYS)
        self.assertEqual(min(hourly), floor_hour(earliest))
        self.assertEqual(sum(slot.rx_bytes for slot in hourly.values()), rx_delta)
        self.assertAlmostEqual(
            sum(slot.covered_seconds for slot in hourly.values()),
            MAX_BACKFILL_DAYS * 86_400,
        )

        daily = buckets(ingest, "total", period="day")
        self.assertEqual(min(daily), floor_day(earliest))
        self.assertEqual(sum(slot.rx_bytes for slot in daily.values()), rx_delta)

    def test_clamp_backfill_start_keeps_recent_starts(self):
        now = naive(4, 10, 12)
        self.assertEqual(clamp_backfill_start(now - timedelta(days=1), now), now - timedelta(days=1))
        self.assertEqual(
            clamp_backfill_start(now - timedelta(days=90), now),
            now - timedelta(days=MAX_BACKFILL_DAYS),
        )


class CounterResetTests(unittest.TestCase):
    def test_uptime_reset_rebases_without_crediting(self):
        """Сброс по аптайму — эвристика, и начислять по ней кумулятив нельзя.

        boot_at считается из часов панели минус аптайм ноды, поэтому шаг часов на любой
        стороне выглядит как ребут. Начисление всего счётчика на окно [boot_at, now]
        закинуло бы в историю терабайты, и проверка скорости этого не поймала бы —
        окно растянуто, подразумеваемая скорость низкая. Цена правильного поведения —
        один потерянный интервал.
        """
        ingest = TrafficIngest()
        ingest.observe(
            SERVER_ID, one_iface(9_000_000_000, 1_000, uptime=100_000), epoch_of(naive(3, 1, 12)),
        )
        ingest.observe(
            SERVER_ID,
            one_iface(40_000_000, 1_000_000, uptime=20),
            epoch_of(naive(3, 1, 12, 0, 30)),
        )

        self.assertEqual(buckets(ingest, "total"), {})
        rebased = ingest._counters[(SERVER_ID, "total", "")]
        self.assertEqual((rebased.rx_value, rebased.tx_value), (40_000_000, 1_000_000))

    def test_boot_id_change_credits_counter_from_boot(self):
        """Смена boot_id — единственный ТОЧНЫЙ признак ребута, только по нему начисляем."""
        ingest = TrafficIngest()
        ingest.observe(
            SERVER_ID,
            one_iface(9_000_000_000, 1_000, uptime=100_000, boot_id="aaa"),
            epoch_of(naive(3, 1, 12)),
        )
        ingest.observe(
            SERVER_ID,
            one_iface(40_000_000, 1_000_000, uptime=1_800, boot_id="bbb"),
            epoch_of(naive(3, 1, 12, 0, 30)),
        )

        hourly = buckets(ingest, "total")
        self.assertTrue(hourly, "ребут с точным boot_id обязан попасть в историю")
        self.assertEqual(sum(slot.rx_bytes for slot in hourly.values()), 40_000_000)

    def test_negative_delta_rebases_everything(self):
        """Счётчик поехал назад — момент обнуления неизвестен, начислять нечего."""
        ingest = TrafficIngest()
        ingest.observe(
            SERVER_ID,
            one_iface(5_000_000, 5_000_000, uptime=3_600,
                      ports=[{"port": 443, "rx_bytes": 4_000_000, "tx_bytes": 4_000_000}],
                      sampled_at=1_000.0),
            epoch_of(naive(3, 1, 12)),
        )
        ingest.observe(
            SERVER_ID,
            one_iface(1_000_000, 900_000, uptime=3_630,
                      ports=[{"port": 443, "rx_bytes": 500_000, "tx_bytes": 400_000}],
                      sampled_at=1_030.0),
            epoch_of(naive(3, 1, 12, 0, 30)),
        )

        self.assertEqual(buckets(ingest, "iface", scope_key=IFACE), {})
        self.assertEqual(buckets(ingest, "port"), {})
        for key, expected in (
            ((SERVER_ID, "iface", IFACE), (1_000_000, 900_000)),
            ((SERVER_ID, "port", "443"), (500_000, 400_000)),
        ):
            rebased = ingest._counters[key]
            self.assertEqual((rebased.rx_value, rebased.tx_value), expected)

    def test_detect_reset_precedence(self):
        prev = CounterState(
            rx_value=1_000, tx_value=1_000,
            observed_at=naive(3, 1, 12), boot_id="old-boot", boot_at=naive(3, 1, 11),
        )
        self.assertIs(detect_reset(None, "any", naive(3, 1, 11), 0, 0), ResetKind.NONE)
        self.assertIs(
            detect_reset(prev, "new-boot", naive(3, 1, 11), 5_000, 5_000), ResetKind.BOOT_ID,
        )
        self.assertIs(
            detect_reset(prev, "old-boot", naive(3, 1, 11, 5), 5_000, 5_000), ResetKind.BOOT_TIME,
        )
        self.assertIs(
            detect_reset(prev, "old-boot", naive(3, 1, 11, 1, 30), 5_000, 5_000), ResetKind.NONE,
        )
        self.assertIs(
            detect_reset(prev, "old-boot", naive(3, 1, 11), 999, 5_000), ResetKind.NEGATIVE,
        )
        self.assertIs(
            detect_reset(prev, "old-boot", naive(3, 1, 11), 5_000, 5_000), ResetKind.NONE,
        )


class RateGuardTests(unittest.TestCase):
    def test_delta_above_link_capacity_is_discarded(self):
        ingest = TrafficIngest()
        ingest.observe(
            SERVER_ID, one_iface(0, 0, uptime=3_600, speed_mbps=100), epoch_of(naive(3, 1, 12)),
        )
        speeds = ingest.observe(
            SERVER_ID,
            one_iface(200_000_000, 0, uptime=3_610, speed_mbps=100),
            epoch_of(naive(3, 1, 12, 0, 10)),
        )

        self.assertEqual(ingest._accum, {})
        self.assertEqual(speeds, ObservedSpeeds())
        self.assertEqual(ingest._counters[(SERVER_ID, "total", "")].rx_value, 200_000_000)

    def test_rate_is_sane_boundary(self):
        limit = int(100 * MBIT_IN_BYTES_PER_SEC * SPEED_TOLERANCE)
        self.assertTrue(rate_is_sane(limit, 0, 1.0, speed_mbps=100))
        self.assertFalse(rate_is_sane(limit + 1, 0, 1.0, speed_mbps=100))

    def test_unknown_link_falls_back_to_hundred_gigabit(self):
        self.assertTrue(rate_is_sane(UNKNOWN_LINK_BYTES_PER_SEC, 0, 1.0, speed_mbps=None))
        self.assertFalse(rate_is_sane(UNKNOWN_LINK_BYTES_PER_SEC + 1, 0, 1.0, speed_mbps=None))
        self.assertTrue(rate_is_sane(0, 0, 0.0, speed_mbps=None))


if __name__ == "__main__":
    unittest.main()
