"""История метрик сервера для графиков.

Один эндпоинт — пять периодов и три источника: снапшоты как есть (1h),
снапшоты в бакетах date_bin (24h), часовые (7d, 30d) и суточные (365d)
агрегаты. Точка ответа одна для всех источников: чего в источнике нет —
null, а не 0, иначе простой ноды рисовался бы нулевой нагрузкой.

Время: в коде панели naive UTC. asyncpg кодирует naive datetime в параметр
timestamptz как системное локальное время, а timestamptz из БД отдаёт
aware — поэтому параметры уходят в БД с явным UTC, а результат
приводится обратно к naive.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Literal, Mapping, Optional

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PanelSettings, ServerDowntime

HistoryPeriod = Literal["1h", "24h", "7d", "30d", "365d"]


class DataSource(str, Enum):
    RAW = "raw"
    HOUR = "hour"
    DAY = "day"


HOUR_SEC = 3600
DAY_SEC = 86400


@dataclass(frozen=True)
class PeriodSpec:
    source: DataSource
    span: timedelta
    bucket_sec: Optional[int]   # None — снапшоты как есть, без группировки


SERIES_PERIODS: dict[str, PeriodSpec] = {
    "1h": PeriodSpec(DataSource.RAW, timedelta(hours=1), None),
    "24h": PeriodSpec(DataSource.RAW, timedelta(hours=24), 300),
    "7d": PeriodSpec(DataSource.HOUR, timedelta(days=7), HOUR_SEC),
    "30d": PeriodSpec(DataSource.HOUR, timedelta(days=30), HOUR_SEC),
    "365d": PeriodSpec(DataSource.DAY, timedelta(days=365), DAY_SEC),
}

# Столбцы тепловой карты: 120 на часе, 144 на сутках — ячейка с зазором
# остаётся различимой на ширине карточки.
PER_CPU_BUCKET: dict[str, int] = {"1h": 30, "24h": 600}
# Выше этого числа ядер бакет вдвое шире — иначе матрица ячеек в ответе
# весит больше самой страницы.
MANY_CORES = 32

# Разрыв линии — простой дольше max(GAP_MIN_SEC, 2×интервал сбора, ширина бакета):
# один пропущенный опрос — не разрыв, а простой короче бакета не оставляет
# пустого бакета, и рвать линию между двумя точками с данными не за что.
GAP_MIN_SEC = 30
DEFAULT_COLLECT_INTERVAL_SEC = 10

EPOCH = datetime(1970, 1, 1)

POINT_METRIC_KEYS = (
    "cpu_usage", "max_cpu",
    "memory_percent", "max_memory_percent", "memory_used", "memory_available",
    "load_avg_1", "max_load",
    "net_rx_bytes_per_sec", "max_net_rx_bytes_per_sec",
    "net_tx_bytes_per_sec", "max_net_tx_bytes_per_sec",
    "disk_percent", "disk_read_bytes_per_sec", "disk_write_bytes_per_sec",
    "process_count",
    "tcp_established", "tcp_listen", "tcp_time_wait", "tcp_close_wait",
    "tcp_syn_sent", "tcp_syn_recv", "tcp_fin_wait",
)

RAW_POINTS_SQL = text("""
    SELECT m.timestamp, m.cpu_usage, m.cpu_usage_max,
           m.memory_percent, m.memory_used, m.memory_available,
           m.load_avg_1,
           m.net_rx_bytes_per_sec, m.net_rx_bytes_per_sec_max,
           m.net_tx_bytes_per_sec, m.net_tx_bytes_per_sec_max,
           m.disk_percent, m.disk_read_bytes_per_sec, m.disk_write_bytes_per_sec,
           m.process_count,
           m.tcp_established, m.tcp_listen, m.tcp_time_wait, m.tcp_close_wait,
           m.tcp_syn_sent, m.tcp_syn_recv, m.tcp_fin_wait
    FROM metrics_snapshots m
    WHERE m.server_id = :sid AND m.timestamp >= :start AND m.timestamp <= :end
    ORDER BY m.timestamp
