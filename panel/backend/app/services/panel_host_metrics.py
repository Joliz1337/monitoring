"""История нагрузки хоста самой панели.

Сэмплер раз в секунду читает CPU, память и load average хоста, раз в
SNAPSHOT_INTERVAL_SEC пишет строку со средним и максимумом по накопленным
пробам — точка истории описывает весь интервал, а не одну секунду, как у нод.
Одна таблица без агрегатов: 30 дней по строке в 10 секунд — четверть
миллиона строк, бакеты date_bin считаются по ним напрямую.

Время — naive UTC, как в metrics_history (см. докстринг там).
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Optional, Sequence

import psutil
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import PanelHostMetric
from app.services.metrics_collector import next_collection_tick
from app.services.metrics_history import (
    GAP_MIN_SEC,
    _as_db_param,
    _as_naive,
    _iso_utc,
    _utcnow,
    align_down,
    insert_gap_markers,
)

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SEC = 1.0
SNAPSHOT_INTERVAL_SEC = 10
RETENTION = timedelta(days=30)
CLEANUP_INTERVAL_SEC = 3600

HostHistoryPeriod = Literal["1h", "24h", "7d", "30d"]


@dataclass(frozen=True)
class HostPeriodSpec:
    span: timedelta
    bucket_sec: Optional[int]   # None — снапшоты как есть


HOST_PERIODS: dict[str, HostPeriodSpec] = {
    "1h": HostPeriodSpec(timedelta(hours=1), None),
    "24h": HostPeriodSpec(timedelta(hours=24), 300),
    "7d": HostPeriodSpec(timedelta(days=7), 3600),
    "30d": HostPeriodSpec(timedelta(days=30), 3600),
}

POINT_METRIC_KEYS = (
    "cpu_usage", "max_cpu",
    "memory_percent", "max_memory_percent", "memory_used", "memory_available",
    "load_avg_1", "max_load",
)


@dataclass(frozen=True)
class HostSample:
    cpu_percent: float
    memory_percent: float
    memory_used: int
    memory_available: int
    load_avg_1: float


def summarize_samples(samples: Sequence[HostSample]) -> dict[str, Any]:
    """Строка снапшота: среднее и максимум по секундным пробам интервала."""
    count = len(samples)
    return {
        "cpu_usage": sum(s.cpu_percent for s in samples) / count,
        "cpu_usage_max": max(s.cpu_percent for s in samples),
        "memory_percent": sum(s.memory_percent for s in samples) / count,
        "memory_percent_max": max(s.memory_percent for s in samples),
        "memory_used": round(sum(s.memory_used for s in samples) / count),
        "memory_available": round(sum(s.memory_available for s in samples) / count),
        "load_avg_1": sum(s.load_avg_1 for s in samples) / count,
        "load_avg_1_max": max(s.load_avg_1 for s in samples),
    }


def detect_gaps(timestamps: Sequence[datetime], min_gap_sec: float) -> list[tuple[datetime, datetime]]:
    """Простои панели по дырам между соседними точками — своей таблицы простоев у неё нет."""
    return [
        (previous, current)
        for previous, current in zip(timestamps, timestamps[1:])
        if (current - previous).total_seconds() > min_gap_sec
    ]


def _cpu_busy_total(times: Any) -> tuple[float, float]:
    # Как в psutil: guest уже входит в user/nice, iowait — простой
    total = sum(times) - getattr(times, "guest", 0.0) - getattr(times, "guest_nice", 0.0)
    idle = times.idle + getattr(times, "iowait", 0.0)
    return total - idle, total


def cpu_percent_between(before: Any, after: Any) -> float:
    busy_before, total_before = _cpu_busy_total(before)
    busy_after, total_after = _cpu_busy_total(after)
    total_delta = total_after - total_before
    if total_delta <= 0:
        return 0.0
    return max(0.0, min(100.0, (busy_after - busy_before) / total_delta * 100))


def read_host_sample(previous_cpu_times: Any) -> tuple[HostSample, Any]:
    """Блокирующее чтение /proc — вызывать через to_thread.

    CPU считается по собственной дельте cpu_times, а не psutil.cpu_percent:
    у того один глобальный «прошлый замер» на процесс, и эндпоинт /system/stats
    сбивал бы окно сэмплера своими вызовами.
    """
    cpu_times = psutil.cpu_times()
    memory = psutil.virtual_memory()
    sample = HostSample(
        cpu_percent=cpu_percent_between(previous_cpu_times, cpu_times),
        memory_percent=memory.percent,
        memory_used=memory.used,
        memory_available=memory.available,
        load_avg_1=psutil.getloadavg()[0],
    )
    return sample, cpu_times


class PanelHostSampler:
    def __init__(self) -> None:
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._samples: list[HostSample] = []

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Panel host sampler started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        cpu_times = await asyncio.to_thread(psutil.cpu_times)
        tick = time.monotonic()
        next_snapshot = tick + SNAPSHOT_INTERVAL_SEC
        next_cleanup = tick
        while self._running:
            tick = next_collection_tick(tick, SAMPLE_INTERVAL_SEC, time.monotonic())
            await asyncio.sleep(max(0.0, tick - time.monotonic()))
            try:
                sample, cpu_times = await asyncio.to_thread(read_host_sample, cpu_times)
                self._samples.append(sample)
            except Exception as e:
                logger.warning(f"Panel host sample failed: {e}")

            now = time.monotonic()
            if now >= next_snapshot:
                next_snapshot = next_collection_tick(next_snapshot, SNAPSHOT_INTERVAL_SEC, now)
                await self._store_snapshot()
            if now >= next_cleanup:
                next_cleanup = now + CLEANUP_INTERVAL_SEC
                await self._cleanup()

    async def _store_snapshot(self) -> None:
        if not self._samples:
            return
        row = summarize_samples(self._samples)
        self._samples = []
        try:
            async with async_session() as db:
                await db.execute(
                    PanelHostMetric.__table__.insert().values(timestamp=_as_db_param(_utcnow()), **row)
                )
                await db.commit()
        except Exception as e:
            logger.error(f"Panel host snapshot not stored: {e}")

    async def _cleanup(self) -> None:
        try:
            async with async_session() as db:
                await db.execute(
                    delete(PanelHostMetric).where(PanelHostMetric.timestamp < _as_db_param(_utcnow() - RETENTION))
                )
                await db.commit()
        except Exception as e:
            logger.error(f"Panel host metrics cleanup failed: {e}")


RAW_POINTS_SQL = text("""
    SELECT timestamp, cpu_usage, cpu_usage_max, memory_percent, memory_percent_max,
           memory_used, memory_available, load_avg_1, load_avg_1_max
    FROM panel_host_metrics
    WHERE timestamp >= :start AND timestamp <= :end
    ORDER BY timestamp
