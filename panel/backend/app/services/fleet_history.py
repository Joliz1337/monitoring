"""Сводная история всего парка для плиток дашборда.

Плитки сводки показывают мгновенный срез: суммарные CPU, RAM и скорости сети
по онлайн-нодам. Здесь та же арифметика, но по истории — за час, сутки, неделю,
месяц или год. Отдельной таблицы под это нет: точка собирается на лету из
per-server истории (`metrics_snapshots` и `aggregated_metrics`) двумя уровнями
агрегации — сначала среднее по (сервер, бакет), потом взвешенная сумма по
бакету. Без внутреннего уровня нода, отдавшая в бакет два снапшота, весила бы
вдвое.

CPU и память взвешиваются ёмкостью ноды: 100% на 8 ядрах весят больше, чем на
двух. Ёмкость в истории не хранится, поэтому берётся текущая — из последнего
ответа ноды (`Server.last_metrics`). Ресайз VPS переписывает вес всей его
истории задним числом; для обзорного графика парка это дешевле, чем колонки
ёмкости в каждой строке снапшотов.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Literal, Mapping, Optional

from sqlalchemy import Float, Integer, bindparam, or_, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Server, ServerDowntime
from app.services.metrics_history import (
    DAY_SEC,
    GAP_MIN_SEC,
    HOUR_SEC,
    DataSource,
    PeriodSpec,
    _as_db_param,
    _iso_utc,
    _utcnow,
    align_down,
    insert_gap_markers,
    load_collect_interval,
    merge_downtime,
)

FleetPeriod = Literal["1h", "24h", "7d", "30d", "365d"]

# Снапшоты разных нод не выровнены по времени, поэтому сырой период тоже
# бакетируется — «точек как есть» у парка не бывает.
FLEET_PERIODS: dict[str, PeriodSpec] = {
    "1h": PeriodSpec(DataSource.RAW, timedelta(hours=1), 60),
    "24h": PeriodSpec(DataSource.RAW, timedelta(hours=24), 300),
    "7d": PeriodSpec(DataSource.HOUR, timedelta(days=7), HOUR_SEC),
    "30d": PeriodSpec(DataSource.HOUR, timedelta(days=30), HOUR_SEC),
    "365d": PeriodSpec(DataSource.DAY, timedelta(days=365), DAY_SEC),
}

# Ответ читает снапшоты всего парка — без кэша каждая открытая вкладка
# дашборда гоняла бы запрос заново. TTL — не чаще появления новой точки.
CACHE_TTL_SEC: dict[str, float] = {"1h": 30.0, "24h": 60.0, "7d": 300.0, "30d": 300.0, "365d": 300.0}

# Простой самой панели (перерыва в сборе) — единственное, что рвёт линию парка:
# падение одной ноды просто вычитает её из суммы. Строка пишется на каждый
# активный сервер, см. MetricsCollector.DOWNTIME_KIND_PANEL.
PANEL_DOWNTIME_KIND = "panel"

# Нода, у которой не удалось прочитать ёмкость, всё равно должна попадать в
# средневзвешенное — с минимальным весом, а не выпадать из него.
DEFAULT_CORES = 1.0

FLEET_METRIC_KEYS = (
    "cpu_usage", "max_cpu",
    "memory_percent", "max_memory_percent", "memory_used", "memory_total",
    "net_rx_bytes_per_sec", "max_net_rx_bytes_per_sec",
    "net_tx_bytes_per_sec", "max_net_tx_bytes_per_sec",
)

# Веса нод приходят тремя массивами и раскладываются unnest'ом в таблицу — так
# ёмкость остаётся параметром запроса, а не подставленным в текст SQL списком.
# CAST обязателен: unnest полиморфен, и без явного типа PostgreSQL не выведет
# тип параметра. FILTER на знаменателе — тоже: нода без значения метрики не
# должна занижать средневзвешенное своим весом.
_FLEET_SELECT = """
    SELECT p.bucket,
           COUNT(*) AS servers,
           SUM(p.cpu * w.cores) / NULLIF(SUM(w.cores) FILTER (WHERE p.cpu IS NOT NULL), 0) AS cpu_usage,
           SUM(p.cpu_max * w.cores) / NULLIF(SUM(w.cores) FILTER (WHERE p.cpu_max IS NOT NULL), 0) AS max_cpu,
           SUM(p.mem * w.ram) / NULLIF(SUM(w.ram) FILTER (WHERE p.mem IS NOT NULL), 0) AS memory_percent,
           SUM(p.mem_max * w.ram) / NULLIF(SUM(w.ram) FILTER (WHERE p.mem_max IS NOT NULL), 0) AS max_memory_percent,
           SUM(w.ram) FILTER (WHERE p.mem IS NOT NULL) AS memory_total,
           SUM(p.rx) AS net_rx_bytes_per_sec,
           SUM(p.rx_max) AS max_net_rx_bytes_per_sec,
           SUM(p.tx) AS net_tx_bytes_per_sec,
           SUM(p.tx_max) AS max_net_tx_bytes_per_sec
    FROM per_server p
    JOIN unnest(CAST(:ids AS int[]), CAST(:cores AS float8[]), CAST(:ram AS float8[]))
         AS w(server_id, cores, ram) ON w.server_id = p.server_id
    GROUP BY p.bucket
    ORDER BY p.bucket
