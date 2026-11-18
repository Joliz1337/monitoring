"""Чистые функции истории метрик.

Проверяется то, что не видно глазом на графике: сетка бакетов совпадает с
date_bin от epoch, разрывы вставляются только в долгие простои, матрица ядер
не сдвигается при пропущенном бакете, а точка любого источника отдаёт один и
тот же набор полей.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import metrics_history  # noqa: E402
from app.services.metrics_history import (  # noqa: E402
    GAP_MIN_SEC,
    MANY_CORES,
    PER_CPU_BUCKET,
    POINT_METRIC_KEYS,
    SERIES_PERIODS,
    DataSource,
    aggregated_point,
    align_down,
    bucket_grid,
    choose_bucket,
    empty_point,
    insert_gap_markers,
    merge_downtime,
    per_cpu_matrix,
    raw_point,
)


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 8, 26, hour, minute, second)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


RAW_ROW = {
    "timestamp": at(12, 0, 10),
    "cpu_usage": 12.5, "cpu_usage_max": 41.0, "cpu_steal": 3.0,
    "memory_percent": 55.0, "memory_used": 4_000, "memory_available": 3_000,
    "load_avg_1": 0.8,
    "net_rx_bytes_per_sec": 1_000.0, "net_rx_bytes_per_sec_max": 9_000.0,
    "net_tx_bytes_per_sec": 200.0, "net_tx_bytes_per_sec_max": None,
    "disk_percent": 70.0, "disk_read_bytes_per_sec": 0.0, "disk_write_bytes_per_sec": 5.0,
    "process_count": 120,
    "tcp_established": 10, "tcp_listen": 5, "tcp_time_wait": 3, "tcp_close_wait": 0,
    "tcp_syn_sent": 0, "tcp_syn_recv": 0, "tcp_fin_wait": 1,
}

HOURLY_ROW = {
    "bucket": at(12), "data_points": 360,
    "cpu_usage": 10.0, "max_cpu": 50.0, "cpu_steal": 2.0,
    "memory_percent": 50.0, "max_memory_percent": 52.0,
    "load_avg_1": 0.5, "max_load": 1.5,
    "net_rx_bytes_per_sec": 100.0, "max_net_rx_bytes_per_sec": 900.0,
    "net_tx_bytes_per_sec": 20.0, "max_net_tx_bytes_per_sec": 90.0,
    "disk_percent": 70.0, "disk_read_bytes_per_sec": 0.0, "disk_write_bytes_per_sec": 5.0,
    "tcp_established": 10.5, "tcp_listen": 5.0, "tcp_time_wait": 3.0, "tcp_close_wait": 0.0,
    "tcp_syn_sent": 0.0, "tcp_syn_recv": 0.0, "tcp_fin_wait": 1.0,
}


class PeriodTableTests(unittest.TestCase):
    def test_every_period_has_source_and_span(self):
        self.assertEqual(SERIES_PERIODS["1h"].source, DataSource.RAW)
        self.assertIsNone(SERIES_PERIODS["1h"].bucket_sec)
        self.assertEqual(SERIES_PERIODS["24h"].bucket_sec, 300)
        # 30 дней часовых — 720 точек; суточных было бы 30
        self.assertEqual(SERIES_PERIODS["30d"].source, DataSource.HOUR)
        self.assertEqual(SERIES_PERIODS["365d"].source, DataSource.DAY)

    def test_choose_bucket_doubles_for_many_cores(self):
        self.assertEqual(choose_bucket("1h", 8), PER_CPU_BUCKET["1h"])
        self.assertEqual(choose_bucket("1h", MANY_CORES), PER_CPU_BUCKET["1h"])
        self.assertEqual(choose_bucket("24h", MANY_CORES + 1), PER_CPU_BUCKET["24h"] * 2)

    def test_choose_bucket_rejects_periods_without_per_cpu(self):
        with self.assertRaises(KeyError):
            choose_bucket("7d", 4)


class BucketGridTests(unittest.TestCase):
    def test_align_down_follows_epoch_grid(self):
        self.assertEqual(align_down(at(12, 7, 43), 300), at(12, 5))
        self.assertEqual(align_down(at(12, 5), 300), at(12, 5))
        self.assertEqual(align_down(at(12, 0, 29), 30), at(12, 0))

    def test_align_down_ignores_microseconds(self):
        self.assertEqual(align_down(at(12, 4, 59).replace(microsecond=999_999), 300), at(12))

    def test_grid_covers_both_window_edges(self):
        grid = bucket_grid(at(12, 0, 10), at(13, 0, 10), 300)

        self.assertEqual(grid[0], at(12))
        self.assertEqual(grid[-1], at(13))
        self.assertEqual(len(grid), 13)

    def test_grid_is_evenly_spaced(self):
        grid = bucket_grid(at(12), at(12, 59, 59), 30)
        steps = {b - a for a, b in zip(grid, grid[1:])}

        self.assertEqual(steps, {timedelta(seconds=30)})
        self.assertEqual(len(grid), 120)


class MergeDowntimeTests(unittest.TestCase):
    def test_node_and_panel_downtime_merge_into_one_gap(self):
        rows = [(at(12, 10), at(12, 20)), (at(12, 15), at(12, 30))]

        self.assertEqual(merge_downtime(rows, at(12), at(13)), [(at(12, 10), at(12, 30))])

    def test_gaps_are_clipped_to_window(self):
        rows = [(at(11, 50), at(12, 5)), (at(12, 50), None)]

        self.assertEqual(
            merge_downtime(rows, at(12), at(13)),
            [(at(12), at(12, 5)), (at(12, 50), at(13))],
        )

    def test_downtime_outside_window_is_dropped(self):
        self.assertEqual(merge_downtime([(at(10), at(11))], at(12), at(13)), [])

    def test_unsorted_rows_still_merge(self):
        rows = [(at(12, 15), at(12, 30)), (at(12, 10), at(12, 20))]

        self.assertEqual(merge_downtime(rows, at(12), at(13)), [(at(12, 10), at(12, 30))])


class GapMarkerTests(unittest.TestCase):
    def setUp(self):
        self.points = [
            raw_point({**RAW_ROW, "timestamp": at(12, 0, 0)}),
            raw_point({**RAW_ROW, "timestamp": at(12, 10, 0)}),
        ]

    def test_short_downtime_leaves_points_untouched(self):
        short = [(at(12, 1), at(12, 1, GAP_MIN_SEC))]

        self.assertEqual(insert_gap_markers(self.points, short, GAP_MIN_SEC), self.points)

    def test_long_downtime_gets_null_marker_in_the_middle(self):
        data = insert_gap_markers(self.points, [(at(12, 2), at(12, 8))], GAP_MIN_SEC)

        self.assertEqual(len(data), 3)
        marker = data[1]
        self.assertEqual(marker["timestamp"], iso(at(12, 5)))
        self.assertEqual(marker["data_points"], 0)
        self.assertTrue(all(marker[key] is None for key in POINT_METRIC_KEYS))

    def test_marker_inside_gap_at_window_edge_lands_after_last_point(self):
        data = insert_gap_markers(self.points, [(at(12, 10, 5), at(13))], GAP_MIN_SEC)

        self.assertEqual([p["data_points"] for p in data], [1, 1, 0])

    def test_threshold_is_exclusive(self):
        exactly = [(at(12, 2), at(12, 2, GAP_MIN_SEC))]
        longer = [(at(12, 2), at(12, 2, GAP_MIN_SEC + 1))]

        self.assertEqual(len(insert_gap_markers(self.points, exactly, GAP_MIN_SEC)), 2)
        self.assertEqual(len(insert_gap_markers(self.points, longer, GAP_MIN_SEC)), 3)


class PerCpuMatrixTests(unittest.TestCase):
    def setUp(self):
        self.grid = bucket_grid(at(12), at(12, 1, 30), 30)   # 12:00:00, :30, 13:00, :30

    def test_missing_bucket_becomes_null_without_shifting_columns(self):
        rows = [
            (at(12, 0, 0), 0, 10.0), (at(12, 0, 0), 1, 20.0),
            (at(12, 1, 0), 0, 30.0), (at(12, 1, 0), 1, 40.0),
        ]

        self.assertEqual(
            per_cpu_matrix(rows, self.grid),
            [[10.0, None, 30.0, None], [20.0, None, 40.0, None]],
        )

    def test_core_count_change_inside_window_pads_with_null(self):
        rows = [(at(12, 0, 0), 0, 1.0), (at(12, 0, 30), 0, 2.0), (at(12, 0, 30), 1, 3.0)]

        self.assertEqual(per_cpu_matrix(rows, self.grid), [[1.0, 2.0, None, None], [None, 3.0, None, None]])

    def test_values_are_rounded_and_buckets_off_grid_are_ignored(self):
        rows = [(at(12, 0, 0), 0, 12.3456), (at(11, 0, 0), 0, 99.0)]

        self.assertEqual(per_cpu_matrix(rows, self.grid), [[12.3, None, None, None]])

    def test_no_rows_means_no_cores(self):
        self.assertEqual(per_cpu_matrix([], self.grid), [])


class PointSchemaTests(unittest.TestCase):
    def test_all_sources_share_one_key_set(self):
        keys = {frozenset(raw_point(RAW_ROW)), frozenset(aggregated_point(HOURLY_ROW)), frozenset(empty_point(at(12)))}

        self.assertEqual(len(keys), 1)
        self.assertEqual(keys.pop(), frozenset(POINT_METRIC_KEYS) | {"timestamp", "data_points"})

    def test_raw_point_uses_window_peaks_and_falls_back_to_average(self):
        point = raw_point(RAW_ROW)

        self.assertEqual(point["data_points"], 1)
        self.assertEqual(point["max_cpu"], 41.0)
        self.assertEqual(point["max_net_rx_bytes_per_sec"], 9_000.0)
        # Старая нода не присылает пик — полоса нулевой высоты, а не дыра
        self.assertEqual(point["max_net_tx_bytes_per_sec"], 200.0)
        # У одиночного замера пиков памяти и load нет
        self.assertIsNone(point["max_memory_percent"])
        self.assertIsNone(point["max_load"])

    def test_aggregated_point_fills_missing_columns_with_null(self):
        point = aggregated_point(HOURLY_ROW)

        self.assertEqual(point["data_points"], 360)
        self.assertEqual(point["max_load"], 1.5)
        self.assertIsNone(point["memory_used"])
        self.assertIsNone(point["process_count"])

    def test_timestamps_are_iso_utc_with_z(self):
        aware = at(12).replace(tzinfo=timezone.utc)

        self.assertEqual(raw_point(RAW_ROW)["timestamp"], iso(at(12, 0, 10)))
        self.assertEqual(empty_point(aware)["timestamp"], iso(at(12)))
        self.assertEqual(aggregated_point({**HOURLY_ROW, "bucket": aware})["timestamp"], iso(at(12)))

    def test_empty_point_has_no_data(self):
        point = empty_point(at(12))

        self.assertEqual(point["data_points"], 0)
        self.assertTrue(all(point[key] is None for key in POINT_METRIC_KEYS))


class SilentServerHistoryTests(unittest.IsolatedAsyncioTestCase):
    """Окно периода у сервера, который перестал отвечать.

    Окно «последний час от сейчас» у ноды, лежащей третий день, пустое, хотя
    её история в базе есть: страница сервера показывала «Нет данных».
    """

    NOW = datetime(2026, 8, 26, 15, 0, 0)
    DIED = datetime(2026, 8, 23, 9, 30, 0)

    async def asyncSetUp(self) -> None:
        self.points_by_source: dict[DataSource, list[dict]] = {}
        self.windows: list[tuple[DataSource, datetime, datetime]] = []
        self.per_cpu = mock.AsyncMock(return_value={"cores": []})
        for name, replacement in (
            ("_utcnow", mock.Mock(return_value=self.NOW)),
            ("_load_points", self.fake_points),
            ("load_downtime", mock.AsyncMock(return_value=[])),
            ("load_collect_interval", mock.AsyncMock(return_value=10)),
            ("_load_per_cpu_block", self.per_cpu),
        ):
            patcher = mock.patch.object(metrics_history, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def fake_points(self, _db, _server_id, spec, start, end):
        self.windows.append((spec.source, start, end))
        return self.points_by_source.get(spec.source, [])

    def has_data(self, *sources: DataSource) -> None:
        for source in sources:
            self.points_by_source[source] = [empty_point(self.DIED - timedelta(hours=1))]

    async def history(self, period: str, frozen_at: datetime | None, per_cpu: bool = False) -> dict:
        return await metrics_history.load_history(None, 1, period, per_cpu, frozen_at)

    async def test_live_server_window_ends_now(self):
        response = await self.history("1h", None)

        self.assertEqual(self.windows, [(DataSource.RAW, self.NOW - timedelta(hours=1), self.NOW)])
        self.assertIsNone(response["frozen_at"])
        self.assertFalse(response["coarser_than_period"])

    async def test_live_server_with_empty_window_is_not_replaced_by_older_data(self):
        self.has_data(DataSource.HOUR)

        response = await self.history("1h", None)

        self.assertEqual(len(self.windows), 1)
        self.assertEqual(response["data_source"], "raw")
        self.assertEqual(response["data"], [])

    async def test_silent_server_window_ends_at_its_last_answer(self):
        self.has_data(DataSource.RAW)

        response = await self.history("1h", self.DIED)

        end = self.DIED + metrics_history.FROZEN_TAIL
        self.assertEqual(self.windows, [(DataSource.RAW, end - timedelta(hours=1), end)])
        self.assertEqual(response["frozen_at"], iso(self.DIED))
        self.assertEqual(response["to_time"], iso(end))
        self.assertEqual(response["count"], 1)
        self.assertFalse(response["coarser_than_period"])

    async def test_frozen_window_never_reaches_past_now(self):
        self.has_data(DataSource.RAW)

        await self.history("1h", self.NOW - timedelta(seconds=30))

        self.assertEqual(self.windows[0][2], self.NOW)

    async def test_last_answer_with_timezone_is_read_as_utc(self):
        self.has_data(DataSource.RAW)

        await self.history("1h", self.DIED.replace(tzinfo=timezone.utc))

        self.assertEqual(self.windows[0][2], self.DIED + metrics_history.FROZEN_TAIL)

    async def test_purged_snapshots_are_replaced_by_hourly_averages_of_the_last_day(self):
        self.has_data(DataSource.HOUR)

        response = await self.history("1h", self.DIED)

        end = self.DIED + metrics_history.FROZEN_TAIL
        self.assertEqual(
            [(source, end - start) for source, start, _ in self.windows],
            [(DataSource.RAW, timedelta(hours=1)), (DataSource.HOUR, timedelta(hours=24))],
        )
        self.assertEqual(response["data_source"], "hour")
        self.assertEqual(response["bucket_sec"], 3600)
        self.assertEqual(response["from_time"], iso(end - timedelta(hours=24)))
        self.assertTrue(response["coarser_than_period"])

    async def test_purged_hourly_averages_are_replaced_by_daily_ones(self):
        self.has_data(DataSource.DAY)

        for period in ("24h", "7d"):
            with self.subTest(period=period):
                response = await self.history(period, self.DIED)

                self.assertEqual(response["data_source"], "day")
                self.assertEqual(response["bucket_sec"], 86400)
                self.assertTrue(response["coarser_than_period"])

    async def test_server_without_any_history_keeps_the_requested_period(self):
        response = await self.history("24h", self.DIED)

        self.assertEqual(response["data_source"], "raw")
        self.assertEqual(response["bucket_sec"], 300)
        self.assertEqual(response["data"], [])
        self.assertFalse(response["coarser_than_period"])

    async def test_per_cpu_follows_the_frozen_window(self):
        self.has_data(DataSource.RAW)

        response = await self.history("1h", self.DIED, per_cpu=True)

        end = self.DIED + metrics_history.FROZEN_TAIL
        self.per_cpu.assert_awaited_once_with(None, 1, "1h", end - timedelta(hours=1), end)
        self.assertEqual(response["per_cpu"], {"cores": []})

    async def test_coarser_data_has_no_per_cpu(self):
        self.has_data(DataSource.HOUR)

        response = await self.history("1h", self.DIED, per_cpu=True)

        self.per_cpu.assert_not_awaited()
        self.assertIsNone(response["per_cpu"])


if __name__ == "__main__":
    unittest.main()