""")

# Origin date_bin — epoch: границы бакетов не зависят от момента запроса,
# при автообновлении страницы точки не «дышат». ::float8 на AVG по integer —
# иначе asyncpg отдаёт Decimal.
RAW_BUCKETS_SQL = text("""
    SELECT date_bin(make_interval(secs => :bucket), m.timestamp, TIMESTAMPTZ 'epoch') AS bucket,
           COUNT(*) AS data_points,
           AVG(m.cpu_usage) AS cpu_usage,
           MAX(COALESCE(m.cpu_usage_max, m.cpu_usage)) AS max_cpu,
           AVG(m.memory_percent) AS memory_percent,
           MAX(m.memory_percent) AS max_memory_percent,
           AVG(m.memory_used)::float8 AS memory_used,
           AVG(m.memory_available)::float8 AS memory_available,
           AVG(m.load_avg_1) AS load_avg_1,
           MAX(m.load_avg_1) AS max_load,
           AVG(m.net_rx_bytes_per_sec) AS net_rx_bytes_per_sec,
           MAX(COALESCE(m.net_rx_bytes_per_sec_max, m.net_rx_bytes_per_sec)) AS max_net_rx_bytes_per_sec,
           AVG(m.net_tx_bytes_per_sec) AS net_tx_bytes_per_sec,
           MAX(COALESCE(m.net_tx_bytes_per_sec_max, m.net_tx_bytes_per_sec)) AS max_net_tx_bytes_per_sec,
           AVG(m.disk_percent) AS disk_percent,
           AVG(m.disk_read_bytes_per_sec) AS disk_read_bytes_per_sec,
           AVG(m.disk_write_bytes_per_sec) AS disk_write_bytes_per_sec,
           AVG(m.process_count)::float8 AS process_count,
           AVG(m.tcp_established)::float8 AS tcp_established,
           AVG(m.tcp_listen)::float8 AS tcp_listen,
           AVG(m.tcp_time_wait)::float8 AS tcp_time_wait,
           AVG(m.tcp_close_wait)::float8 AS tcp_close_wait,
           AVG(m.tcp_syn_sent)::float8 AS tcp_syn_sent,
           AVG(m.tcp_syn_recv)::float8 AS tcp_syn_recv,
           AVG(m.tcp_fin_wait)::float8 AS tcp_fin_wait
    FROM metrics_snapshots m
    WHERE m.server_id = :sid AND m.timestamp >= :start AND m.timestamp <= :end
    GROUP BY bucket
    ORDER BY bucket
""")

# max_* агрегатов NULL у строк, посчитанных до появления колонок пиков —
# полоса пиков тогда нулевой высоты, а не дыра в ряду.
AGGREGATED_SQL = text("""
    SELECT a.timestamp AS bucket, a.data_points,
           a.avg_cpu AS cpu_usage, a.max_cpu,
           a.avg_memory_percent AS memory_percent, a.max_memory_percent,
           a.avg_load AS load_avg_1, COALESCE(a.max_load, a.avg_load) AS max_load,
           a.avg_rx_speed AS net_rx_bytes_per_sec,
           COALESCE(a.max_rx_speed, a.avg_rx_speed) AS max_net_rx_bytes_per_sec,
           a.avg_tx_speed AS net_tx_bytes_per_sec,
           COALESCE(a.max_tx_speed, a.avg_tx_speed) AS max_net_tx_bytes_per_sec,
           a.avg_disk_percent AS disk_percent,
           a.avg_disk_read_speed AS disk_read_bytes_per_sec,
           a.avg_disk_write_speed AS disk_write_bytes_per_sec,
           a.avg_tcp_established AS tcp_established,
           a.avg_tcp_listen AS tcp_listen,
           a.avg_tcp_time_wait AS tcp_time_wait,
           a.avg_tcp_close_wait AS tcp_close_wait,
           a.avg_tcp_syn_sent AS tcp_syn_sent,
           a.avg_tcp_syn_recv AS tcp_syn_recv,
           a.avg_tcp_fin_wait AS tcp_fin_wait
    FROM aggregated_metrics a
    WHERE a.server_id = :sid AND a.period_type = :period_type
      AND a.timestamp >= :start AND a.timestamp <= :end
    ORDER BY a.timestamp
