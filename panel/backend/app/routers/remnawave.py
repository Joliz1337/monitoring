"""Remnawave integration router for Xray visit statistics"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func as sql_func, and_, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import (
    Server, RemnawaveSettings, RemnawaveNode, 
    XrayVisitStats, RemnawaveUserCache
)
from app.services.remnawave_api import get_remnawave_api
from app.services.xray_stats_collector import get_xray_stats_collector

router = APIRouter(prefix="/remnawave", tags=["remnawave"])


# === Request/Response Models ===

class UpdateSettingsRequest(BaseModel):
    api_url: Optional[str] = Field(None, max_length=500)
    api_token: Optional[str] = Field(None, max_length=500)
    cookie_secret: Optional[str] = Field(None, max_length=500)
    enabled: Optional[bool] = None
    collection_interval: Optional[int] = Field(None, ge=10, le=3600)


class AddNodeRequest(BaseModel):
    server_id: int


class AddNodesRequest(BaseModel):
    server_ids: list[int]


class SyncNodesRequest(BaseModel):
    server_ids: list[int]


class NodeResponse(BaseModel):
    id: int
    server_id: int
    server_name: str
    enabled: bool
    last_collected: Optional[str]
    last_error: Optional[str]


# === Settings Endpoints ===

@router.get("/settings")
async def get_settings(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get Remnawave settings"""
    result = await db.execute(select(RemnawaveSettings).limit(1))
    settings = result.scalar_one_or_none()
    
    if not settings:
        return {
            "api_url": None,
            "api_token": None,
            "cookie_secret": None,
            "enabled": False,
            "collection_interval": 60
        }
    
    return {
        "api_url": settings.api_url,
        "api_token": "***" if settings.api_token else None,  # Hide token
        "cookie_secret": "***" if settings.cookie_secret else None,  # Hide secret
        "enabled": settings.enabled,
        "collection_interval": settings.collection_interval
    }