"""

# origin date_bin — epoch: границы бакетов не зависят от момента запроса,
# при автообновлении дашборда точки не «дышат».
_RAW_CTE = """
    WITH per_server AS (
        SELECT date_bin(make_interval(secs => :bucket), m.timestamp, TIMESTAMPTZ 'epoch') AS bucket,
               m.server_id,
               AVG(m.cpu_usage) AS cpu,
               MAX(COALESCE(m.cpu_usage_max, m.cpu_usage)) AS cpu_max,
               AVG(m.memory_percent) AS mem,
               MAX(m.memory_percent) AS mem_max,
               AVG(m.net_rx_bytes_per_sec) AS rx,
               MAX(COALESCE(m.net_rx_bytes_per_sec_max, m.net_rx_bytes_per_sec)) AS rx_max,
               AVG(m.net_tx_bytes_per_sec) AS tx,
               MAX(COALESCE(m.net_tx_bytes_per_sec_max, m.net_tx_bytes_per_sec)) AS tx_max
        FROM metrics_snapshots m
        WHERE m.server_id = ANY(:ids) AND m.timestamp >= :start AND m.timestamp <= :end
        GROUP BY bucket, m.server_id
    )
"""

# У агрегатов уникальный ключ (server_id, period_type, timestamp), так что
# строка на сервер и бакет уже одна — группировать нечего.
_AGGREGATED_CTE = """
    WITH per_server AS (
        SELECT a.timestamp AS bucket, a.server_id,
               a.avg_cpu AS cpu, COALESCE(a.max_cpu, a.avg_cpu) AS cpu_max,
               a.avg_memory_percent AS mem, COALESCE(a.max_memory_percent, a.avg_memory_percent) AS mem_max,
               a.avg_rx_speed AS rx, COALESCE(a.max_rx_speed, a.avg_rx_speed) AS rx_max,
               a.avg_tx_speed AS tx, COALESCE(a.max_tx_speed, a.avg_tx_speed) AS tx_max
        FROM aggregated_metrics a
        WHERE a.server_id = ANY(:ids) AND a.period_type = :period_type
          AND a.timestamp >= :start AND a.timestamp <= :end
    )
