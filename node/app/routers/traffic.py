"""Traffic API: живые счётчики по портам и замороженные легаси-витрины.

Историю трафика ведёт панель, нода отдаёт только сырые счётчики. View hourly/
daily/monthly/summary/ports/interfaces остались как read-only витрины старой
SQLite: «Обновить всё» в панели обновляет сначала ноды и только потом саму
панель, поэтому окно «новая нода со старой панелью» существует всегда, а фолбэк
на кэш у старой панели срабатывает только по 5xx — на 404 страница трафика
покраснела бы.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.legacy_traffic_store import (
    MAX_EXPORT_DAYS,
    LegacyStorePurgeError,
    LegacyStoreUnreadableError,
    get_legacy_traffic_store,
)
from app.services.port_traffic_sampler import get_port_traffic_sampler

router = APIRouter(prefix="/api/traffic", tags=["traffic"])


class PortRequest(BaseModel):
    port: int = Field(ge=1, le=65535)


@router.get("/hourly")
async def get_hourly_traffic(
    hours: int = Query(24, ge=1, le=168, description="Number of hours (max 168 = 7 days)"),
    interface: Optional[str] = Query(None, description="Filter by interface name"),
    port: Optional[int] = Query(None, description="Filter by port number"),
):
    data = await get_legacy_traffic_store().hourly(hours=hours, interface=interface, port=port)
    return {
        "hours": hours,
        "interface": interface,
        "port": port,
        "data": data,
        "total_rx": sum(d["rx_bytes"] for d in data),
        "total_tx": sum(d["tx_bytes"] for d in data),
    }


@router.get("/daily")
async def get_daily_traffic(
    days: int = Query(30, ge=1, le=90, description="Number of days (max 90)"),
    interface: Optional[str] = Query(None, description="Filter by interface name"),
    port: Optional[int] = Query(None, description="Filter by port number"),
):
    data = await get_legacy_traffic_store().daily(days=days, interface=interface, port=port)
    return {
        "days": days,
        "interface": interface,
        "port": port,
        "data": data,
        "total_rx": sum(d["rx_bytes"] for d in data),
        "total_tx": sum(d["tx_bytes"] for d in data),
    }


@router.get("/monthly")
async def get_monthly_traffic(
    months: int = Query(12, ge=1, le=24, description="Number of months (max 24)"),
    interface: Optional[str] = Query(None, description="Filter by interface name"),
    port: Optional[int] = Query(None, description="Filter by port number"),
):
    data = await get_legacy_traffic_store().monthly(months=months, interface=interface, port=port)
    return {
        "months": months,
        "interface": interface,
        "port": port,
        "data": data,
        "total_rx": sum(d["rx_bytes"] for d in data),
        "total_tx": sum(d["tx_bytes"] for d in data),
    }


@router.get("/summary")
async def get_traffic_summary(
    days: int = Query(30, ge=1, le=90, description="Number of days for summary"),
):
    store = get_legacy_traffic_store()
    return {
        "days": days,
        "total": await store.totals(days=days),
        "by_interface": await store.interface_summary(days=days),
        "by_port": await store.port_summary(days=days),
        "tracked_ports": get_port_traffic_sampler().tracked_ports(),
    }


@router.get("/ports")
async def get_ports_traffic(
    days: int = Query(30, ge=1, le=90, description="Number of days"),
):
    ports = await get_legacy_traffic_store().port_summary(days=days)
    return {
        "days": days,
        "tracked_ports": get_port_traffic_sampler().tracked_ports(),
        "data": ports,
        "total_rx": sum(p["rx_bytes"] for p in ports),
        "total_tx": sum(p["tx_bytes"] for p in ports),
    }


@router.get("/interfaces")
async def get_interfaces_traffic(
    days: int = Query(30, ge=1, le=90, description="Number of days"),
):
    interfaces = await get_legacy_traffic_store().interface_summary(days=days)
    return {
        "days": days,
        "data": interfaces,
        "total_rx": sum(i["rx_bytes"] for i in interfaces),
        "total_tx": sum(i["tx_bytes"] for i in interfaces),
    }


@router.get("/ports/tracked")
async def get_tracked_ports():
    return {"tracked_ports": get_port_traffic_sampler().tracked_ports()}


@router.post("/ports/add")
async def add_tracked_port(request: PortRequest):
    success, message = await get_port_traffic_sampler().add_tracked_port(request.port)
    return {"success": success, "message": message}


@router.post("/ports/remove")
async def remove_tracked_port(request: PortRequest):
    success, message = await get_port_traffic_sampler().remove_tracked_port(request.port)
    return {"success": success, "message": message}


@router.get("/legacy/export")
async def export_legacy_traffic(
    max_days: int = Query(
        MAX_EXPORT_DAYS, ge=1, le=MAX_EXPORT_DAYS, description="History window in days"
    ),
):
    """Дневные итоги старой SQLite для одноразового импорта в панель."""
    try:
        return await get_legacy_traffic_store().export(max_days=max_days)
    except LegacyStoreUnreadableError as e:
        # Именно 5xx, а не пустой ответ: панель не должна принять нечитаемую БД
        # за пустую и удалить её, не забрав историю.
        raise HTTPException(status_code=503, detail=f"Legacy traffic DB is unreadable: {e.reason}")


@router.post("/legacy/purge")
async def purge_legacy_traffic():
    """Удалить старую SQLite после успешного импорта. Повторный вызов — no-op."""
    try:
        return await get_legacy_traffic_store().purge()
    except LegacyStorePurgeError as e:
        raise HTTPException(status_code=500, detail=f"Legacy traffic DB purge failed: {e.reason}")
