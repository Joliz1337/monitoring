from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, Any, AsyncGenerator
from datetime import datetime, timedelta, timezone
import asyncio
import httpx
import json
import logging

from app.database import get_db
from app.models import Server, MetricsSnapshot, AggregatedMetrics
from app.auth import verify_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/proxy", tags=["proxy"])


def to_iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Convert datetime to ISO format with explicit UTC timezone suffix.
    
    All timestamps are stored as naive UTC, so we add 'Z' suffix for frontend.
    Truncates microseconds to milliseconds for better JS compatibility.
    """
    if dt is None:
        return None
    # Truncate to milliseconds (JS ISO format standard)
    dt = dt.replace(microsecond=(dt.microsecond // 1000) * 1000)
    # Format as ISO and append Z (all our times are UTC)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


async def get_server_by_id(server_id: int, db: AsyncSession) -> Server:
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404)
    return server


def enrich_metrics_with_speeds(metrics: dict, snapshot: MetricsSnapshot) -> dict:
    """Enrich raw metrics with calculated network/disk speeds from snapshot.
    
    Node returns raw bytes only, panel calculates speeds from byte differences.
    This function adds the calculated speeds to the metrics dict.
    """
    if not snapshot:
        return metrics
    
    # Enrich network metrics with calculated speeds
    if "network" in metrics:
        total_rx_speed = snapshot.net_rx_bytes_per_sec or 0
        total_tx_speed = snapshot.net_tx_bytes_per_sec or 0
        
        if "total" in metrics["network"]:
            metrics["network"]["total"]["rx_bytes_per_sec"] = total_rx_speed
            metrics["network"]["total"]["tx_bytes_per_sec"] = total_tx_speed
        
        # Distribute speed proportionally to interfaces based on bytes
        interfaces = metrics["network"].get("interfaces", [])
        if interfaces:
            total_rx_bytes = sum(i.get("rx_bytes", 0) for i in interfaces)
            total_tx_bytes = sum(i.get("tx_bytes", 0) for i in interfaces)
            
            for iface in interfaces:
                if total_rx_bytes > 0:
                    ratio = iface.get("rx_bytes", 0) / total_rx_bytes
                    iface["rx_bytes_per_sec"] = total_rx_speed * ratio
                if total_tx_bytes > 0:
                    ratio = iface.get("tx_bytes", 0) / total_tx_bytes
                    iface["tx_bytes_per_sec"] = total_tx_speed * ratio
    
    # Enrich disk metrics with calculated speeds
    if "disk" in metrics and "io" in metrics["disk"]:
        disk_read_speed = snapshot.disk_read_bytes_per_sec or 0
        disk_write_speed = snapshot.disk_write_bytes_per_sec or 0
        
        io_stats = metrics["disk"]["io"]
        if io_stats:
            total_read = sum(d.get("read_bytes", 0) for d in io_stats.values())
            total_write = sum(d.get("write_bytes", 0) for d in io_stats.values())
            
            for disk_name, disk_io in io_stats.items():
                if total_read > 0:
                    ratio = disk_io.get("read_bytes", 0) / total_read
                    disk_io["read_bytes_per_sec"] = disk_read_speed * ratio
                if total_write > 0:
                    ratio = disk_io.get("write_bytes", 0) / total_write
                    disk_io["write_bytes_per_sec"] = disk_write_speed * ratio
    
    return metrics


async def get_latest_snapshot(server_id: int, db: AsyncSession) -> Optional[MetricsSnapshot]:
    """Get the most recent metrics snapshot for a server."""
    result = await db.execute(
        select(MetricsSnapshot)
        .where(MetricsSnapshot.server_id == server_id)
        .order_by(desc(MetricsSnapshot.timestamp))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def proxy_request(
    server: Server,
    endpoint: str,
    method: str = "GET",
    json_data: dict = None,
    params: dict = None,
    timeout: float = 30.0
) -> dict:
    url = f"{server.url}{endpoint}"
    logger.info(f"Proxying {method} request to: {url}")
    
    try:
        async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
            headers = {"X-API-Key": server.api_key}
            
            if method == "GET":
                response = await client.get(url, headers=headers, params=params)
            elif method == "POST":
                response = await client.post(url, headers=headers, json=json_data, params=params)
            elif method == "PUT":
                response = await client.put(url, headers=headers, json=json_data)
            elif method == "DELETE":
                response = await client.delete(url, headers=headers)
            else:
                raise HTTPException(status_code=400)
            
            logger.info(f"Response from {url}: {response.status_code}")
            
            if response.status_code == 200:
                return response.json()
            else:
                raise HTTPException(status_code=response.status_code)
    except httpx.TimeoutException:
        logger.error(f"Timeout connecting to: {url}")
        raise HTTPException(status_code=504)
    except httpx.RequestError as e:
        logger.error(f"Connection error to {url}: {str(e)}")
        raise HTTPException(status_code=502)


@router.get("/{server_id}/metrics")
async def get_metrics(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get cached metrics from panel's database (collected by background worker).
    This avoids additional load on nodes when viewing dashboard.
    Enriches metrics with calculated network/disk speeds from latest snapshot.
    """
    server = await get_server_by_id(server_id, db)
    
    if server.last_metrics:
        try:
            metrics = json.loads(server.last_metrics)
            snapshot = await get_latest_snapshot(server_id, db)
            return enrich_metrics_with_speeds(metrics, snapshot)
        except json.JSONDecodeError:
            pass
    
    # No cached data
    raise HTTPException(status_code=503)


