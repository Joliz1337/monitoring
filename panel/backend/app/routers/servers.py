from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update, desc, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional
import httpx
import json

from app.database import get_db
from app.models import Server, MetricsSnapshot
from app.auth import verify_auth

router = APIRouter(prefix="/servers", tags=["servers"])


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


async def get_latest_snapshots_bulk(server_ids: list[int], db: AsyncSession) -> dict[int, MetricsSnapshot]:
    """Get the most recent metrics snapshot for multiple servers in one query."""
    if not server_ids:
        return {}
    
    # Subquery to get max timestamp per server
    subquery = (
        select(
            MetricsSnapshot.server_id,
            func.max(MetricsSnapshot.timestamp).label('max_ts')
        )
        .where(MetricsSnapshot.server_id.in_(server_ids))
        .group_by(MetricsSnapshot.server_id)
        .subquery()
    )
    
    # Join to get full snapshot records
    result = await db.execute(
        select(MetricsSnapshot)
        .join(
            subquery,
            and_(
                MetricsSnapshot.server_id == subquery.c.server_id,
                MetricsSnapshot.timestamp == subquery.c.max_ts
            )
        )
    )
    
    snapshots = result.scalars().all()
    return {s.server_id: s for s in snapshots}


class ServerCreate(BaseModel):
    name: str
    url: str
    api_key: str


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    api_key: Optional[str] = None
    is_active: Optional[bool] = None
    folder: Optional[str] = None


class ServerReorder(BaseModel):
    server_ids: list[int]


class MoveServersToFolder(BaseModel):
    server_ids: list[int]
    folder: Optional[str] = None


class RenameServerFolder(BaseModel):
    old_name: str
    new_name: str


class ServerResponse(BaseModel):
    id: int
    name: str
    url: str
    position: int
    is_active: bool

    class Config:
        from_attributes = True


@router.get("")
async def list_servers(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
    include_metrics: bool = False
):
    result = await db.execute(
        select(Server).order_by(Server.position, Server.id)
    )
    servers = result.scalars().all()
    
    # Get all snapshots in ONE query if metrics requested (fixes N+1 problem)
    snapshots_map = {}
    if include_metrics:
        server_ids = [s.id for s in servers]
        snapshots_map = await get_latest_snapshots_bulk(server_ids, db)
    
    servers_data = []
    for s in servers:
        server_info = {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "position": s.position,
            "is_active": s.is_active,
            "folder": s.folder,
            "last_seen": to_iso_utc(s.last_seen),
            "last_error": s.last_error,
            "error_code": s.error_code
        }
        
        if include_metrics and s.last_metrics:
            try:
                metrics = json.loads(s.last_metrics)
                # Get snapshot from bulk map instead of separate query
                snapshot = snapshots_map.get(s.id)
                server_info["metrics"] = enrich_metrics_with_speeds(metrics, snapshot)
                # Status depends on whether server has recent errors
                # If last_error is set, server is offline/error even if we have cached metrics
                if s.last_error:
                    server_info["status"] = "offline"
                else:
                    server_info["status"] = "online"
            except json.JSONDecodeError:
                server_info["metrics"] = None
                server_info["status"] = "error" if s.last_error else "loading"
        elif include_metrics:
            server_info["metrics"] = None
            server_info["status"] = "offline" if s.last_error else "loading"
        
        # Include cached traffic data if available
        if include_metrics and s.last_traffic_data:
            try:
                traffic_data = json.loads(s.last_traffic_data)
                summary = traffic_data.get("summary", {})
                total = summary.get("total", {})
                if total:
                    server_info["traffic"] = {
                        "rx_bytes": total.get("rx_bytes", 0),
                        "tx_bytes": total.get("tx_bytes", 0),
                        "days": total.get("days", 30)
                    }
            except json.JSONDecodeError:
                pass
        
        servers_data.append(server_info)
    
    return {
        "count": len(servers),
        "servers": servers_data
    }