"""


def _weight_params():
    return (
        bindparam("ids", type_=ARRAY(Integer)),
        bindparam("cores", type_=ARRAY(Float)),
        bindparam("ram", type_=ARRAY(Float)),
    )


RAW_SQL = text(_RAW_CTE + _FLEET_SELECT).bindparams(*_weight_params())
AGGREGATED_SQL = text(_AGGREGATED_CTE + _FLEET_SELECT).bindparams(*_weight_params())


@dataclass(frozen=True)
class FleetCapacity:
    """Ёмкость парка тремя параллельными массивами — как их принимает unnest."""
    ids: list[int]
    cores: list[float]
    ram: list[float]

    def __bool__(self) -> bool:
        return bool(self.ids)


def _section(data: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    for key in path:
        value = data.get(key)
        if not isinstance(value, Mapping):
            return {}
        data = value
    return data


def _positive(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def node_capacity(last_metrics: Optional[str]) -> tuple[float, float]:
    """Ядра и объём RAM ноды из её последнего ответа.

    Нулевой вес памяти у ноды, чьей ёмкости не знаем, — она не участвует в
    средневзвешенном по памяти, но остаётся в суммах скоростей.
    """
    if not last_metrics:
        return DEFAULT_CORES, 0.0
    try:
        metrics = json.loads(last_metrics)
    except (TypeError, ValueError):
        return DEFAULT_CORES, 0.0
    if not isinstance(metrics, Mapping):
        return DEFAULT_CORES, 0.0
    cores = _positive(_section(metrics, "cpu").get("cores_logical")) or DEFAULT_CORES
    ram = _positive(_section(metrics, "memory", "ram").get("total")) or 0.0
    return cores, ram


def build_capacity(rows: Iterable[tuple[int, Optional[str]]]) -> FleetCapacity:
    ids: list[int] = []
    cores: list[float] = []
    ram: list[float] = []
    for server_id, last_metrics in rows:
        node_cores, node_ram = node_capacity(last_metrics)
        ids.append(server_id)
        cores.append(node_cores)
        ram.append(node_ram)
    return FleetCapacity(ids, cores, ram)


def empty_fleet_point(ts: datetime) -> dict[str, Any]:
    point: dict[str, Any] = {"timestamp": _iso_utc(ts), "servers": 0}
    point.update({key: None for key in FLEET_METRIC_KEYS})
    return point


def fleet_point(row: Mapping[str, Any]) -> dict[str, Any]:
    """Строка запроса → точка ряда; занятые байты — из процента и суммы объёмов."""
    percent = row["memory_percent"]
    total = row["memory_total"]
    point = empty_fleet_point(row["bucket"])
    point.update({key: row[key] for key in FLEET_METRIC_KEYS if key in row})
    point["servers"] = row["servers"]
    point["memory_used"] = None if percent is None or total is None else percent * total / 100
    return point


async def load_capacity(db: AsyncSession) -> FleetCapacity:
    result = await db.execute(
        select(Server.id, Server.last_metrics).where(Server.is_active == True)  # noqa: E712
    )
    return build_capacity(result.all())


async def load_panel_downtime(
    db: AsyncSession, window_start: datetime, window_end: datetime
) -> list[tuple[datetime, Optional[datetime]]]:
    """Простои панели, пересекающиеся с окном; distinct — строка пишется на каждую ноду."""
    result = await db.execute(
        select(ServerDowntime.started_at, ServerDowntime.ended_at)
        .where(
            ServerDowntime.kind == PANEL_DOWNTIME_KIND,
            ServerDowntime.started_at < window_end,
            or_(ServerDowntime.ended_at.is_(None), ServerDowntime.ended_at > window_start),
        )
        .distinct()
        .order_by(ServerDowntime.started_at)
    )
    return [(started_at, ended_at) for started_at, ended_at in result.all()]


async def load_points(
    db: AsyncSession,
    capacity: FleetCapacity,
    spec: PeriodSpec,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "ids": capacity.ids,
        "cores": capacity.cores,
        "ram": capacity.ram,
        "start": _as_db_param(start),
        "end": _as_db_param(end),
    }
    if spec.source is DataSource.RAW:
        params["bucket"] = float(spec.bucket_sec)
        result = await db.execute(RAW_SQL, params)
    else:
        params["period_type"] = spec.source.value
        result = await db.execute(AGGREGATED_SQL, params)
    return [fleet_point(row) for row in result.mappings().all()]


async def _build_history(db: AsyncSession, period: FleetPeriod) -> dict[str, Any]:
    spec = FLEET_PERIODS[period]
    end = _utcnow()
    # Начало окна — на границе бакета: иначе первый бакет считался бы по
    # усыхающему хвосту снапшотов и менялся при каждом обновлении страницы.
    start = align_down(end - spec.span, spec.bucket_sec)

    capacity = await load_capacity(db)
    points = await load_points(db, capacity, spec, start, end) if capacity else []
    gaps = merge_downtime(await load_panel_downtime(db, start, end), start, end)
    interval = await load_collect_interval(db)
    min_gap_sec = max(GAP_MIN_SEC, 2 * interval, spec.bucket_sec)

    data = insert_gap_markers(points, gaps, min_gap_sec, empty_fleet_point)
    return {
        "period": period,
        "data_source": spec.source.value,
        "bucket_sec": spec.bucket_sec,
        "from_time": _iso_utc(start),
        "to_time": _iso_utc(end),
        "count": len(data),
        "data": data,
        "gaps": [{"from": _iso_utc(gap_start), "to": _iso_utc(gap_end)} for gap_start, gap_end in gaps],
    }


_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = asyncio.Lock()


def _cached(period: str) -> Optional[dict[str, Any]]:
    entry = _cache.get(period)
    if entry is None:
        return None
    stored_at, payload = entry
    return payload if time.monotonic() - stored_at < CACHE_TTL_SEC[period] else None


async def load_fleet_history(db: AsyncSession, period: FleetPeriod) -> dict[str, Any]:
    payload = _cached(period)
    if payload is not None:
        return payload

    async with _cache_lock:
        # Пока ждали лок, другой запрос мог пересобрать кэш.
        payload = _cached(period)
        if payload is not None:
            return payload
        payload = await _build_history(db, period)
        _cache[period] = (time.monotonic(), payload)
        return payload
