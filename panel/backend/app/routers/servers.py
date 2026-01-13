from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update, desc
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, HttpUrl
from typing import Optional
import httpx
import json

from app.database import get_db
from app.models import Server, MetricsSnapshot
from app.auth import verify_auth

router = APIRouter(prefix="/servers", tags=["servers"])


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


class ServerCreate(BaseModel):
    name: str
    url: str
    api_key: str


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    api_key: Optional[str] = None
    is_active: Optional[bool] = None


class ServerReorder(BaseModel):
    server_ids: list[int]


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
    
    servers_data = []
    for s in servers:
        server_info = {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "position": s.position,
            "is_active": s.is_active,
            "last_seen": s.last_seen.isoformat() if s.last_seen else None,
            "last_error": s.last_error,
            "error_code": s.error_code
        }
        
        if include_metrics and s.last_metrics:
            try:
                metrics = json.loads(s.last_metrics)
                # Enrich metrics with calculated speeds from latest snapshot
                snapshot = await get_latest_snapshot(s.id, db)
                server_info["metrics"] = enrich_metrics_with_speeds(metrics, snapshot)
                server_info["status"] = "online"
            except json.JSONDecodeError:
                server_info["metrics"] = None
                server_info["status"] = "error" if s.last_error else "loading"
        elif include_metrics:
            server_info["metrics"] = None
            server_info["status"] = "error" if s.last_error else "loading"
        
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
        "last_seen": server.last_seen.isoformat() if server.last_seen else None,
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
            response = await client.get(
                f"{server.url}/health"
            )
            
            if response.status_code == 200:
                data = response.json()
                return {
                    "success": True,
                    "status": "online",
                    "server_name": data.get("server_name", "Unknown")
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