@router.get("/{server_id}/metrics/live")
async def get_live_metrics(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get live metrics directly from node (use sparingly, causes load on node).
    Enriches metrics with calculated network/disk speeds from latest snapshot.
    """
    server = await get_server_by_id(server_id, db)
    metrics, snapshot = await asyncio.gather(
        proxy_request(server, "/api/metrics"),
        get_latest_snapshot(server_id, db),
    )
    return enrich_metrics_with_speeds(metrics, snapshot)


@router.get("/{server_id}/metrics/history")
async def get_metrics_history(
    server_id: int,
    period: Optional[str] = Query(default="1h", description="Period: 1h, 24h, 7d, 30d, 365d"),
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    limit: int = Query(default=500, le=5000),
    include_per_cpu: bool = Query(default=False, description="Include per-CPU usage data"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get metrics history from panel's local database.
    
    Period determines data source:
    - 1h: raw data (5-second intervals) - ~720 points
    - 24h: raw data with downsampling (30-sec intervals) - ~2880 points max
    - 7d: hourly aggregated data - ~168 points
    - 30d, 365d: daily aggregated data
    
    Note: Uses naive UTC datetime (no timezone info stored in database).
    Set include_per_cpu=true to include per-CPU usage data (only for raw data periods).
    """
    await get_server_by_id(server_id, db)
    # Use naive UTC datetime
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    
    # Parse period to determine time range, data source, and max points for charts
    # max_points: target number of points for the chart (will downsample if more)
    period_config = {
        "1h": {"delta": timedelta(hours=1), "source": "raw", "max_points": 800},      # ~720 raw points
        "24h": {"delta": timedelta(hours=24), "source": "raw", "max_points": 1500},   # ~17k raw -> 1500
        "7d": {"delta": timedelta(days=7), "source": "hour", "max_points": 500},      # ~168 hourly
        "30d": {"delta": timedelta(days=30), "source": "day", "max_points": 500},     # ~30 daily
        "365d": {"delta": timedelta(days=365), "source": "day", "max_points": 500},   # ~365 daily
    }
    
    config = period_config.get(period, period_config["1h"])
    
    # Use explicit time range if provided, otherwise use period
    # Convert to naive UTC
    if to_time:
        try:
            parsed = datetime.fromisoformat(to_time.replace('Z', '+00:00'))
            if parsed.tzinfo:
                end_time = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            else:
                end_time = parsed
        except ValueError:
            end_time = now
    else:
        end_time = now
    
    if from_time:
        try:
            parsed = datetime.fromisoformat(from_time.replace('Z', '+00:00'))
            if parsed.tzinfo:
                start_time = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            else:
                start_time = parsed
        except ValueError:
            start_time = now - config["delta"]
    else:
        start_time = now - config["delta"]
    
    data_source = config["source"]
    max_points = config.get("max_points", limit)
    
    if data_source == "raw":
        count_result = await db.execute(
            select(func.count(MetricsSnapshot.id))
            .where(MetricsSnapshot.server_id == server_id)
            .where(MetricsSnapshot.timestamp >= start_time)
            .where(MetricsSnapshot.timestamp <= end_time)
        )
        total_count = count_result.scalar() or 0

        if total_count <= max_points:
            result = await db.execute(
                select(MetricsSnapshot)
                .where(MetricsSnapshot.server_id == server_id)
                .where(MetricsSnapshot.timestamp >= start_time)
                .where(MetricsSnapshot.timestamp <= end_time)
                .order_by(MetricsSnapshot.timestamp)
            )
            snapshots = result.scalars().all()
        else:
            step = total_count // max_points
            numbered = (
                select(
                    MetricsSnapshot.id.label("ms_id"),
                    func.row_number().over(
                        order_by=MetricsSnapshot.timestamp
                    ).label("rn")
                )
                .where(MetricsSnapshot.server_id == server_id)
                .where(MetricsSnapshot.timestamp >= start_time)
                .where(MetricsSnapshot.timestamp <= end_time)
                .subquery()
            )
            result = await db.execute(
                select(MetricsSnapshot)
                .join(numbered, MetricsSnapshot.id == numbered.c.ms_id)
                .where(numbered.c.rn % step == 1)
                .order_by(MetricsSnapshot.timestamp)
            )
            snapshots = result.scalars().all()
        
        def build_snapshot_dict(s: MetricsSnapshot) -> dict:
            result = {
                "timestamp": to_iso_utc(s.timestamp),
                "cpu_usage": s.cpu_usage,
                "max_cpu": s.cpu_usage,  # Same as avg for raw data
                "load_avg_1": s.load_avg_1,
                "memory_used": s.memory_used,
                "memory_available": s.memory_available,
                "memory_percent": s.memory_percent,
                "max_memory_percent": s.memory_percent,
                "swap_used": s.swap_used,
                "net_rx_bytes_per_sec": s.net_rx_bytes_per_sec or 0,
                "net_tx_bytes_per_sec": s.net_tx_bytes_per_sec or 0,
                "disk_percent": s.disk_percent,
                "disk_read_bytes_per_sec": s.disk_read_bytes_per_sec or 0,
                "disk_write_bytes_per_sec": s.disk_write_bytes_per_sec or 0,
                "process_count": s.process_count,
                "tcp_established": s.tcp_established,
                "tcp_listen": s.tcp_listen,
                "tcp_time_wait": s.tcp_time_wait,
                "tcp_close_wait": s.tcp_close_wait,
                "tcp_syn_sent": s.tcp_syn_sent,
                "tcp_syn_recv": s.tcp_syn_recv,
                "tcp_fin_wait": s.tcp_fin_wait,
            }
            if include_per_cpu and s.per_cpu_percent:
                try:
                    result["per_cpu_percent"] = json.loads(s.per_cpu_percent)
                except json.JSONDecodeError:
                    pass
            return result
        
        data = [build_snapshot_dict(s) for s in snapshots]
    else:
        # Query aggregated metrics (hourly or daily) - ascending order for charts
        result = await db.execute(
            select(AggregatedMetrics)
            .where(AggregatedMetrics.server_id == server_id)
            .where(AggregatedMetrics.period_type == data_source)
            .where(AggregatedMetrics.timestamp >= start_time)
            .where(AggregatedMetrics.timestamp <= end_time)
            .order_by(AggregatedMetrics.timestamp)  # Ascending order
        )
        aggregated = result.scalars().all()
        
        # Apply downsampling if needed
        total_count = len(aggregated)
        if total_count > max_points:
            step = total_count // max_points
            if step > 1:
                aggregated = aggregated[::step]
        
        data = [
            {
                "timestamp": to_iso_utc(a.timestamp),
                "cpu_usage": a.avg_cpu,
                "max_cpu": a.max_cpu,
                "load_avg_1": a.avg_load,
                "memory_percent": a.avg_memory_percent,
                "max_memory_percent": a.max_memory_percent,
                "disk_percent": a.avg_disk_percent,
                "net_rx_bytes_per_sec": a.avg_rx_speed or 0,
                "net_tx_bytes_per_sec": a.avg_tx_speed or 0,
                "total_rx_bytes": a.total_rx_bytes or 0,
                "total_tx_bytes": a.total_tx_bytes or 0,
                "disk_read_bytes_per_sec": a.avg_disk_read_speed or 0,
                "disk_write_bytes_per_sec": a.avg_disk_write_speed or 0,
                "data_points": a.data_points,
                "tcp_established": round(a.avg_tcp_established) if a.avg_tcp_established is not None else None,
                "tcp_listen": round(a.avg_tcp_listen) if a.avg_tcp_listen is not None else None,
                "tcp_time_wait": round(a.avg_tcp_time_wait) if a.avg_tcp_time_wait is not None else None,
                "tcp_close_wait": round(a.avg_tcp_close_wait) if a.avg_tcp_close_wait is not None else None,
                "tcp_syn_sent": round(a.avg_tcp_syn_sent) if a.avg_tcp_syn_sent is not None else None,
                "tcp_syn_recv": round(a.avg_tcp_syn_recv) if a.avg_tcp_syn_recv is not None else None,
                "tcp_fin_wait": round(a.avg_tcp_fin_wait) if a.avg_tcp_fin_wait is not None else None,
            }
            for a in aggregated
        ]
    
    return {
        "period": period,
        "data_source": data_source,
        "from_time": to_iso_utc(start_time),
        "to_time": to_iso_utc(end_time),
        "count": len(data),
        "data": data
    }


@router.get("/{server_id}/haproxy/cached")
async def get_haproxy_cached(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get cached HAProxy data (status, rules, certs, firewall) from panel's database.
    Updated by background collector every 30 seconds.
    """
    server = await get_server_by_id(server_id, db)
    
    if server.last_haproxy_data:
        try:
            return json.loads(server.last_haproxy_data)
        except json.JSONDecodeError:
            pass
    
    raise HTTPException(status_code=503)


@router.get("/{server_id}/traffic/cached")
async def get_traffic_cached(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get cached Traffic data (summary, tracked_ports) from panel's database.
    Updated by background collector every 30 seconds.
    """
    server = await get_server_by_id(server_id, db)
    
    if server.last_traffic_data:
        try:
            return json.loads(server.last_traffic_data)
        except json.JSONDecodeError:
            pass
    
    raise HTTPException(status_code=503)


@router.get("/{server_id}/haproxy/status")
async def get_haproxy_status(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/status")


@router.get("/{server_id}/haproxy/rules")
async def get_haproxy_rules(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/rules")


@router.get("/{server_id}/haproxy/rules/{name}")
async def get_haproxy_rule(
    server_id: int,
    name: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, f"/api/haproxy/rules/{name}")


@router.post("/{server_id}/haproxy/rules")
async def create_haproxy_rule(
    server_id: int,
    rule: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/rules", method="POST", json_data=rule)


@router.put("/{server_id}/haproxy/rules/{name}")
async def update_haproxy_rule(
    server_id: int,
    name: str,
    rule: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, f"/api/haproxy/rules/{name}", method="PUT", json_data=rule)


@router.delete("/{server_id}/haproxy/rules/{name}")
async def delete_haproxy_rule(
    server_id: int,
    name: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, f"/api/haproxy/rules/{name}", method="DELETE")


@router.post("/{server_id}/haproxy/reload")
async def reload_haproxy(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/reload", method="POST")


@router.post("/{server_id}/haproxy/restart")
async def restart_haproxy(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/restart", method="POST")


@router.post("/{server_id}/haproxy/start")
async def start_haproxy(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/start", method="POST")


@router.post("/{server_id}/haproxy/stop")
async def stop_haproxy(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/stop", method="POST")


@router.post("/{server_id}/haproxy/validate")
async def validate_haproxy(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/validate", method="POST")


@router.get("/{server_id}/haproxy/config")
async def get_haproxy_config(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/config")


@router.post("/{server_id}/haproxy/config/apply")
async def apply_haproxy_config(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Apply HAProxy config from panel (node just saves and reloads)"""
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/config/apply", method="POST", json_data=data)


@router.get("/{server_id}/haproxy/certs")
async def get_haproxy_certs(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/certs")


@router.get("/{server_id}/haproxy/certs/all")
async def get_all_certs(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/certs/all")


@router.get("/{server_id}/haproxy/certs/{domain}")
async def get_haproxy_cert(
    server_id: int,
    domain: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, f"/api/haproxy/certs/{domain}")


@router.post("/{server_id}/haproxy/certs/generate")
async def generate_cert(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    # Certificate generation can take 2-3 minutes
    return await proxy_request(server, "/api/haproxy/certs/generate", method="POST", json_data=data, timeout=300.0)


@router.post("/{server_id}/haproxy/certs/renew")
async def renew_certs(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    # Certificate renewal can take 2+ minutes per certificate
    return await proxy_request(server, "/api/haproxy/certs/renew", method="POST", timeout=300.0)


@router.post("/{server_id}/haproxy/certs/{domain}/renew")
async def renew_single_cert(
    server_id: int,
    domain: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    # Certificate renewal can take 2-3 minutes
    return await proxy_request(server, f"/api/haproxy/certs/{domain}/renew", method="POST", timeout=300.0)


@router.delete("/{server_id}/haproxy/certs/{domain}")
async def delete_cert(
    server_id: int,
    domain: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, f"/api/haproxy/certs/{domain}", method="DELETE")


@router.post("/{server_id}/haproxy/certs/upload")
async def upload_cert(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/certs/upload", method="POST", json_data=data)


# ==================== Firewall Management ====================

@router.get("/{server_id}/haproxy/firewall/status")
async def get_firewall_status(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/firewall/status")


@router.get("/{server_id}/haproxy/firewall/rules")
async def get_firewall_rules(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/firewall/rules")


@router.post("/{server_id}/haproxy/firewall/allow")
async def allow_port(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/firewall/allow", method="POST", json_data=data)


@router.post("/{server_id}/haproxy/firewall/deny")
async def deny_port(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/firewall/deny", method="POST", json_data=data)


@router.post("/{server_id}/haproxy/firewall/rule")
async def add_firewall_rule(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/firewall/rule", method="POST", json_data=data)


@router.delete("/{server_id}/haproxy/firewall/rule/{rule_number}")
async def delete_firewall_rule_by_number(
    server_id: int,
    rule_number: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, f"/api/haproxy/firewall/rule/{rule_number}", method="DELETE")


@router.delete("/{server_id}/haproxy/firewall/{port}")
async def delete_firewall_rule(
    server_id: int,
    port: int,
    protocol: str = Query(default="tcp"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, f"/api/haproxy/firewall/{port}", method="DELETE", params={"protocol": protocol})


@router.post("/{server_id}/haproxy/firewall/enable")
async def enable_firewall(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/firewall/enable", method="POST")


@router.post("/{server_id}/haproxy/firewall/disable")
async def disable_firewall(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/firewall/disable", method="POST")


# ==================== System Optimization ====================

@router.get("/{server_id}/haproxy/system/info")
async def get_system_info(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/system/info")


# ==================== Traffic Tracking ====================

@router.get("/{server_id}/traffic/summary")
async def get_traffic_summary(
    server_id: int,
    days: int = Query(default=30, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/traffic/summary", params={"days": days})


@router.get("/{server_id}/traffic/hourly")
async def get_hourly_traffic(
    server_id: int,
    hours: int = Query(default=24, ge=1, le=168),
    interface: Optional[str] = None,
    port: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    params = {"hours": hours}
    if interface:
        params["interface"] = interface
    if port:
        params["port"] = port
    return await proxy_request(server, "/api/traffic/hourly", params=params)


@router.get("/{server_id}/traffic/daily")
async def get_daily_traffic(
    server_id: int,
    days: int = Query(default=30, ge=1, le=90),
    interface: Optional[str] = None,
    port: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    params = {"days": days}
    if interface:
        params["interface"] = interface
    if port:
        params["port"] = port
    return await proxy_request(server, "/api/traffic/daily", params=params)


@router.get("/{server_id}/traffic/monthly")
async def get_monthly_traffic(
    server_id: int,
    months: int = Query(default=12, ge=1, le=24),
    interface: Optional[str] = None,
    port: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    params = {"months": months}
    if interface:
        params["interface"] = interface
    if port:
        params["port"] = port
    return await proxy_request(server, "/api/traffic/monthly", params=params)


@router.get("/{server_id}/traffic/ports")
async def get_ports_traffic(
    server_id: int,
    days: int = Query(default=30, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/traffic/ports", params={"days": days})


@router.get("/{server_id}/traffic/interfaces")
async def get_interfaces_traffic(
    server_id: int,
    days: int = Query(default=30, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/traffic/interfaces", params={"days": days})


@router.get("/{server_id}/traffic/ports/tracked")
async def get_tracked_ports(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/traffic/ports/tracked")


@router.post("/{server_id}/traffic/ports/add")
async def add_tracked_port(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/traffic/ports/add", method="POST", json_data=data)


@router.post("/{server_id}/traffic/ports/remove")
async def remove_tracked_port(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/traffic/ports/remove", method="POST", json_data=data)


# ==================== System / Updates ====================

@router.get("/{server_id}/system/version")
async def get_node_version(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get node version information"""
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/version")


@router.post("/{server_id}/system/update")
async def trigger_node_update(
    server_id: int,
    data: dict = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """
    Trigger node update.
    Optional data: { "target_version": "v1.1.0" }
    If not specified, updates to latest version.
    """
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/system/update", method="POST", json_data=data or {})


@router.get("/{server_id}/system/update/status")
async def get_node_update_status(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get node update status"""
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/system/update/status")


@router.post("/{server_id}/system/execute")
async def execute_command_on_node(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """
    Execute a shell command on the node's host system.
    
    Uses nsenter to run commands in the host namespace from Docker container.
    
    Request body:
        command: str - Shell command to execute (required)
        timeout: int - Timeout in seconds, 1-600 (default: 30)
        shell: str - Shell to use: "sh" or "bash" (default: "sh")
    
    Response:
        success: bool - Whether command exited with code 0
        exit_code: int - Command exit code
        stdout: str - Standard output
        stderr: str - Standard error
        execution_time_ms: int - Execution time in milliseconds
        error: str | null - Error message if execution failed
    
    Examples:
        {"command": "sysctl -p /etc/sysctl.d/99-network-tuning.conf"}
        {"command": "systemctl restart nginx", "timeout": 60}
        {"command": "cat /etc/os-release && uname -a", "shell": "bash"}
    """
    server = await get_server_by_id(server_id, db)
    # Use longer timeout for potentially long-running commands
    request_timeout = min((data.get("timeout", 30) or 30) + 10, 620)
    return await proxy_request(
        server,
        "/api/system/execute",
        method="POST",
        json_data=data,
        timeout=float(request_timeout)
    )


@router.post("/{server_id}/system/execute-stream")
async def execute_command_stream_on_node(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """
    Execute a shell command on the node's host system with streaming output (SSE).
    
    Returns Server-Sent Events with real-time stdout/stderr output.
    Proxies SSE stream from the node to the client.
    
    SSE Event types:
        - stdout: {"line": "output line"}
        - stderr: {"line": "error line"}
        - done: {"exit_code": 0, "execution_time_ms": 1234, "success": true}
        - error: {"message": "error description"}
    
    Request body:
        command: str - Shell command to execute (required)
        timeout: int - Timeout in seconds, 1-600 (default: 30)
        shell: str - Shell to use: "sh" or "bash" (default: "sh")
    """
    server = await get_server_by_id(server_id, db)
    url = f"{server.url}/api/system/execute-stream"
    request_timeout = min((data.get("timeout", 30) or 30) + 15, 620)
    
    async def stream_proxy() -> AsyncGenerator[bytes, None]:
        try:
            async with httpx.AsyncClient(verify=False, timeout=request_timeout) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers={"X-API-Key": server.api_key},
                    json=data
                ) as response:
                    if response.status_code != 200:
                        error_event = f'event: error\ndata: {{"message": "Node returned status {response.status_code}"}}\n\n'
                        yield error_event.encode()
                        return
                    
                    async for chunk in response.aiter_bytes():
                        yield chunk
        except httpx.TimeoutException:
            error_event = 'event: error\ndata: {"message": "Connection to node timed out"}\n\n'
            yield error_event.encode()
        except httpx.RequestError as e:
            error_event = f'event: error\ndata: {{"message": "Connection error: {str(e)}"}}\n\n'
            yield error_event.encode()
        except Exception as e:
            logger.error(f"SSE proxy error: {e}")
            error_event = f'event: error\ndata: {{"message": "Proxy error: {str(e)}"}}\n\n'
            yield error_event.encode()
    
    return StreamingResponse(
        stream_proxy(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# ==================== System Optimizations ====================

@router.get("/{server_id}/system/optimizations/version")
async def get_node_optimizations_version(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get node system optimizations version"""
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/system/optimizations/version")


@router.post("/{server_id}/system/optimizations/apply")
async def apply_node_optimizations(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """
    Apply system optimizations to a node.
    
    Fetches latest configs from GitHub and applies them to the node.
    """
    import httpx as httpx_client
    
    server = await get_server_by_id(server_id, db)
    
    # Fetch configs from GitHub via system router
    from app.routers.system import get_optimizations_from_github
    github_data = await get_optimizations_from_github()
    
    if not github_data.get("sysctl_content"):
        raise HTTPException(status_code=502, detail="Failed to fetch configs from GitHub")
    
    # Apply to node (include version for tracking)
    apply_data = {
        "sysctl_content": github_data["sysctl_content"],
        "limits_content": github_data["limits_content"],
        "systemd_content": github_data["systemd_content"],
        "network_tune_content": github_data.get("network_tune_content"),
        "network_tune_service_content": github_data.get("network_tune_service_content"),
        "version": github_data.get("version")
    }
    
    return await proxy_request(
        server,
        "/api/system/optimizations/apply",
        method="POST",
        json_data=apply_data,
        timeout=60.0
    )


# ==================== IPSet Management ====================

@router.get("/{server_id}/ipset/status")
async def get_ipset_status(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get ipset status from node"""
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/ipset/status")


@router.get("/{server_id}/ipset/list/{set_type}")
async def get_ipset_list(
    server_id: int,
    set_type: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get IPs from ipset list (permanent or temp)"""
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, f"/api/ipset/list/{set_type}")


@router.post("/{server_id}/ipset/add")
async def add_to_ipset(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add IP/CIDR to ipset"""
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/ipset/add", method="POST", json_data=data)


@router.post("/{server_id}/ipset/bulk-add")
async def bulk_add_to_ipset(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add multiple IPs to ipset"""
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/ipset/bulk-add", method="POST", json_data=data, timeout=120.0)


@router.delete("/{server_id}/ipset/remove")
async def remove_from_ipset(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Remove IP/CIDR from ipset"""
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/ipset/remove", method="DELETE", json_data=data)


@router.post("/{server_id}/ipset/bulk-remove")
async def bulk_remove_from_ipset(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Remove multiple IPs from ipset"""
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/ipset/bulk-remove", method="POST", json_data=data, timeout=120.0)


@router.post("/{server_id}/ipset/clear/{set_type}")
async def clear_ipset(
    server_id: int,
    set_type: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Clear all IPs from ipset list"""
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, f"/api/ipset/clear/{set_type}", method="POST")


@router.put("/{server_id}/ipset/timeout")
async def set_ipset_timeout(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Set timeout for temp ipset list"""
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/ipset/timeout", method="PUT", json_data=data)


@router.post("/{server_id}/ipset/sync")
async def sync_ipset(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Sync (replace) entire ipset list"""
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/ipset/sync", method="POST", json_data=data, timeout=120.0)