@router.put("/settings")
async def update_settings(
    request: UpdateSettingsRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Update Remnawave settings"""
    result = await db.execute(select(RemnawaveSettings).limit(1))
    settings = result.scalar_one_or_none()
    
    if not settings:
        settings = RemnawaveSettings()
        db.add(settings)
    
    if request.api_url is not None:
        settings.api_url = request.api_url
    if request.api_token is not None:
        settings.api_token = request.api_token
    if request.cookie_secret is not None:
        settings.cookie_secret = request.cookie_secret
    if request.enabled is not None:
        settings.enabled = request.enabled
    if request.collection_interval is not None:
        settings.collection_interval = request.collection_interval
    
    await db.commit()
    
    return {"success": True, "message": "Settings updated"}


@router.post("/settings/test")
async def test_connection(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Test connection to Remnawave API"""
    result = await db.execute(select(RemnawaveSettings).limit(1))
    settings = result.scalar_one_or_none()
    
    if not settings or not settings.api_url or not settings.api_token:
        return {
            "success": False,
            "error": "API URL and token not configured"
        }
    
    api = get_remnawave_api(settings.api_url, settings.api_token, settings.cookie_secret)
    
    try:
        result = await api.check_connection()
        return {
            "success": result.get("auth_valid", False),
            "api_reachable": result.get("api_reachable", False),
            "error": result.get("error")
        }
    finally:
        await api.close()


# === Collector Status & Control ===

@router.get("/status")
async def get_collector_status(
    _: dict = Depends(verify_auth)
):
    """Get Xray stats collector status"""
    collector = get_xray_stats_collector()
    return collector.get_status()


@router.post("/collect")
async def force_collect(
    _: dict = Depends(verify_auth)
):
    """Force immediate collection from all nodes"""
    collector = get_xray_stats_collector()
    return await collector.collect_now()


# === Nodes Endpoints ===

@router.get("/nodes")
async def get_nodes(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get list of Remnawave nodes and all servers"""
    # Get existing nodes with server info
    result = await db.execute(
        select(RemnawaveNode, Server)
        .join(Server, RemnawaveNode.server_id == Server.id)
        .order_by(Server.name)
    )
    rows = result.all()
    
    # Get all servers
    all_servers = await db.execute(select(Server).order_by(Server.name))
    servers = all_servers.scalars().all()
    
    # Build node map for quick lookup
    node_map = {row[0].server_id: row[0] for row in rows}
    
    return {
        "nodes": [
            {
                "id": node.id,
                "server_id": node.server_id,
                "server_name": server.name,
                "enabled": node.enabled,
                "last_collected": node.last_collected.isoformat() if node.last_collected else None,
                "last_error": node.last_error
            }
            for node, server in rows
        ],
        "all_servers": [
            {
                "id": s.id,
                "name": s.name,
                "is_active": s.is_active,
                "is_node": s.id in node_map,
                "node_enabled": node_map[s.id].enabled if s.id in node_map else False
            }
            for s in servers
        ]
    }


@router.post("/nodes")
async def add_node(
    request: AddNodeRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add a server as Remnawave node"""
    # Check server exists
    server = await db.execute(select(Server).where(Server.id == request.server_id))
    if not server.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Server not found")
    
    # Check not already added
    existing = await db.execute(
        select(RemnawaveNode).where(RemnawaveNode.server_id == request.server_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Server already added as Remnawave node")
    
    node = RemnawaveNode(server_id=request.server_id, enabled=True)
    db.add(node)
    await db.commit()
    
    return {"success": True, "message": "Node added"}


@router.delete("/nodes/{server_id}")
async def remove_node(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Remove a Remnawave node"""
    await db.execute(
        delete(RemnawaveNode).where(RemnawaveNode.server_id == server_id)
    )
    await db.commit()
    
    return {"success": True, "message": "Node removed"}


@router.post("/nodes/sync")
async def sync_nodes(
    request: SyncNodesRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Sync Remnawave nodes - add/remove to match provided server_ids list"""
    # Get current nodes
    result = await db.execute(select(RemnawaveNode))
    current_nodes = {n.server_id: n for n in result.scalars().all()}
    
    # Validate all server_ids exist
    if request.server_ids:
        servers_result = await db.execute(
            select(Server.id).where(Server.id.in_(request.server_ids))
        )
        existing_server_ids = {s[0] for s in servers_result.fetchall()}
        invalid_ids = set(request.server_ids) - existing_server_ids
        if invalid_ids:
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid server IDs: {list(invalid_ids)}"
            )
    
    new_server_ids = set(request.server_ids)
    current_server_ids = set(current_nodes.keys())
    
    # Remove nodes not in new list
    to_remove = current_server_ids - new_server_ids
    if to_remove:
        await db.execute(
            delete(RemnawaveNode).where(RemnawaveNode.server_id.in_(to_remove))
        )
    
    # Add new nodes
    to_add = new_server_ids - current_server_ids
    for server_id in to_add:
        db.add(RemnawaveNode(server_id=server_id, enabled=True))
    
    await db.commit()
    
    return {
        "success": True,
        "added": len(to_add),
        "removed": len(to_remove),
        "total": len(new_server_ids)
    }


@router.put("/nodes/{server_id}")
async def update_node(
    server_id: int,
    enabled: bool = Query(...),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Enable/disable a Remnawave node"""
    result = await db.execute(
        select(RemnawaveNode).where(RemnawaveNode.server_id == server_id)
    )
    node = result.scalar_one_or_none()
    
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    
    node.enabled = enabled
    await db.commit()
    
    return {"success": True, "message": "Node updated"}


# === Statistics Endpoints ===

def _get_period_filter(period: str):
    """Get datetime filter for period."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    
    if period == "1h":
        return now - timedelta(hours=1), 'hour'
    elif period == "24h":
        return now - timedelta(hours=24), 'hour'
    elif period == "7d":
        return now - timedelta(days=7), 'hour'
    elif period == "30d":
        return now - timedelta(days=30), 'day'
    elif period == "365d":
        return now - timedelta(days=365), 'day'
    else:
        return now - timedelta(hours=24), 'hour'


@router.get("/stats/summary")
async def get_stats_summary(
    period: str = Query("24h", regex="^(1h|24h|7d|30d|365d)$"),
    server_ids: Optional[str] = Query(None, description="Comma-separated server IDs"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get summary statistics"""
    start_time, period_type = _get_period_filter(period)
    
    # Build query
    conditions = [
        XrayVisitStats.period_start >= start_time
    ]
    
    # For short periods use hourly, for long use both
    if period in ["1h", "24h", "7d"]:
        conditions.append(XrayVisitStats.period_type == 'hour')
    else:
        conditions.append(XrayVisitStats.period_type == 'day')
    
    if server_ids:
        ids = [int(x.strip()) for x in server_ids.split(",") if x.strip().isdigit()]
        if ids:
            conditions.append(XrayVisitStats.server_id.in_(ids))
    
    # Total visits
    total_result = await db.execute(
        select(sql_func.sum(XrayVisitStats.visit_count))
        .where(and_(*conditions))
    )
    total_visits = total_result.scalar() or 0
    
    # Unique users
    users_result = await db.execute(
        select(sql_func.count(sql_func.distinct(XrayVisitStats.email)))
        .where(and_(*conditions))
    )
    unique_users = users_result.scalar() or 0
    
    # Unique destinations
    dest_result = await db.execute(
        select(sql_func.count(sql_func.distinct(XrayVisitStats.destination)))
        .where(and_(*conditions))
    )
    unique_destinations = dest_result.scalar() or 0
    
    return {
        "period": period,
        "total_visits": total_visits,
        "unique_users": unique_users,
        "unique_destinations": unique_destinations
    }


@router.get("/stats/top-destinations")
async def get_top_destinations(
    period: str = Query("24h", regex="^(1h|24h|7d|30d|365d)$"),
    limit: int = Query(50, ge=1, le=500),
    email: Optional[int] = Query(None, description="Filter by user email/ID"),
    server_id: Optional[int] = Query(None, description="Filter by server"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get top visited destinations"""
    start_time, period_type = _get_period_filter(period)
    
    conditions = [XrayVisitStats.period_start >= start_time]
    
    if period in ["1h", "24h", "7d"]:
        conditions.append(XrayVisitStats.period_type == 'hour')
    else:
        conditions.append(XrayVisitStats.period_type == 'day')
    
    if email:
        conditions.append(XrayVisitStats.email == email)
    if server_id:
        conditions.append(XrayVisitStats.server_id == server_id)
    
    result = await db.execute(
        select(
            XrayVisitStats.destination,
            XrayVisitStats.destination_domain,
            sql_func.sum(XrayVisitStats.visit_count).label('total')
        )
        .where(and_(*conditions))
        .group_by(XrayVisitStats.destination, XrayVisitStats.destination_domain)
        .order_by(sql_func.sum(XrayVisitStats.visit_count).desc())
        .limit(limit)
    )
    
    rows = result.fetchall()
    
    return {
        "period": period,
        "destinations": [
            {
                "destination": row.destination,
                "domain": row.destination_domain,
                "visits": row.total
            }
            for row in rows
        ]
    }


@router.get("/stats/top-users")
async def get_top_users(
    period: str = Query("24h", regex="^(1h|24h|7d|30d|365d)$"),
    limit: int = Query(50, ge=1, le=500),
    server_id: Optional[int] = Query(None, description="Filter by server"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get top active users"""
    start_time, period_type = _get_period_filter(period)
    
    conditions = [XrayVisitStats.period_start >= start_time]
    
    if period in ["1h", "24h", "7d"]:
        conditions.append(XrayVisitStats.period_type == 'hour')
    else:
        conditions.append(XrayVisitStats.period_type == 'day')
    
    if server_id:
        conditions.append(XrayVisitStats.server_id == server_id)
    
    result = await db.execute(
        select(
            XrayVisitStats.email,
            sql_func.sum(XrayVisitStats.visit_count).label('total'),
            sql_func.count(sql_func.distinct(XrayVisitStats.destination)).label('unique_sites')
        )
        .where(and_(*conditions))
        .group_by(XrayVisitStats.email)
        .order_by(sql_func.sum(XrayVisitStats.visit_count).desc())
        .limit(limit)
    )
    
    rows = result.fetchall()
    user_ids = [row.email for row in rows]
    
    # Get user info from cache
    user_cache = {}
    if user_ids:
        cache_result = await db.execute(
            select(RemnawaveUserCache).where(RemnawaveUserCache.email.in_(user_ids))
        )
        for user in cache_result.scalars().all():
            user_cache[user.email] = {
                "username": user.username,
                "status": user.status
            }
    
    return {
        "period": period,
        "users": [
            {
                "email": row.email,
                "username": user_cache.get(row.email, {}).get("username"),
                "status": user_cache.get(row.email, {}).get("status"),
                "total_visits": row.total,
                "unique_sites": row.unique_sites
            }
            for row in rows
        ]
    }


@router.get("/stats/user/{email}")
async def get_user_stats(
    email: int,
    period: str = Query("24h", regex="^(1h|24h|7d|30d|365d)$"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get detailed statistics for a specific user"""
    start_time, period_type = _get_period_filter(period)
    
    conditions = [
        XrayVisitStats.period_start >= start_time,
        XrayVisitStats.email == email
    ]
    
    if period in ["1h", "24h", "7d"]:
        conditions.append(XrayVisitStats.period_type == 'hour')
    else:
        conditions.append(XrayVisitStats.period_type == 'day')
    
    # Get user info
    user_result = await db.execute(
        select(RemnawaveUserCache).where(RemnawaveUserCache.email == email)
    )
    user = user_result.scalar_one_or_none()
    
    # Total visits
    total_result = await db.execute(
        select(sql_func.sum(XrayVisitStats.visit_count))
        .where(and_(*conditions))
    )
    total_visits = total_result.scalar() or 0
    
    # Top destinations for this user
    dest_result = await db.execute(
        select(
            XrayVisitStats.destination,
            XrayVisitStats.destination_domain,
            sql_func.sum(XrayVisitStats.visit_count).label('total')
        )
        .where(and_(*conditions))
        .group_by(XrayVisitStats.destination, XrayVisitStats.destination_domain)
        .order_by(sql_func.sum(XrayVisitStats.visit_count).desc())
        .limit(100)
    )
    
    destinations = dest_result.fetchall()
    
    return {
        "email": email,
        "username": user.username if user else None,
        "status": user.status if user else None,
        "period": period,
        "total_visits": total_visits,
        "destinations": [
            {
                "destination": row.destination,
                "domain": row.destination_domain,
                "visits": row.total
            }
            for row in destinations
        ]
    }


@router.get("/stats/timeline")
async def get_timeline(
    period: str = Query("24h", regex="^(1h|24h|7d|30d)$"),
    email: Optional[int] = Query(None),
    server_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get timeline of visits for charting"""
    start_time, _ = _get_period_filter(period)
    
    conditions = [
        XrayVisitStats.period_start >= start_time,
        XrayVisitStats.period_type == 'hour'
    ]
    
    if email:
        conditions.append(XrayVisitStats.email == email)
    if server_id:
        conditions.append(XrayVisitStats.server_id == server_id)
    
    result = await db.execute(
        select(
            XrayVisitStats.period_start,
            sql_func.sum(XrayVisitStats.visit_count).label('total')
        )
        .where(and_(*conditions))
        .group_by(XrayVisitStats.period_start)
        .order_by(XrayVisitStats.period_start)
    )
    
    rows = result.fetchall()
    
    return {
        "period": period,
        "data": [
            {
                "timestamp": row.period_start.isoformat() if row.period_start else None,
                "visits": row.total
            }
            for row in rows
        ]
    }


@router.get("/users")
async def get_users(
    search: Optional[str] = Query(None, min_length=1),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get cached Remnawave users"""
    query = select(RemnawaveUserCache).order_by(RemnawaveUserCache.username)
    
    if search:
        query = query.where(
            RemnawaveUserCache.username.ilike(f"%{search}%")
        )
    
    query = query.limit(limit)
    result = await db.execute(query)
    users = result.scalars().all()
    
    return {
        "count": len(users),
        "users": [
            {
                "email": u.email,
                "uuid": u.uuid,
                "username": u.username,
                "telegram_id": u.telegram_id,
                "status": u.status
            }
            for u in users
        ]
    }