@router.post("")
async def create_server(
    server: ServerCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    result = await db.execute(select(Server).order_by(Server.position.desc()))
    last_server = result.scalars().first()
    next_position = (last_server.position + 1) if last_server else 0
    
    new_server = Server(
        name=server.name,
        url=server.url.rstrip("/"),
        api_key=server.api_key,
        position=next_position
    )
    db.add(new_server)
    await db.commit()
    await db.refresh(new_server)
    
    return {
        "success": True,
        "server": {
            "id": new_server.id,
            "name": new_server.name,
            "url": new_server.url,
            "position": new_server.position
        }
    }


@router.post("/move-to-folder")
async def move_servers_to_folder(
    data: MoveServersToFolder,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    folder_value = data.folder.strip() if data.folder and data.folder.strip() else None
    result = await db.execute(select(Server).where(Server.id.in_(data.server_ids)))
    servers = result.scalars().all()
    for s in servers:
        s.folder = folder_value
    await db.commit()
    return {"success": True, "moved": len(servers)}


@router.post("/folders/rename")
async def rename_server_folder(
    data: RenameServerFolder,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    new_name = data.new_name.strip() if data.new_name else None
    if not new_name:
        raise HTTPException(400, "new_name is required")
    result = await db.execute(select(Server).where(Server.folder == data.old_name))
    servers = result.scalars().all()
    for s in servers:
        s.folder = new_name
    await db.commit()
    return {"success": True, "renamed": len(servers)}


@router.delete("/folders/{folder_name}")
async def delete_server_folder(
    folder_name: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    result = await db.execute(select(Server).where(Server.folder == folder_name))
    servers = result.scalars().all()
    for s in servers:
        s.folder = None
    await db.commit()
    return {"success": True, "unfoldered": len(servers)}


@router.get("/{server_id}")
async def get_server(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    
    if not server:
        raise HTTPException(status_code=404)
    
    return {
        "id": server.id,
        "name": server.name,
        "url": server.url,
        "api_key": server.api_key,
        "position": server.position,
        "is_active": server.is_active,
        "folder": server.folder,
        "last_seen": to_iso_utc(server.last_seen),
        "last_error": server.last_error,
        "error_code": server.error_code
    }


@router.put("/{server_id}")
async def update_server(
    server_id: int,
    data: ServerUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    
    if not server:
        raise HTTPException(status_code=404)
    
    update_data = data.model_dump(exclude_unset=True)
    if "url" in update_data:
        update_data["url"] = update_data["url"].rstrip("/")
    
    for key, value in update_data.items():
        setattr(server, key, value)
    
    await db.commit()
    
    return {"success": True, "message": "Server updated"}


@router.delete("/{server_id}")
async def delete_server(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    
    if not server:
        raise HTTPException(status_code=404)
    
    await db.delete(server)
    await db.commit()
    
    return {"success": True, "message": "Server deleted"}


@router.post("/reorder")
async def reorder_servers(
    data: ServerReorder,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    for position, server_id in enumerate(data.server_ids):
        await db.execute(
            update(Server).where(Server.id == server_id).values(position=position)
        )
    
    await db.commit()
    
    return {"success": True, "message": "Servers reordered"}


@router.post("/{server_id}/test")
async def test_server_connection(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    
    if not server:
        raise HTTPException(status_code=404)
    
    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            # Use /api/version with API key (not /health which is localhost-only)
            response = await client.get(
                f"{server.url}/api/version",
                headers={"X-API-Key": server.api_key}
            )
            
            if response.status_code == 200:
                data = response.json()
                return {
                    "success": True,
                    "status": "online",
                    "server_name": data.get("node_name", "Unknown"),
                    "version": data.get("version")
                }
            else:
                return {
                    "success": False,
                    "status": "error",
                    "message": f"HTTP {response.status_code}"
                }
    except httpx.TimeoutException:
        return {"success": False, "status": "timeout", "message": "Connection timeout"}
    except Exception as e:
        return {"success": False, "status": "error", "message": str(e)}
