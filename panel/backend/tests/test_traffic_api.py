"""Чистые функции роутера истории трафика.

Проверяются места, где ошибка не видна глазом: плотность ряда (лишняя или недостающая
точка сдвигает весь график), разметка разрывов и разбор списка портов из JSON-колонки.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # локальный прогон без установленного рантайма панели
    from app.routers.traffic import (  # noqa: E402
        SERIES_PERIODS,
        _as_port,
        _dense_points,
        _summary_since,
        _tracked_ports,
    )
    from app.services.traffic_ingest import Period, floor_day  # noqa: E402
except ImportError as e:  # pragma: no cover
    raise unittest.SkipTest(f"traffic router requires the panel runtime: {e}")


def bucket(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 3, day, hour)


class DensePointsTests(unittest.TestCase):
    def test_every_bucket_present_even_without_data(self):
        points = _dense_points(bucket(1), timedelta(hours=1), 24, {})

        self.assertEqual(len(points), 24)
        self.assertTrue(all(p["rx"] is None and p["tx"] is None for p in points))

    def test_measured_buckets_keep_their_values(self):
        measured = {bucket(1, 3): (100, 20), bucket(1, 5): (0, 0)}
        points = _dense_points(bucket(1), timedelta(hours=1), 6, measured)

        by_hour = {p["timestamp"]: (p["rx"], p["tx"]) for p in points}
        self.assertEqual(len(points), 6)
        self.assertIn((100, 20), by_hour.values())
        # Нулевой, но наблюдённый бакет — это тишина, а не разрыв: если отдать его как
        # null, простаивающий порт нарисуется так же, как недоступная нода.
        self.assertIn((0, 0), by_hour.values())
        self.assertEqual(sum(1 for rx, _ in by_hour.values() if rx is None), 4)

    def test_points_are_ordered_and_evenly_spaced(self):
        points = _dense_points(bucket(1), timedelta(days=1), 5, {})
        stamps = [p["timestamp"] for p in points]

        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), len(stamps))

    def test_period_table_matches_declared_point_counts(self):
        for period, (period_type, step, count) in SERIES_PERIODS.items():
            with self.subTest(period=period):
                points = _dense_points(bucket(1), step, count, {})
                self.assertEqual(len(points), count)
                expected_step = timedelta(hours=1) if period_type is Period.HOUR else timedelta(days=1)
                self.assertEqual(step, expected_step)


class SummaryWindowTests(unittest.TestCase):
    def test_window_covers_exactly_requested_days_including_today(self):
        for days in (1, 7, 30, 365):
            with self.subTest(days=days):
                since = _summary_since(days)
                today = floor_day(datetime.now(timezone.utc).replace(tzinfo=None))
                self.assertEqual((today - since).days, days - 1)


class TrackedPortsTests(unittest.TestCase):
    def test_valid_json_list(self):
        self.assertEqual(_tracked_ports("[80, 443, 8443]"), [80, 443, 8443])

    def test_garbage_never_raises(self):
        for raw in (None, "", "not json", '{"port": 80}', "[]", '["80"]', "[0, -1, 70000]"):
            with self.subTest(raw=raw):
                self.assertEqual(_tracked_ports(raw), [])

    def test_mixed_list_keeps_only_valid_ports(self):
        self.assertEqual(_tracked_ports('[80, "x", 0, 443, 99999]'), [80, 443])


class ScopeKeyTests(unittest.TestCase):
    def test_port_scope_key_roundtrip(self):
        self.assertEqual(_as_port("443"), 443)

    def test_non_numeric_scope_key_is_not_a_port(self):
        self.assertIsNone(_as_port("eth0"))
        self.assertIsNone(_as_port(""))


if __name__ == "__main__":
    unittest.main()