""")

BUCKETS_SQL = text("""
    SELECT date_bin(make_interval(secs => :bucket), timestamp, TIMESTAMPTZ 'epoch') AS timestamp,
           COUNT(*) AS data_points,
           AVG(cpu_usage) AS cpu_usage,
           MAX(cpu_usage_max) AS cpu_usage_max,
           AVG(memory_percent) AS memory_percent,
           MAX(memory_percent_max) AS memory_percent_max,
           AVG(memory_used)::float8 AS memory_used,
           AVG(memory_available)::float8 AS memory_available,
           AVG(load_avg_1) AS load_avg_1,
           MAX(load_avg_1_max) AS load_avg_1_max
    FROM panel_host_metrics
    WHERE timestamp >= :start AND timestamp <= :end
    GROUP BY 1
    ORDER BY 1
""")


def history_point(row: Any, data_points: int) -> dict[str, Any]:
    return {
        "timestamp": _iso_utc(row.timestamp),
        "data_points": data_points,
        "cpu_usage": row.cpu_usage,
        "max_cpu": row.cpu_usage_max,
        "memory_percent": row.memory_percent,
        "max_memory_percent": row.memory_percent_max,
        "memory_used": row.memory_used,
        "memory_available": row.memory_available,
        "load_avg_1": row.load_avg_1,
        "max_load": row.load_avg_1_max,
    }


async def load_host_history(db: AsyncSession, period: HostHistoryPeriod) -> dict[str, Any]:
    spec = HOST_PERIODS[period]
    end = _utcnow()
    start = end - spec.span
    if spec.bucket_sec is not None:
        start = align_down(start, spec.bucket_sec)

    params = {"start": _as_db_param(start), "end": _as_db_param(end)}
    if spec.bucket_sec is None:
        rows = (await db.execute(RAW_POINTS_SQL, params)).all()
        points = [history_point(row, 1) for row in rows]
    else:
        rows = (await db.execute(BUCKETS_SQL, {**params, "bucket": spec.bucket_sec})).all()
        points = [history_point(row, row.data_points) for row in rows]

    min_gap_sec = max(GAP_MIN_SEC, 2 * SNAPSHOT_INTERVAL_SEC, spec.bucket_sec or 0)
    gaps = detect_gaps([_as_naive(row.timestamp) for row in rows], min_gap_sec)
    return {
        "period": period,
        "bucket_sec": spec.bucket_sec,
        "from_time": _iso_utc(start),
        "to_time": _iso_utc(end),
        "count": len(points),
        "data": insert_gap_markers(points, gaps, min_gap_sec),
        "gaps": [{"from": _iso_utc(gap_start), "to": _iso_utc(gap_end)} for gap_start, gap_end in gaps],
    }


_sampler: Optional[PanelHostSampler] = None


async def start_panel_host_sampler() -> None:
    global _sampler
    if _sampler is None:
        _sampler = PanelHostSampler()
    await _sampler.start()


async def stop_panel_host_sampler() -> None:
    if _sampler is not None:
        await _sampler.stop()
