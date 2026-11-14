"""Чистые функции сводной истории парка.

Проверяется то, что не видно на графике: ёмкость ноды разбирается из чего
угодно и не роняет запрос, точка сводки всегда одной формы, занятые байты
считаются из процента и суммы объёмов, а одинаковые простои панели (строка на
каждую ноду) схлопываются в один разрыв.

Час и сутки доращиваются по бакетам: вместо PostgreSQL здесь таблица снапшотов
в памяти, которая считает бакеты так же, как запрос, и помнит, какие диапазоны
из неё читали.
"""

import asyncio
import os
import sys
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Callable, Iterable, Optional
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.dialects import postgresql  # noqa: E402

from app.services.fleet_history import (  # noqa: E402
    CACHE_TTL_SEC,
    DEFAULT_CORES,
    FLEET_METRIC_KEYS,
    FLEET_PERIODS,
    RAW_CHUNK_SEC,
    RAW_SETTLE_SEC,
    FleetCapacity,
    build_capacity,
    empty_fleet_point,
    fleet_point,
    node_capacity,
)
from app.services import fleet_history  # noqa: E402
from app.services.latest_metrics import LatestMetrics  # noqa: E402
from app.services.metrics_history import (  # noqa: E402
    DataSource,
    align_down,
    insert_gap_markers,
    merge_downtime,
)


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

    def test_raw_chunk_holds_a_whole_number_of_buckets(self):
        # Точку бакета нельзя сложить из двух запросов: стык кусков обязан лежать на сетке
        for period, spec in FLEET_PERIODS.items():
            if spec.source is DataSource.RAW:
                self.assertEqual(RAW_CHUNK_SEC % spec.bucket_sec, 0, period)


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
        # Новая точка суток появляется раз в бакет — чаще пересобирать незачем
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


NOW = datetime(2026, 9, 2, 12, 3, 17)
SNAPSHOT_STEP = timedelta(seconds=30)
FLEET = FleetCapacity(ids=[1, 2, 3], cores=[2.0, 8.0, 4.0], ram=[4e9, 16e9, 0.0])


def snapshot(server_id: int, ts: datetime) -> dict:
    """Строка metrics_snapshots: значения свои у каждой ноды и момента, пики и пропуски вперемешку."""
    tick = int((ts - at(0)).total_seconds()) // 30
    wave = float(tick * (server_id + 2) % 97)
    return {
        "server_id": server_id,
        "timestamp": ts,
        "cpu_usage": wave,
        "cpu_usage_max": wave + 3 if tick % 4 else None,
        "memory_percent": None if server_id == 2 and tick % 50 == 0 else 20 + wave / 2,
        "net_rx_bytes_per_sec": 1000 * wave,
        "net_rx_bytes_per_sec_max": 1500 * wave if tick % 3 else None,
        "net_tx_bytes_per_sec": 10 * wave,
        "net_tx_bytes_per_sec_max": None,
    }


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def highest(values: Iterable[Optional[float]]) -> Optional[float]:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def total(values: Iterable[Optional[float]]) -> Optional[float]:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def weighted_mean(pairs: Iterable[tuple[Optional[float], float]]) -> Optional[float]:
    present = [(value, weight) for value, weight in pairs if value is not None]
    weight_sum = sum(weight for _, weight in present)
    return sum(value * weight for value, weight in present) / weight_sum if weight_sum else None


def peaks(rows: list[dict], column: str) -> list[Optional[float]]:
    return [row[column] if row[f"{column}_max"] is None else row[f"{column}_max"] for row in rows]


def fleet_rows(
    snapshots: list[dict], capacity: FleetCapacity, bucket_sec: int, in_window: Callable[[datetime], bool]
) -> list[dict]:
    """То же, что считает RAW_SQL: среднее по (сервер, бакет), затем взвешенная сумма по бакету."""
    weights = dict(zip(capacity.ids, zip(capacity.cores, capacity.ram)))
    per_server: dict[tuple[datetime, int], list[dict]] = defaultdict(list)
    for row in snapshots:
        if row["server_id"] in weights and in_window(row["timestamp"]):
            per_server[(align_down(row["timestamp"], bucket_sec), row["server_id"])].append(row)

    buckets: dict[datetime, list[dict]] = defaultdict(list)
    for (bucket, server_id), rows in per_server.items():
        cores, ram = weights[server_id]
        buckets[bucket].append({
            "cores": cores,
            "ram": ram,
            "cpu": mean(row["cpu_usage"] for row in rows),
            "cpu_max": highest(peaks(rows, "cpu_usage")),
            "mem": mean(row["memory_percent"] for row in rows),
            "mem_max": highest(row["memory_percent"] for row in rows),
            "rx": mean(row["net_rx_bytes_per_sec"] for row in rows),
            "rx_max": highest(peaks(rows, "net_rx_bytes_per_sec")),
            "tx": mean(row["net_tx_bytes_per_sec"] for row in rows),
            "tx_max": highest(peaks(rows, "net_tx_bytes_per_sec")),
        })

    return [
        {
            "bucket": bucket,
            "servers": len(nodes),
            "cpu_usage": weighted_mean((node["cpu"], node["cores"]) for node in nodes),
            "max_cpu": weighted_mean((node["cpu_max"], node["cores"]) for node in nodes),
            "memory_percent": weighted_mean((node["mem"], node["ram"]) for node in nodes),
            "max_memory_percent": weighted_mean((node["mem_max"], node["ram"]) for node in nodes),
            "memory_total": total(node["ram"] for node in nodes if node["mem"] is not None),
            "net_rx_bytes_per_sec": total(node["rx"] for node in nodes),
            "max_net_rx_bytes_per_sec": total(node["rx_max"] for node in nodes),
            "net_tx_bytes_per_sec": total(node["tx"] for node in nodes),
            "max_net_tx_bytes_per_sec": total(node["tx_max"] for node in nodes),
        }
        for bucket, nodes in sorted(buckets.items())
    ]


