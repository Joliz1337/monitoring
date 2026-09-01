"""Чистые функции сводной истории парка.

Проверяется то, что не видно на графике: ёмкость ноды разбирается из чего
угодно и не роняет запрос, точка сводки всегда одной формы, занятые байты
считаются из процента и суммы объёмов, а одинаковые простои панели (строка на
каждую ноду) схлопываются в один разрыв.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.fleet_history import (  # noqa: E402
    CACHE_TTL_SEC,
    DEFAULT_CORES,
    FLEET_METRIC_KEYS,
    FLEET_PERIODS,
    build_capacity,
    empty_fleet_point,
    fleet_point,
    node_capacity,
)
from app.services.metrics_history import insert_gap_markers, merge_downtime  # noqa: E402


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 1, hour, minute)


def row(bucket: datetime, **values) -> dict:
    base = {"bucket": bucket, "servers": 1}
    base.update({key: None for key in FLEET_METRIC_KEYS if key != "memory_used"})
    base.update(values)
    return base


class NodeCapacityTests(unittest.TestCase):
    def test_reads_cores_and_ram(self):
        metrics = '{"cpu": {"cores_logical": 8}, "memory": {"ram": {"total": 16777216}}}'
        self.assertEqual(node_capacity(metrics), (8.0, 16777216.0))

    def test_missing_metrics_fall_back_to_minimal_weight(self):
        for value in (None, "", "not json", "[]", "null", "{}"):
            self.assertEqual(node_capacity(value), (DEFAULT_CORES, 0.0), value)

    def test_partial_metrics_keep_what_is_readable(self):
        self.assertEqual(node_capacity('{"cpu": {"cores_logical": 4}}'), (4.0, 0.0))
        self.assertEqual(
            node_capacity('{"memory": {"ram": {"total": 2048}}}'), (DEFAULT_CORES, 2048.0)
        )

    def test_nonpositive_and_nonnumeric_values_are_rejected(self):
        self.assertEqual(node_capacity('{"cpu": {"cores_logical": 0}}'), (DEFAULT_CORES, 0.0))
        self.assertEqual(node_capacity('{"cpu": {"cores_logical": -2}}'), (DEFAULT_CORES, 0.0))
        self.assertEqual(node_capacity('{"cpu": {"cores_logical": "8"}}'), (DEFAULT_CORES, 0.0))
        self.assertEqual(node_capacity('{"cpu": {"cores_logical": true}}'), (DEFAULT_CORES, 0.0))

    def test_wrong_shapes_do_not_raise(self):
        self.assertEqual(node_capacity('{"cpu": 8, "memory": []}'), (DEFAULT_CORES, 0.0))
        self.assertEqual(node_capacity('{"memory": {"ram": 16}}'), (DEFAULT_CORES, 0.0))


class BuildCapacityTests(unittest.TestCase):
    def test_arrays_stay_aligned_with_ids(self):
        capacity = build_capacity([
            (7, '{"cpu": {"cores_logical": 2}, "memory": {"ram": {"total": 100}}}'),
            (3, None),
        ])
        self.assertEqual(capacity.ids, [7, 3])
        self.assertEqual(capacity.cores, [2.0, DEFAULT_CORES])
        self.assertEqual(capacity.ram, [100.0, 0.0])

    def test_empty_fleet_is_falsy(self):
        self.assertFalse(build_capacity([]))
        self.assertTrue(build_capacity([(1, None)]))


class FleetPointTests(unittest.TestCase):
    def test_every_point_has_the_same_keys(self):
        marker = empty_fleet_point(at(12))
        point = fleet_point(row(at(12), cpu_usage=40.0))
        self.assertEqual(set(marker), set(point))
        self.assertEqual(set(marker), {"timestamp", "servers", *FLEET_METRIC_KEYS})

    def test_marker_carries_no_values(self):
        marker = empty_fleet_point(at(12))
        self.assertEqual(marker["servers"], 0)
        self.assertTrue(all(marker[key] is None for key in FLEET_METRIC_KEYS))

    def test_used_bytes_come_from_percent_and_total(self):
        point = fleet_point(row(at(12), memory_percent=25.0, memory_total=8000.0, servers=4))
        self.assertEqual(point["memory_used"], 2000.0)
        self.assertEqual(point["servers"], 4)

    def test_used_bytes_stay_null_without_percent_or_total(self):
        self.assertIsNone(fleet_point(row(at(12), memory_total=8000.0))["memory_used"])
        self.assertIsNone(fleet_point(row(at(12), memory_percent=25.0))["memory_used"])


class PeriodTests(unittest.TestCase):
    def test_every_period_is_bucketed(self):
        # Снапшоты разных нод не выровнены по времени: сборка окна опирается на
        # bucket_sec и упала бы на None.
        for period, spec in FLEET_PERIODS.items():
            self.assertIsNotNone(spec.bucket_sec, period)
            self.assertGreater(spec.bucket_sec, 0, period)

    def test_every_period_has_a_cache_ttl(self):
        self.assertEqual(set(CACHE_TTL_SEC), set(FLEET_PERIODS))


class PanelDowntimeTests(unittest.TestCase):
    def test_same_gap_recorded_per_node_collapses_into_one(self):
        rows = [(at(2), at(3))] * 5
        self.assertEqual(merge_downtime(rows, at(0), at(6)), [(at(2), at(3))])

    def test_marker_of_fleet_shape_breaks_the_line(self):
        points = [fleet_point(row(at(1), cpu_usage=10.0)), fleet_point(row(at(5), cpu_usage=20.0))]
        gaps = [(at(2), at(4))]
        data = insert_gap_markers(points, gaps, timedelta(minutes=5).total_seconds(), empty_fleet_point)
        self.assertEqual(len(data), 3)
        self.assertEqual(set(data[1]), set(points[0]))
        self.assertIsNone(data[1]["cpu_usage"])

    def test_short_gap_leaves_the_line_alone(self):
        points = [fleet_point(row(at(1), cpu_usage=10.0))]
        gaps = [(at(2), at(2, 1))]
        data = insert_gap_markers(points, gaps, timedelta(minutes=5).total_seconds(), empty_fleet_point)
        self.assertEqual(data, points)


if __name__ == "__main__":
    unittest.main()
