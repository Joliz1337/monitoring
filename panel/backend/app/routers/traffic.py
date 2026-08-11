"""История трафика из базы панели: сводки, ряды точек и окна простоя нод.

Роутер принципиально не ходит к нодам. Всё, что раньше приходилось спрашивать у
агента — версия, список отслеживаемых портов, сама история — лежит в PostgreSQL,
поэтому страница трафика открывается и когда нода недоступна.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import Server, ServerDowntime, ServerTraffic, TrafficImportState
from app.routers.proxy import to_iso_utc
from app.services.traffic_import import (
    MIN_NODE_VERSION_FOR_TRAFFIC_V2,
    ImportStatus,
    node_supports_traffic_v2,
)
from app.services.traffic_ingest import Period, Scope, TOTAL_SCOPE_KEY, floor_day, floor_hour

router = APIRouter(prefix="/traffic", tags=["traffic"])

SeriesPeriod = Literal["24h", "7d", "30d", "365d"]
SeriesScope = Literal["total", "iface", "port"]

# period → тип бакета, шаг ряда, количество точек
SERIES_PERIODS: dict[str, tuple[Period, timedelta, int]] = {
    "24h": (Period.HOUR, timedelta(hours=1), 24),
    "7d": (Period.HOUR, timedelta(hours=1), 24 * 7),
    "30d": (Period.DAY, timedelta(days=1), 30),
    "365d": (Period.DAY, timedelta(days=1), 365),
}

MAX_SUMMARY_DAYS = 400   # горизонт хранения суточных бакетов у коллектора
MAX_PORT_NUMBER = 65535


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _summary_since(days: int) -> datetime:
    """Левая граница сводки: ровно `days` суточных бакетов, включая текущие сутки."""
    return floor_day(_utcnow()) - timedelta(days=days - 1)


def _tracked_ports(raw: Optional[str]) -> list[int]:
    if not raw:
        return []
    try:
        ports = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(ports, list):
        return []
    return [port for port in ports if isinstance(port, int) and 0 < port <= MAX_PORT_NUMBER]


def _as_port(scope_key: str) -> Optional[int]:
    try:
        return int(scope_key)
    except ValueError:
        return None


def _by_volume(entry: dict) -> int:
    return entry["rx_bytes"] + entry["tx_bytes"]


async def _load_server(server_id: int, db: AsyncSession) -> Server:
    server = await db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")
    return server


@router.get("/{server_id}/status")
async def get_traffic_status(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    first_data_at = (
        select(func.min(ServerTraffic.bucket))
        .where(
            ServerTraffic.server_id == server_id,
            ServerTraffic.period_type == Period.DAY.value,
            ServerTraffic.scope == Scope.TOTAL.value,
        )
        .scalar_subquery()
    )
    result = await db.execute(
        select(
            Server.node_version,
            TrafficImportState.status,
            TrafficImportState.imported_at,
            first_data_at.label("first_data_at"),
        )
        .select_from(Server)
        .outerjoin(TrafficImportState, TrafficImportState.server_id == Server.id)
        .where(Server.id == server_id)
    )
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Server not found")

    return {
        "supported": node_supports_traffic_v2(row.node_version),
        "node_version": row.node_version,
        "min_version": MIN_NODE_VERSION_FOR_TRAFFIC_V2,
        "import": {
            "status": row.status or ImportStatus.PENDING.value,
            "imported_at": to_iso_utc(row.imported_at),
        },
        "first_data_at": to_iso_utc(row.first_data_at),
    }


@router.get("/{server_id}/summary")
async def get_server_traffic_summary(
    server_id: int,
    days: int = Query(default=30, ge=1, le=MAX_SUMMARY_DAYS),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    # Гейт версии сознательно НЕ применяется к чтению: интерфейсные счётчики приходят в
    # /api/metrics от ноды любой версии, поэтому суммарный и по-интерфейсный трафик панель
    # копит и для необновлённых агентов. Прятать эти данные значило бы противоречить самой
    # себе — плитка трафика на карточке сервера считает их из той же таблицы. От версии
    # зависит только разбивка по портам, и об этом говорит поле supported в /status.
    server = await _load_server(server_id, db)
    result = await db.execute(
        select(
            ServerTraffic.scope,
            ServerTraffic.scope_key,
            func.sum(ServerTraffic.rx_bytes).label("rx_bytes"),
            func.sum(ServerTraffic.tx_bytes).label("tx_bytes"),
        )
        .where(
            ServerTraffic.server_id == server_id,
            ServerTraffic.period_type == Period.DAY.value,
            ServerTraffic.bucket >= _summary_since(days),
        )
        .group_by(ServerTraffic.scope, ServerTraffic.scope_key)
    )
    rows = result.all()

    total = {"rx_bytes": 0, "tx_bytes": 0, "days": days}
    by_interface: list[dict] = []
    by_port: list[dict] = []
    for row in rows:
        rx, tx = int(row.rx_bytes or 0), int(row.tx_bytes or 0)
        if row.scope == Scope.TOTAL.value:
            total["rx_bytes"] = rx
            total["tx_bytes"] = tx
        elif row.scope == Scope.IFACE.value:
            by_interface.append({"interface": row.scope_key, "rx_bytes": rx, "tx_bytes": tx})
        elif row.scope == Scope.PORT.value:
            port = _as_port(row.scope_key)
            if port is not None:
                by_port.append({"port": port, "rx_bytes": rx, "tx_bytes": tx})

    by_interface.sort(key=_by_volume, reverse=True)
    by_port.sort(key=_by_volume, reverse=True)

    return {
        "days": days,
        "total": total,
        "by_interface": by_interface,
        "by_port": by_port,
        # Список портов — из колонки servers, а не с ноды: сводка обязана открываться офлайн
        "tracked_ports": _tracked_ports(server.tracked_ports),
    }


@router.get("/{server_id}/series")
async def get_traffic_series(
    server_id: int,
    period: SeriesPeriod = Query(default="24h"),
    scope: SeriesScope = Query(default="total"),
    key: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    await _load_server(server_id, db)
    period_type, step, point_count = SERIES_PERIODS[period]

    scope_key = TOTAL_SCOPE_KEY if scope == Scope.TOTAL.value else key.strip()
    if scope != Scope.TOTAL.value and not scope_key:
        raise HTTPException(status_code=400, detail=f"key is required for scope={scope}")

    now = _utcnow()
    last_bucket = floor_hour(now) if period_type is Period.HOUR else floor_day(now)
    first_bucket = last_bucket - step * (point_count - 1)

    measured = await _load_buckets(db, server_id, period_type, scope, scope_key, first_bucket, last_bucket)
    gaps = await _load_gaps(db, server_id, first_bucket, now)

    return {
        "period": period,
        "scope": scope,
        "key": scope_key,
        "points": _dense_points(first_bucket, step, point_count, measured),
        "gaps": gaps,
    }


async def _load_buckets(
    db: AsyncSession,
    server_id: int,
    period_type: Period,
    scope: str,
    scope_key: str,
    first_bucket: datetime,
    last_bucket: datetime,
) -> dict[datetime, tuple[int, int]]:
    result = await db.execute(
        select(
            ServerTraffic.bucket,
            ServerTraffic.rx_bytes,
            ServerTraffic.tx_bytes,
            ServerTraffic.covered_seconds,
        ).where(
            ServerTraffic.server_id == server_id,
            ServerTraffic.period_type == period_type.value,
            ServerTraffic.scope == scope,
            ServerTraffic.scope_key == scope_key,
            ServerTraffic.bucket >= first_bucket,
            ServerTraffic.bucket <= last_bucket,
        )
    )
    # covered_seconds = 0 сам по себе разрывом не считается: у суток, перенесённых из
    # легаси-истории ноды, окна измерений нет по построению, а байты в них настоящие.
    return {
        row.bucket: (int(row.rx_bytes), int(row.tx_bytes))
        for row in result.all()
        if row.covered_seconds > 0 or row.rx_bytes > 0 or row.tx_bytes > 0
    }


async def _load_gaps(
    db: AsyncSession, server_id: int, window_start: datetime, window_end: datetime
) -> list[dict]:
    """Простои ноды, пересекающиеся с окном ряда, обрезанные по его границам."""
    result = await db.execute(
        select(ServerDowntime.started_at, ServerDowntime.ended_at)
        .where(
            ServerDowntime.server_id == server_id,
            ServerDowntime.started_at < window_end,
            or_(ServerDowntime.ended_at.is_(None), ServerDowntime.ended_at > window_start),
        )
        .order_by(ServerDowntime.started_at)
    )

    merged: list[list[datetime]] = []
    for started_at, ended_at in result.all():
        start = max(started_at, window_start)
        end = min(ended_at or window_end, window_end)
        if end <= start:
            continue
        # Простои ноды и панели за одно и то же время лежат отдельными строками —
        # без склейки фронт закрасил бы один провал двумя полосами.
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            continue
        merged.append([start, end])

    return [{"from": to_iso_utc(start), "to": to_iso_utc(end)} for start, end in merged]


def _dense_points(
    first_bucket: datetime,
    step: timedelta,
    point_count: int,
    measured: dict[datetime, tuple[int, int]],
) -> list[dict]:
    """Все бакеты периода подряд; там, где наблюдений не было — rx/tx = null.

    Без явного null график соединил бы соседние точки прямой линией через провал,
    и оператор не увидел бы, что ноды в это время не было.
    """
    points = []
    bucket = first_bucket
    for _ in range(point_count):
        value = measured.get(bucket)
        points.append({
            "timestamp": to_iso_utc(bucket),
            "rx": value[0] if value is not None else None,
            "tx": value[1] if value is not None else None,
        })
        bucket += step
    return points