""")

# LEFT(...) = '[' — гард от строк, где per_cpu_percent не JSON-массив: ::jsonb
# на них уронил бы весь запрос.
PER_CPU_SQL = text("""
    SELECT date_bin(make_interval(secs => :bucket), m.timestamp, TIMESTAMPTZ 'epoch') AS bucket,
           c.ord - 1 AS core,
           AVG(c.val::float8) AS usage_percent
    FROM metrics_snapshots m
    CROSS JOIN LATERAL jsonb_array_elements_text(m.per_cpu_percent::jsonb) WITH ORDINALITY AS c(val, ord)
    WHERE m.server_id = :sid AND m.timestamp >= :start AND m.timestamp <= :end
      AND m.per_cpu_percent IS NOT NULL AND LEFT(m.per_cpu_percent, 1) = '['
    GROUP BY bucket, core
    ORDER BY bucket, core
""")

CORE_COUNT_SQL = text("""
    SELECT jsonb_array_length(m.per_cpu_percent::jsonb)
    FROM metrics_snapshots m
    WHERE m.server_id = :sid AND m.timestamp >= :start AND m.timestamp <= :end
      AND m.per_cpu_percent IS NOT NULL AND LEFT(m.per_cpu_percent, 1) = '['
    ORDER BY m.timestamp DESC
    LIMIT 1
