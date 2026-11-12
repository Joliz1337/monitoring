"""Чистые функции сводной истории парка.

Проверяется то, что не видно на графике: ёмкость ноды разбирается из чего
угодно и не роняет запрос, точка сводки всегда одной формы, занятые байты
считаются из процента и суммы объёмов, а одинаковые простои панели (строка на
каждую ноду) схлопываются в один разрыв.
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.dialects import postgresql  # noqa: E402

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
from app.services import fleet_history  # noqa: E402
from app.services.latest_metrics import LatestMetrics  # noqa: E402
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
        metrics = {"cpu": {"cores_logical": 8}, "memory": {"ram": {"total": 16777216}}}
        self.assertEqual(node_capacity(metrics), (8.0, 16777216.0))

    def test_missing_metrics_fall_back_to_minimal_weight(self):
        for value in (None, {}):
            self.assertEqual(node_capacity(value), (DEFAULT_CORES, 0.0), value)

    def test_partial_metrics_keep_what_is_readable(self):
        self.assertEqual(node_capacity({"cpu": {"cores_logical": 4}}), (4.0, 0.0))
        self.assertEqual(
            node_capacity({"memory": {"ram": {"total": 2048}}}), (DEFAULT_CORES, 2048.0)
        )

    def test_nonpositive_and_nonnumeric_values_are_rejected(self):
        self.assertEqual(node_capacity({"cpu": {"cores_logical": 0}}), (DEFAULT_CORES, 0.0))
        self.assertEqual(node_capacity({"cpu": {"cores_logical": -2}}), (DEFAULT_CORES, 0.0))
        self.assertEqual(node_capacity({"cpu": {"cores_logical": "8"}}), (DEFAULT_CORES, 0.0))
        self.assertEqual(node_capacity({"cpu": {"cores_logical": True}}), (DEFAULT_CORES, 0.0))

    def test_wrong_shapes_do_not_raise(self):
        self.assertEqual(node_capacity({"cpu": 8, "memory": []}), (DEFAULT_CORES, 0.0))
        self.assertEqual(node_capacity({"memory": {"ram": 16}}), (DEFAULT_CORES, 0.0))


class BuildCapacityTests(unittest.TestCase):
    def test_arrays_stay_aligned_with_ids(self):
        capacity = build_capacity([
            (7, {"cpu": {"cores_logical": 2}, "memory": {"ram": {"total": 100}}}),
            (3, None),
        ])
        self.assertEqual(capacity.ids, [7, 3])
        self.assertEqual(capacity.cores, [2.0, DEFAULT_CORES])
        self.assertEqual(capacity.ram, [100.0, 0.0])

    def test_empty_fleet_is_falsy(self):
        self.assertFalse(build_capacity([]))
        self.assertTrue(build_capacity([(1, None)]))


class RecordingSession:
    """Сессия без базы: отдаёт id активных серверов и запоминает текст запросов."""

    def __init__(self, server_ids: list[int]) -> None:
        self._server_ids = server_ids
        self.queries: list[str] = []

    async def execute(self, statement):
        self.queries.append(str(statement.compile(dialect=postgresql.dialect())))
        return SimpleNamespace(
            all=lambda: [(server_id,) for server_id in self._server_ids],
            scalars=lambda: SimpleNamespace(all=lambda: list(self._server_ids)),
        )


class LoadCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.latest = LatestMetrics()
        store_patch = patch.object(fleet_history, "get_latest_metrics", return_value=self.latest)
        store_patch.start()
        self.addCleanup(store_patch.stop)

    async def test_capacity_comes_from_the_latest_answers_in_memory(self):
        self.latest.put(7, {"cpu": {"cores_logical": 2}, "memory": {"ram": {"total": 100}}})

        capacity = await fleet_history.load_capacity(RecordingSession([7, 3]), None)

        self.assertEqual(capacity.ids, [7, 3])
        self.assertEqual(capacity.cores, [2.0, DEFAULT_CORES])
        self.assertEqual(capacity.ram, [100.0, 0.0])

    async def test_saved_answers_are_not_read_from_the_database(self):
        db = RecordingSession([7])

        await fleet_history.load_capacity(db, "EU")

        self.assertFalse([query for query in db.queries if "last_metrics" in query])


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


class CacheTests(unittest.TestCase):
    def setUp(self):
        fleet_history._cache.clear()
        self.addCleanup(fleet_history._cache.clear)
        clock = patch.object(fleet_history.time, "monotonic", return_value=1000.0)
        self.monotonic = clock.start()
        self.addCleanup(clock.stop)

    def test_folder_and_whole_fleet_are_cached_apart(self):
        fleet_history._store(("24h", None), {"scope": "fleet"})
        fleet_history._store(("24h", "EU"), {"scope": "EU"})
        self.assertEqual(fleet_history._cached(("24h", None)), {"scope": "fleet"})
        self.assertEqual(fleet_history._cached(("24h", "EU")), {"scope": "EU"})
        self.assertIsNone(fleet_history._cached(("24h", "US")))

    def test_entry_expires_after_period_ttl(self):
        fleet_history._store(("1h", "EU"), {"scope": "EU"})
        self.monotonic.return_value = 1000.0 + CACHE_TTL_SEC["1h"]
        self.assertIsNone(fleet_history._cached(("1h", "EU")))

    def test_store_drops_expired_keys_of_renamed_folders(self):
        fleet_history._store(("1h", "old name"), {"scope": "old"})
        self.monotonic.return_value = 1000.0 + CACHE_TTL_SEC["1h"]
        fleet_history._store(("1h", "new name"), {"scope": "new"})
        self.assertEqual(set(fleet_history._cache), {("1h", "new name")})

    def test_day_history_is_kept_until_its_next_point_can_appear(self):
        # Пересборка суток читает сырые снапшоты всего парка за 24 часа — секунды
        # на большом парке, а новая точка появляется раз в бакет
        fleet_history._store(("24h", None), {"scope": "fleet"})
        self.monotonic.return_value = 1000.0 + FLEET_PERIODS["24h"].bucket_sec - 1
        self.assertEqual(fleet_history._cached(("24h", None)), {"scope": "fleet"})


class ColdBuildTests(unittest.IsolatedAsyncioTestCase):
    """Сборка суток на большом парке идёт секунды: остальные периоды и папки
    не должны стоять за ней в очереди."""

    async def asyncSetUp(self) -> None:
        for state in (fleet_history._cache, fleet_history._build_locks):
            state.clear()
            self.addCleanup(state.clear)
        self.day_may_finish = asyncio.Event()
        self.builds: list[tuple[str, str | None]] = []

        async def build(db, period, folder):
            self.builds.append((period, folder))
            if period == "24h":
                await self.day_may_finish.wait()
            return {"period": period, "folder": folder}

        build_patch = patch.object(fleet_history, "_build_history", build)
        build_patch.start()
        self.addCleanup(build_patch.stop)

    async def test_slow_build_of_one_period_does_not_hold_another(self):
        day = asyncio.create_task(fleet_history.load_fleet_history(None, "24h"))
        await asyncio.sleep(0)
        try:
            hour = await asyncio.wait_for(fleet_history.load_fleet_history(None, "1h"), timeout=1.0)
        finally:
            self.day_may_finish.set()
            await day
        self.assertEqual(hour, {"period": "1h", "folder": None})

    async def test_requests_for_the_same_key_still_build_it_once(self):
        first = asyncio.create_task(fleet_history.load_fleet_history(None, "24h", "EU"))
        second = asyncio.create_task(fleet_history.load_fleet_history(None, "24h", "EU"))
        await asyncio.sleep(0)
        self.day_may_finish.set()
        self.assertEqual(await first, await second)
        self.assertEqual(self.builds, [("24h", "EU")])

    async def test_locks_of_renamed_folders_do_not_pile_up(self):
        # Папки переименовывают и удаляют: лок на ключ старого имени больше никому не нужен
        with patch.dict(fleet_history.CACHE_TTL_SEC, {"1h": 0.0}):
            await fleet_history.load_fleet_history(None, "1h", "old name")
            await fleet_history.load_fleet_history(None, "1h", "new name")
        self.assertEqual(set(fleet_history._build_locks), {("1h", "new name")})


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
