"""Traffic statistics API endpoints."""

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services.traffic_collector import get_traffic_collector

router = APIRouter(prefix="/api/traffic", tags=["traffic"])


class PortRequest(BaseModel):
    port: int


@router.get("/hourly")
async def get_hourly_traffic(
    hours: int = Query(24, ge=1, le=168, description="Number of hours (max 168 = 7 days)"),
    interface: Optional[str] = Query(None, description="Filter by interface name"),
    port: Optional[int] = Query(None, description="Filter by port number")
):
    """Get hourly traffic statistics."""
    collector = get_traffic_collector()
    data = await collector.get_hourly_traffic(hours=hours, interface=interface, port=port)
    total_rx = sum(d["rx_bytes"] for d in data)
    total_tx = sum(d["tx_bytes"] for d in data)
    return {
        "hours": hours,
        "interface": interface,
        "port": port,
        "data": data,
        "total_rx": total_rx,
        "total_tx": total_tx
    }


@router.get("/daily")
async def get_daily_traffic(
    days: int = Query(30, ge=1, le=90, description="Number of days (max 90)"),
    interface: Optional[str] = Query(None, description="Filter by interface name"),
    port: Optional[int] = Query(None, description="Filter by port number")
):
    """Get daily traffic statistics."""
    collector = get_traffic_collector()
    data = await collector.get_daily_traffic(days=days, interface=interface, port=port)
    total_rx = sum(d["rx_bytes"] for d in data)
    total_tx = sum(d["tx_bytes"] for d in data)
    return {
        "days": days,
        "interface": interface,
        "port": port,
        "data": data,
        "total_rx": total_rx,
        "total_tx": total_tx
    }


@router.get("/monthly")
async def get_monthly_traffic(
    months: int = Query(12, ge=1, le=24, description="Number of months (max 24)"),
    interface: Optional[str] = Query(None, description="Filter by interface name"),
    port: Optional[int] = Query(None, description="Filter by port number")
):
    """Get monthly traffic statistics."""
    collector = get_traffic_collector()
    data = await collector.get_monthly_traffic(months=months, interface=interface, port=port)
    total_rx = sum(d["rx_bytes"] for d in data)
    total_tx = sum(d["tx_bytes"] for d in data)
    return {
        "months": months,
        "interface": interface,
        "port": port,
        "data": data,
        "total_rx": total_rx,
        "total_tx": total_tx
    }


@router.get("/summary")
async def get_traffic_summary(
    days: int = Query(30, ge=1, le=90, description="Number of days for summary")
):
    """Get traffic summary: total, per interface, and per port."""
    collector = get_traffic_collector()
    
    total = await collector.get_total_traffic(days=days)
    interfaces = await collector.get_interface_summary(days=days)
    ports = await collector.get_port_summary(days=days)
    
    return {
        "days": days,
        "total": total,
        "by_interface": interfaces,
        "by_port": ports,
        "tracked_ports": collector.get_tracked_ports()
    }


@router.get("/ports")
async def get_ports_traffic(
    days: int = Query(30, ge=1, le=90, description="Number of days")
):
    """Get traffic breakdown by port."""
    collector = get_traffic_collector()
    ports = await collector.get_port_summary(days=days)
    tracked = collector.get_tracked_ports()
    
    return {
        "days": days,
        "tracked_ports": tracked,
        "data": ports,
        "total_rx": sum(p["rx_bytes"] for p in ports),
        "total_tx": sum(p["tx_bytes"] for p in ports)
    }


@router.get("/interfaces")
async def get_interfaces_traffic(
    days: int = Query(30, ge=1, le=90, description="Number of days")
):
    """Get traffic breakdown by interface."""
    collector = get_traffic_collector()
    interfaces = await collector.get_interface_summary(days=days)
    
    return {
        "days": days,
        "data": interfaces,
        "total_rx": sum(i["rx_bytes"] for i in interfaces),
        "total_tx": sum(i["tx_bytes"] for i in interfaces)
    }


# Port management endpoints
@router.get("/ports/tracked")
async def get_tracked_ports():
    """Get list of ports being tracked."""
    collector = get_traffic_collector()
    return {
        "tracked_ports": collector.get_tracked_ports()
    }


@router.post("/ports/add")
async def add_tracked_port(request: PortRequest):
    """Add a port to traffic tracking."""
    collector = get_traffic_collector()
    result = await collector.add_tracked_port(request.port)
    return result


@router.post("/ports/remove")
async def remove_tracked_port(request: PortRequest):
    """Remove a port from traffic tracking."""
    collector = get_traffic_collector()
    result = await collector.remove_tracked_port(request.port)
    return result