""")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _as_db_param(dt: datetime) -> datetime:
    return _as_naive(dt).replace(tzinfo=timezone.utc)


def _iso_utc(dt: datetime) -> str:
    """Тот же формат, что у proxy.to_iso_utc (миллисекунды + Z).

    Свой, а не импорт: роутер proxy импортирует этот сервис, обратный импорт
    замкнул бы цикл.
    """
    dt = _as_naive(dt)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def choose_bucket(period: str, cores: int) -> int:
    base = PER_CPU_BUCKET[period]
    return base * 2 if cores > MANY_CORES else base


def align_down(ts: datetime, bucket_sec: int) -> datetime:
    """Начало бакета, в который попадает ts, — та же сетка, что у date_bin от epoch."""
    step = timedelta(seconds=bucket_sec)
    return EPOCH + step * ((ts - EPOCH) // step)


def bucket_grid(start: datetime, end: datetime, bucket_sec: int) -> list[datetime]:
    """Все бакеты от бакета start до бакета end включительно."""
    step = timedelta(seconds=bucket_sec)
    first, last = align_down(start, bucket_sec), align_down(end, bucket_sec)
    count = (last - first) // step + 1
    return [first + step * index for index in range(count)]


def merge_downtime(
    rows: Iterable[tuple[datetime, Optional[datetime]]],
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[datetime, datetime]]:
    """Простои, обрезанные по окну и склеенные там, где пересекаются.

    Простои ноды и панели за одно и то же время лежат отдельными строками —
    без склейки фронт закрасил бы один провал двумя полосами.
    """
    merged: list[list[datetime]] = []
    for started_at, ended_at in sorted(rows, key=lambda row: row[0]):
        start = max(started_at, window_start)
        end = min(ended_at or window_end, window_end)
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            continue
        merged.append([start, end])
    return [(start, end) for start, end in merged]


def empty_point(ts: datetime) -> dict[str, Any]:
    point: dict[str, Any] = {"timestamp": _iso_utc(ts), "data_points": 0}
    point.update({key: None for key in POINT_METRIC_KEYS})
    return point


def _peak(max_value: Optional[float], avg_value: Optional[float]) -> Optional[float]:
    return avg_value if max_value is None else max_value


def raw_point(row: Mapping[str, Any]) -> dict[str, Any]:
    """Снапшот как есть: пиков памяти и load у одиночного замера нет."""
    point = empty_point(row["timestamp"])
    point.update({
        "data_points": 1,
        "cpu_usage": row["cpu_usage"],
        "max_cpu": _peak(row["cpu_usage_max"], row["cpu_usage"]),
        "memory_percent": row["memory_percent"],
        "memory_used": row["memory_used"],
        "memory_available": row["memory_available"],
        "load_avg_1": row["load_avg_1"],
        "net_rx_bytes_per_sec": row["net_rx_bytes_per_sec"],
        "max_net_rx_bytes_per_sec": _peak(row["net_rx_bytes_per_sec_max"], row["net_rx_bytes_per_sec"]),
        "net_tx_bytes_per_sec": row["net_tx_bytes_per_sec"],
        "max_net_tx_bytes_per_sec": _peak(row["net_tx_bytes_per_sec_max"], row["net_tx_bytes_per_sec"]),
        "disk_percent": row["disk_percent"],
        "disk_read_bytes_per_sec": row["disk_read_bytes_per_sec"],
        "disk_write_bytes_per_sec": row["disk_write_bytes_per_sec"],
        "process_count": row["process_count"],
        "tcp_established": row["tcp_established"],
        "tcp_listen": row["tcp_listen"],
        "tcp_time_wait": row["tcp_time_wait"],
        "tcp_close_wait": row["tcp_close_wait"],
        "tcp_syn_sent": row["tcp_syn_sent"],
        "tcp_syn_recv": row["tcp_syn_recv"],
        "tcp_fin_wait": row["tcp_fin_wait"],
    })
    return point


def aggregated_point(row: Mapping[str, Any]) -> dict[str, Any]:
    """Бакет или агрегат: колонки уже названы как поля точки, отсутствующие — null."""
    point = empty_point(row["bucket"])
    point["data_points"] = row["data_points"]
    point.update({key: row.get(key) for key in POINT_METRIC_KEYS})
    return point


def insert_gap_markers(
    points: list[dict[str, Any]],
    gaps: Iterable[tuple[datetime, datetime]],
    min_gap_sec: float,
    make_empty: Callable[[datetime], dict[str, Any]] = empty_point,
) -> list[dict[str, Any]]:
    """Точка со всеми null посреди долгого простоя — чтобы линия там рвалась.

    Метки времени в фиксированном формате (UTC, миллисекунды, Z), поэтому
    строки сравниваются как время. `make_empty` — чтобы ряд с другой схемой
    точки (сводка по парку) получал маркер своей формы, а не этой.
    """
    markers = [
        make_empty(start + (end - start) / 2)
        for start, end in gaps
        if (end - start).total_seconds() > min_gap_sec
    ]
    if not markers:
        return points
    return sorted(points + markers, key=lambda point: point["timestamp"])


def per_cpu_matrix(
    rows: Iterable[tuple[datetime, int, float]],
    grid: list[datetime],
) -> list[list[Optional[float]]]:
    """Строка — ядро, столбец — бакет сетки; бакет без замеров → null."""
    column_by_bucket = {bucket: index for index, bucket in enumerate(grid)}
    cores: list[list[Optional[float]]] = []
    for bucket, core, usage in rows:
        column = column_by_bucket.get(bucket)
        if column is None:
            continue
        while len(cores) <= core:
            cores.append([None] * len(grid))
        cores[core][column] = round(usage, 1)
    return cores


async def load_collect_interval(db: AsyncSession) -> int:
    result = await db.execute(
        select(PanelSettings.value).where(PanelSettings.key == "metrics_collect_interval")
    )
    value = result.scalar_one_or_none()
    return int(value) if value else DEFAULT_COLLECT_INTERVAL_SEC


async def load_raw_points(
    db: AsyncSession, server_id: int, start: datetime, end: datetime
) -> list[Mapping[str, Any]]:
    result = await db.execute(
        RAW_POINTS_SQL,
        {"sid": server_id, "start": _as_db_param(start), "end": _as_db_param(end)},
    )
    return list(result.mappings().all())


async def load_raw_buckets(
    db: AsyncSession, server_id: int, start: datetime, end: datetime, bucket_sec: int
) -> list[Mapping[str, Any]]:
    result = await db.execute(
        RAW_BUCKETS_SQL,
        {
            "sid": server_id,
            "bucket": float(bucket_sec),
            "start": _as_db_param(start),
            "end": _as_db_param(end),
        },
    )
    return list(result.mappings().all())


async def load_aggregated(
    db: AsyncSession, server_id: int, source: DataSource, start: datetime, end: datetime
) -> list[Mapping[str, Any]]:
    result = await db.execute(
        AGGREGATED_SQL,
        {
            "sid": server_id,
            "period_type": source.value,
            "start": _as_db_param(start),
            "end": _as_db_param(end),
        },
    )
    return list(result.mappings().all())


async def load_core_count(
    db: AsyncSession, server_id: int, start: datetime, end: datetime
) -> int:
    result = await db.execute(
        CORE_COUNT_SQL,
        {"sid": server_id, "start": _as_db_param(start), "end": _as_db_param(end)},
    )
    return result.scalar_one_or_none() or 0


async def load_per_cpu(
    db: AsyncSession, server_id: int, start: datetime, end: datetime, bucket_sec: int
) -> list[tuple[datetime, int, float]]:
    result = await db.execute(
        PER_CPU_SQL,
        {
            "sid": server_id,
            "bucket": float(bucket_sec),
            "start": _as_db_param(start),
            "end": _as_db_param(end),
        },
    )
    return [(_as_naive(row.bucket), int(row.core), float(row.usage_percent)) for row in result.all()]


async def load_downtime(
    db: AsyncSession, server_id: int, window_start: datetime, window_end: datetime
) -> list[tuple[datetime, Optional[datetime]]]:
    result = await db.execute(
        select(ServerDowntime.started_at, ServerDowntime.ended_at)
        .where(
            ServerDowntime.server_id == server_id,
            ServerDowntime.started_at < window_end,
            or_(ServerDowntime.ended_at.is_(None), ServerDowntime.ended_at > window_start),
        )
        .order_by(ServerDowntime.started_at)
    )
    return [(started_at, ended_at) for started_at, ended_at in result.all()]


async def _load_points(
    db: AsyncSession, server_id: int, spec: PeriodSpec, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    if spec.source is not DataSource.RAW:
        rows = await load_aggregated(db, server_id, spec.source, start, end)
        return [aggregated_point(row) for row in rows]
    if spec.bucket_sec is None:
        rows = await load_raw_points(db, server_id, start, end)
        return [raw_point(row) for row in rows]
    rows = await load_raw_buckets(db, server_id, start, end, spec.bucket_sec)
    return [aggregated_point(row) for row in rows]


async def _load_per_cpu_block(
    db: AsyncSession, server_id: int, period: str, start: datetime, end: datetime
) -> Optional[dict[str, Any]]:
    if period not in PER_CPU_BUCKET:
        return None
    cores = await load_core_count(db, server_id, start, end)
    if cores == 0:
        return None
    bucket_sec = choose_bucket(period, cores)
    grid_start = align_down(start, bucket_sec)
    grid = bucket_grid(grid_start, end, bucket_sec)
    rows = await load_per_cpu(db, server_id, grid_start, end, bucket_sec)
    return {
        "bucket_sec": bucket_sec,
        "timestamps": [_iso_utc(bucket) for bucket in grid],
        "cores": per_cpu_matrix(rows, grid),
    }


async def load_history(
    db: AsyncSession, server_id: int, period: HistoryPeriod, include_per_cpu: bool
) -> dict[str, Any]:
    spec = SERIES_PERIODS[period]
    end = _utcnow()
    start = end - spec.span
    if spec.source is DataSource.RAW and spec.bucket_sec is not None:
        # Начало окна — на границе бакета: иначе первый бакет считался бы по
        # усыхающему хвосту снапшотов и менялся при каждом обновлении страницы
        start = align_down(start, spec.bucket_sec)

    interval = await load_collect_interval(db)
    points = await _load_points(db, server_id, spec, start, end)
    gaps = merge_downtime(await load_downtime(db, server_id, start, end), start, end)
    min_gap_sec = max(GAP_MIN_SEC, 2 * interval, spec.bucket_sec or 0)

    data = insert_gap_markers(points, gaps, min_gap_sec)
    response: dict[str, Any] = {
        "period": period,
        "data_source": spec.source.value,
        "bucket_sec": spec.bucket_sec,
        "from_time": _iso_utc(start),
        "to_time": _iso_utc(end),
        "count": len(data),
        "data": data,
        "gaps": [{"from": _iso_utc(gap_start), "to": _iso_utc(gap_end)} for gap_start, gap_end in gaps],
    }
    if include_per_cpu:
        response["per_cpu"] = await _load_per_cpu_block(db, server_id, period, start, end)
    return response