class SnapshotTable:
    """metrics_snapshots в памяти: отвечает на RAW_SQL и помнит, какие диапазоны из неё читали."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.reads: list[tuple[datetime, datetime]] = []
        self.fail_on_read: Optional[int] = None

    def fill(self, server_ids: Iterable[int], after: datetime, until: datetime) -> None:
        """Снапшоты с метками в (after, until]; у каждой ноды свой сдвиг, как у настоящего опроса."""
        tick = align_down(after, 30)
        while tick <= until:
            for server_id in server_ids:
                ts = tick + timedelta(seconds=server_id)
                if after < ts <= until:
                    self.rows.append(snapshot(server_id, ts))
            tick += SNAPSHOT_STEP

    async def execute(self, statement, params: dict):
        if statement is not fleet_history.RAW_SQL:
            raise AssertionError("сырой период читает снапшоты только через RAW_SQL")
        # Как настоящий запрос: управление уходит циклу событий
        await asyncio.sleep(0)
        if len(self.reads) == self.fail_on_read:
            self.fail_on_read = None
            raise ConnectionError("connection is closed")
        start, end = (params[name].replace(tzinfo=None) for name in ("start", "end"))
        self.reads.append((start, end))
        capacity = FleetCapacity(params["ids"], params["cores"], params["ram"])
        rows = fleet_rows(self.rows, capacity, int(params["bucket"]), lambda ts: start <= ts < end)
        for row in rows:
            # asyncpg отдаёт timestamptz с зоной
            row["bucket"] = row["bucket"].replace(tzinfo=timezone.utc)
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))


def window_start(period: str, now: datetime) -> datetime:
    spec = FLEET_PERIODS[period]
    return align_down(now - spec.span, spec.bucket_sec)


def one_query(table: SnapshotTable, capacity: FleetCapacity, period: str, now: datetime) -> list[dict]:
    """Прежняя сборка: один запрос на всё окно, снапшоты по `now` включительно."""
    start = window_start(period, now)
    rows = fleet_rows(table.rows, capacity, FLEET_PERIODS[period].bucket_sec, lambda ts: start <= ts <= now)
    return [fleet_point(row) for row in rows]


class RawSeriesTests(unittest.IsolatedAsyncioTestCase):
    """Час и сутки доращиваются по бакетам: точки те же, что у одного запроса на
    всё окно, а из базы читается только то, чего ещё нет или что ещё меняется."""

    async def asyncSetUp(self) -> None:
        fleet_history._raw_series.clear()
        self.addCleanup(fleet_history._raw_series.clear)
        self.now = NOW
        self.table = SnapshotTable()
        self.table.fill(FLEET.ids, NOW - timedelta(hours=25), NOW)

    def advance(self, seconds: int) -> None:
        """Время идёт, коллектор пишет новые снапшоты."""
        later = self.now + timedelta(seconds=seconds)
        self.table.fill(FLEET.ids, self.now, later)
        self.now = later

    async def refresh(
        self, period: str, capacity: FleetCapacity = FLEET, folder: Optional[str] = None
    ) -> list[dict]:
        return await fleet_history.refresh_raw_series(
            self.table, (period, folder), capacity, FLEET_PERIODS[period].bucket_sec,
            window_start(period, self.now), self.now,
        )

    async def test_cold_build_gives_the_points_of_one_query(self):
        for period in ("1h", "24h"):
            with self.subTest(period=period):
                self.assertEqual(await self.refresh(period), one_query(self.table, FLEET, period, self.now))

    async def test_refreshes_keep_giving_them_as_the_window_slides(self):
        for period in ("1h", "24h"):
            await self.refresh(period)
        for seconds in (40, 310, 95, 1200, 3700):
            self.advance(seconds)
            for period in ("1h", "24h"):
                with self.subTest(period=period, seconds=seconds):
                    self.assertEqual(await self.refresh(period), one_query(self.table, FLEET, period, self.now))

    async def test_snapshot_exactly_on_a_joint_of_two_pieces_is_counted_once(self):
        joint = window_start("24h", self.now) + timedelta(seconds=RAW_CHUNK_SEC)
        self.table.rows.append(snapshot(1, joint))

        self.assertEqual(await self.refresh("24h"), one_query(self.table, FLEET, "24h", self.now))

    async def test_cold_build_reads_the_day_in_hourly_pieces(self):
        await self.refresh("24h")

        reads = self.table.reads
        self.assertEqual(len(reads), 25)
        self.assertEqual(reads[0][0], window_start("24h", self.now))
        self.assertEqual(reads[-1][1], align_down(self.now, 300) + timedelta(seconds=300))
        self.assertEqual([start for start, _ in reads[1:]], [end for _, end in reads[:-1]])
        self.assertLessEqual(max(end - start for start, end in reads), timedelta(seconds=RAW_CHUNK_SEC))

    async def test_refresh_reads_only_buckets_that_can_still_change(self):
        await self.refresh("24h")
        built_at = self.now
        self.table.reads.clear()

        self.advance(300)
        await self.refresh("24h")

        still_open = align_down(built_at - timedelta(seconds=RAW_SETTLE_SEC), 300)
        self.assertEqual(self.table.reads, [(still_open, align_down(self.now, 300) + timedelta(seconds=300))])
        self.assertEqual(self.table.reads[0][1] - still_open, timedelta(minutes=10))

    async def test_snapshot_committed_after_its_bucket_closed_is_counted(self):
        # 12:05:30 — бакет 12:00–12:05 уже закрыт, а цикл коллектора с меткой
        # 12:04:59 ещё не дошёл до базы
        self.advance(133)
        before = (await self.refresh("24h"))[-2]
        self.assertEqual(before["timestamp"], "2026-09-02T12:00:00.000Z")

        self.table.rows.append(snapshot(1, datetime(2026, 9, 2, 12, 4, 59)))
        self.advance(300)
        points = await self.refresh("24h")

        self.assertEqual(points, one_query(self.table, FLEET, "24h", self.now))
        self.assertNotEqual(points[-3], before)

    async def test_buckets_that_left_the_window_are_dropped(self):
        await self.refresh("1h")
        self.advance(1800)
        await self.refresh("1h")

        kept = fleet_history._raw_series[("1h", None)].points
        self.assertEqual(min(kept), window_start("1h", self.now))
        self.assertEqual(len(kept), 61)

    async def test_resized_node_recounts_every_bucket(self):
        before = await self.refresh("24h")
        self.table.reads.clear()
        resized = FleetCapacity(FLEET.ids, [2.0, 16.0, 4.0], FLEET.ram)

        points = await self.refresh("24h", resized)

        self.assertEqual(self.table.reads[0][0], window_start("24h", self.now))
        self.assertEqual(points, one_query(self.table, resized, "24h", self.now))
        self.assertNotEqual(points, before)

    async def test_changed_set_of_nodes_recounts_every_bucket(self):
        await self.refresh("24h")
        self.table.reads.clear()
        without_third = FleetCapacity(FLEET.ids[:2], FLEET.cores[:2], FLEET.ram[:2])

        points = await self.refresh("24h", without_third)

        self.assertEqual(len(self.table.reads), 25)
        self.assertEqual(points, one_query(self.table, without_third, "24h", self.now))
        self.assertEqual({point["servers"] for point in points}, {2})

    async def test_same_fleet_listed_in_another_order_keeps_its_buckets(self):
        # Выборка серверов идёт без ORDER BY: порядок строк может смениться сам
        await self.refresh("24h")
        self.table.reads.clear()
        reordered = FleetCapacity(FLEET.ids[::-1], FLEET.cores[::-1], FLEET.ram[::-1])

        points = await self.refresh("24h", reordered)

        self.assertEqual(len(self.table.reads), 1)
        self.assertEqual(points, one_query(self.table, FLEET, "24h", self.now))

    async def test_interrupted_cold_build_goes_on_from_where_it_stopped(self):
        self.table.fail_on_read = 3
        with self.assertRaises(ConnectionError):
            await self.refresh("24h")
        finished = list(self.table.reads)
        self.table.reads.clear()

        points = await self.refresh("24h")

        self.assertEqual(len(finished), 3)
        self.assertEqual(self.table.reads[0][0], finished[-1][1])
        self.assertEqual(len(self.table.reads), 22)
        self.assertEqual(points, one_query(self.table, FLEET, "24h", self.now))

    async def test_folder_and_whole_fleet_grow_apart(self):
        europe = FleetCapacity([1], [2.0], [4e9])
        await self.refresh("24h", europe, "EU")
        await self.refresh("24h")
        self.advance(310)

        self.assertEqual(await self.refresh("24h", europe, "EU"), one_query(self.table, europe, "24h", self.now))
        self.assertEqual(await self.refresh("24h"), one_query(self.table, FLEET, "24h", self.now))


class RawHistoryAnswerTests(unittest.IsolatedAsyncioTestCase):
    """Ответ эндпоинта для сырого периода: форма прежняя, пересборка — с хвоста."""

    async def asyncSetUp(self) -> None:
        for state in (fleet_history._cache, fleet_history._build_locks, fleet_history._raw_series):
            state.clear()
            self.addCleanup(state.clear)
        self.now = NOW
        self.capacity = FLEET
        self.table = SnapshotTable()
        self.table.fill(FLEET.ids, NOW - timedelta(hours=25), NOW)
        for name, replacement in (
            ("_utcnow", lambda: self.now),
            ("load_capacity", AsyncMock(side_effect=lambda db, folder: self.capacity)),
            ("load_panel_downtime", AsyncMock(return_value=[])),
            ("load_collect_interval", AsyncMock(return_value=10)),
        ):
            patcher = patch.object(fleet_history, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_day_answer_keeps_its_shape_and_points(self):
        payload = await fleet_history.load_fleet_history(self.table, "24h")

        points = one_query(self.table, FLEET, "24h", NOW)
        self.assertEqual(len(points), 289)
        self.assertEqual(payload, {
            "period": "24h",
            "data_source": "raw",
            "bucket_sec": 300,
            "from_time": "2026-09-01T12:00:00.000Z",
            "to_time": "2026-09-02T12:03:17.000Z",
            "count": 289,
            "data": points,
            "gaps": [],
        })

    async def test_expired_answer_is_rebuilt_from_the_tail(self):
        with patch.dict(fleet_history.CACHE_TTL_SEC, {"24h": 0.0}):
            await fleet_history.load_fleet_history(self.table, "24h")
            self.table.reads.clear()
            self.table.fill(FLEET.ids, self.now, self.now + timedelta(seconds=300))
            self.now += timedelta(seconds=300)

            payload = await fleet_history.load_fleet_history(self.table, "24h")

        self.assertEqual(len(self.table.reads), 1)
        self.assertEqual(payload["data"], one_query(self.table, FLEET, "24h", self.now))

    async def test_folder_that_lost_its_nodes_drops_its_buckets(self):
        with patch.dict(fleet_history.CACHE_TTL_SEC, {"24h": 0.0}):
            await fleet_history.load_fleet_history(self.table, "24h", "EU")
            self.table.reads.clear()
            self.capacity = FleetCapacity([], [], [])

            payload = await fleet_history.load_fleet_history(self.table, "24h", "EU")

        self.assertEqual((payload["count"], payload["data"]), (0, []))
        self.assertEqual(self.table.reads, [])
        self.assertNotIn(("24h", "EU"), fleet_history._raw_series)


class SeriesLifetimeTests(unittest.TestCase):
    def setUp(self):
        for state in (fleet_history._cache, fleet_history._raw_series):
            state.clear()
            self.addCleanup(state.clear)
        clock = patch.object(fleet_history.time, "monotonic", return_value=1000.0)
        self.monotonic = clock.start()
        self.addCleanup(clock.stop)

    def series(self, refreshed_at: float) -> fleet_history.RawSeries:
        return fleet_history.RawSeries({1: (2.0, 4e9)}, settled_until=at(12), refreshed_at=refreshed_at)

    def test_series_idle_longer_than_its_window_is_dropped(self):
        # Папки переименовывают и удаляют; а в серии, которую сутки не трогали,
        # не осталось ни одного бакета из окна
        day = FLEET_PERIODS["24h"].span.total_seconds()
        fleet_history._raw_series[("24h", "old name")] = self.series(refreshed_at=1000.0)
        fleet_history._raw_series[("24h", "EU")] = self.series(refreshed_at=1000.0 + day / 2)
        self.monotonic.return_value = 1000.0 + day

        fleet_history._store(("7d", None), {"scope": "fleet"})

        self.assertEqual(set(fleet_history._raw_series), {("24h", "EU")})

    def test_replaced_database_forgets_everything_counted(self):
        fleet_history._store(("24h", None), {"scope": "fleet"})
        fleet_history._raw_series[("24h", None)] = self.series(refreshed_at=1000.0)

        fleet_history.forget_fleet_history()

        self.assertIsNone(fleet_history._cached(("24h", None)))
        self.assertEqual(fleet_history._raw_series, {})


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
