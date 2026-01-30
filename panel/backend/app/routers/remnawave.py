"""Remnawave integration router for Xray visit statistics

Optimized version with cumulative counters:
- XrayVisitStats: total counts per (server, destination, email)
- XrayHourlyStats: timeline data per (server, hour)
"""

import asyncio
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
    XrayVisitStats, XrayHourlyStats, RemnawaveUserCache, XrayUserIpStats,
    XrayIpDestinationStats
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


class SyncNodesRequest(BaseModel):
    server_ids: list[int]


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
        "api_token": "***" if settings.api_token else None,
        "cookie_secret": "***" if settings.cookie_secret else None,
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
    result = await db.execute(
        select(RemnawaveNode, Server)
        .join(Server, RemnawaveNode.server_id == Server.id)
        .order_by(Server.name)
    )
    rows = result.all()
    
    all_servers = await db.execute(select(Server).order_by(Server.name))
    servers = all_servers.scalars().all()
    
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
    server = await db.execute(select(Server).where(Server.id == request.server_id))
    if not server.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Server not found")
    
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
    result = await db.execute(select(RemnawaveNode))
    current_nodes = {n.server_id: n for n in result.scalars().all()}
    
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
    
    to_remove = current_server_ids - new_server_ids
    if to_remove:
        await db.execute(
            delete(RemnawaveNode).where(RemnawaveNode.server_id.in_(to_remove))
        )
    
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

def _get_time_filter(period: str) -> datetime:
    """Get datetime filter for period."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    
    if period == "1h":
        return now - timedelta(hours=1)
    elif period == "24h":
        return now - timedelta(hours=24)
    elif period == "7d":
        return now - timedelta(days=7)
    elif period == "30d":
        return now - timedelta(days=30)
    elif period == "365d":
        return now - timedelta(days=365)
    elif period == "all":
        return datetime(2020, 1, 1)  # All time
    else:
        return now - timedelta(hours=24)


@router.get("/stats/summary")
async def get_stats_summary(
    period: str = Query("all", pattern="^(1h|24h|7d|30d|365d|all)$"),
    server_ids: Optional[str] = Query(None, description="Comma-separated server IDs"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get summary statistics
    
    For period="all": uses cumulative counters (XrayVisitStats)
    For time-limited: uses hourly stats (XrayHourlyStats)
    """
    if period == "all":
        # Use cumulative counters
        conditions = []
        
        if server_ids:
            ids = [int(x.strip()) for x in server_ids.split(",") if x.strip().isdigit()]
            if ids:
                conditions.append(XrayVisitStats.server_id.in_(ids))
        
        # Total visits
        total_query = select(sql_func.sum(XrayVisitStats.visit_count))
        if conditions:
            total_query = total_query.where(and_(*conditions))
        total_result = await db.execute(total_query)
        total_visits = total_result.scalar() or 0
        
        # Unique users
        users_query = select(sql_func.count(sql_func.distinct(XrayVisitStats.email)))
        if conditions:
            users_query = users_query.where(and_(*conditions))
        users_result = await db.execute(users_query)
        unique_users = users_result.scalar() or 0
        
        # Unique destinations
        dest_query = select(sql_func.count(sql_func.distinct(XrayVisitStats.destination)))
        if conditions:
            dest_query = dest_query.where(and_(*conditions))
        dest_result = await db.execute(dest_query)
        unique_destinations = dest_result.scalar() or 0
    else:
        # Use hourly stats
        start_time = _get_time_filter(period)
        conditions = [XrayHourlyStats.hour >= start_time]
        
        if server_ids:
            ids = [int(x.strip()) for x in server_ids.split(",") if x.strip().isdigit()]
            if ids:
                conditions.append(XrayHourlyStats.server_id.in_(ids))
        
        result = await db.execute(
            select(
                sql_func.sum(XrayHourlyStats.visit_count),
                sql_func.max(XrayHourlyStats.unique_users),
                sql_func.max(XrayHourlyStats.unique_destinations)
            ).where(and_(*conditions))
        )
        row = result.one()
        total_visits = row[0] or 0
        unique_users = row[1] or 0
        unique_destinations = row[2] or 0
    
    return {
        "period": period,
        "total_visits": total_visits,
        "unique_users": unique_users,
        "unique_destinations": unique_destinations
    }


@router.get("/stats/top-destinations")
async def get_top_destinations(
    period: str = Query("all", pattern="^(1h|24h|7d|30d|365d|all)$"),
    limit: int = Query(50, ge=1, le=500),
    email: Optional[int] = Query(None, description="Filter by user email/ID"),
    server_id: Optional[int] = Query(None, description="Filter by server"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get top visited destinations (all time or filtered by last_seen)"""
    conditions = []
    
    if period != "all":
        start_time = _get_time_filter(period)
        conditions.append(XrayVisitStats.last_seen >= start_time)
    
    if email:
        conditions.append(XrayVisitStats.email == email)
    if server_id:
        conditions.append(XrayVisitStats.server_id == server_id)
    
    query = select(
        XrayVisitStats.destination,
        sql_func.sum(XrayVisitStats.visit_count).label('total')
    )
    
    if conditions:
        query = query.where(and_(*conditions))
    
    query = query.group_by(XrayVisitStats.destination) \
                 .order_by(sql_func.sum(XrayVisitStats.visit_count).desc()) \
                 .limit(limit)
    
    result = await db.execute(query)
    rows = result.fetchall()
    
    return {
        "period": period,
        "destinations": [
            {
                "destination": row.destination,
                "visits": row.total
            }
            for row in rows
        ]
    }


@router.get("/stats/top-users")
async def get_top_users(
    period: str = Query("all", pattern="^(1h|24h|7d|30d|365d|all)$"),
    limit: int = Query(50, ge=1, le=500),
    server_id: Optional[int] = Query(None, description="Filter by server"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get top active users"""
    conditions = []
    
    if period != "all":
        start_time = _get_time_filter(period)
        conditions.append(XrayVisitStats.last_seen >= start_time)
    
    if server_id:
        conditions.append(XrayVisitStats.server_id == server_id)
    
    query = select(
        XrayVisitStats.email,
        sql_func.sum(XrayVisitStats.visit_count).label('total'),
        sql_func.count(sql_func.distinct(XrayVisitStats.destination)).label('unique_sites')
    )
    
    if conditions:
        query = query.where(and_(*conditions))
    
    query = query.group_by(XrayVisitStats.email) \
                 .order_by(sql_func.sum(XrayVisitStats.visit_count).desc()) \
                 .limit(limit)
    
    result = await db.execute(query)
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
    
    # Get unique IP counts for each user (across all servers, deduplicated)
    ip_counts = {}
    if user_ids:
        ip_conditions = [XrayUserIpStats.email.in_(user_ids)]
        if period != "all":
            ip_conditions.append(XrayUserIpStats.last_seen >= start_time)
        if server_id:
            ip_conditions.append(XrayUserIpStats.server_id == server_id)
        
        ip_result = await db.execute(
            select(
                XrayUserIpStats.email,
                sql_func.count(sql_func.distinct(XrayUserIpStats.source_ip)).label('unique_ips')
            )
            .where(and_(*ip_conditions))
            .group_by(XrayUserIpStats.email)
        )
        for ip_row in ip_result.fetchall():
            ip_counts[ip_row.email] = ip_row.unique_ips
    
    return {
        "period": period,
        "users": [
            {
                "email": row.email,
                "username": user_cache.get(row.email, {}).get("username"),
                "status": user_cache.get(row.email, {}).get("status"),
                "total_visits": row.total,
                "unique_sites": row.unique_sites,
                "unique_ips": ip_counts.get(row.email, 0)
            }
            for row in rows
        ]
    }


@router.get("/stats/user/{email}")
async def get_user_stats(
    email: int,
    period: str = Query("all", pattern="^(1h|24h|7d|30d|365d|all)$"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get detailed statistics for a specific user"""
    conditions = [XrayVisitStats.email == email]
    ip_conditions = [XrayUserIpStats.email == email]
    
    if period != "all":
        start_time = _get_time_filter(period)
        conditions.append(XrayVisitStats.last_seen >= start_time)
        ip_conditions.append(XrayUserIpStats.last_seen >= start_time)
    
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
            XrayVisitStats.visit_count,
            XrayVisitStats.first_seen,
            XrayVisitStats.last_seen
        )
        .where(and_(*conditions))
        .order_by(XrayVisitStats.visit_count.desc())
        .limit(limit)
    )
    
    destinations = dest_result.fetchall()
    
    # Get unique IPs count
    unique_ips_result = await db.execute(
        select(sql_func.count(sql_func.distinct(XrayUserIpStats.source_ip)))
        .where(and_(*ip_conditions))
    )
    unique_ips = unique_ips_result.scalar() or 0
    
    # Get IP details with server info
    ip_result = await db.execute(
        select(
            XrayUserIpStats.source_ip,
            XrayUserIpStats.server_id,
            XrayUserIpStats.connection_count,
            XrayUserIpStats.first_seen,
            XrayUserIpStats.last_seen,
            Server.name.label('server_name')
        )
        .join(Server, XrayUserIpStats.server_id == Server.id)
        .where(and_(*ip_conditions))
        .order_by(XrayUserIpStats.connection_count.desc())
    )
    ip_rows = ip_result.fetchall()
    
    # Aggregate IPs across servers
    ip_map: dict[str, dict] = {}
    for row in ip_rows:
        if row.source_ip not in ip_map:
            ip_map[row.source_ip] = {
                "source_ip": row.source_ip,
                "servers": [],
                "total_count": 0,
                "first_seen": row.first_seen,
                "last_seen": row.last_seen
            }
        ip_map[row.source_ip]["servers"].append({
            "server_id": row.server_id,
            "server_name": row.server_name,
            "count": row.connection_count
        })
        ip_map[row.source_ip]["total_count"] += row.connection_count
        if row.first_seen and (not ip_map[row.source_ip]["first_seen"] or row.first_seen < ip_map[row.source_ip]["first_seen"]):
            ip_map[row.source_ip]["first_seen"] = row.first_seen
        if row.last_seen and (not ip_map[row.source_ip]["last_seen"] or row.last_seen > ip_map[row.source_ip]["last_seen"]):
            ip_map[row.source_ip]["last_seen"] = row.last_seen
    
    # Sort IPs by total count
    ips = sorted(ip_map.values(), key=lambda x: x["total_count"], reverse=True)
    
    return {
        "email": email,
        "username": user.username if user else None,
        "status": user.status if user else None,
        "period": period,
        "total_visits": total_visits,
        "unique_ips": unique_ips,
        "destinations": [
            {
                "destination": row.destination,
                "visits": row.visit_count,
                "first_seen": row.first_seen.isoformat() if row.first_seen else None,
                "last_seen": row.last_seen.isoformat() if row.last_seen else None
            }
            for row in destinations
        ],
        "ips": [
            {
                "source_ip": ip["source_ip"],
                "servers": ip["servers"],
                "total_count": ip["total_count"],
                "first_seen": ip["first_seen"].isoformat() if ip["first_seen"] else None,
                "last_seen": ip["last_seen"].isoformat() if ip["last_seen"] else None
            }
            for ip in ips[:50]  # Limit to 50 IPs
        ]
    }


@router.get("/stats/destination/users")
async def get_destination_users(
    destination: str = Query(..., description="Destination to get users for"),
    period: str = Query("all", pattern="^(1h|24h|7d|30d|365d|all)$"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get users who visited a specific destination"""
    conditions = [XrayVisitStats.destination == destination]
    
    if period != "all":
        start_time = _get_time_filter(period)
        conditions.append(XrayVisitStats.last_seen >= start_time)
    
    # Get total visits for this destination
    total_result = await db.execute(
        select(sql_func.sum(XrayVisitStats.visit_count))
        .where(and_(*conditions))
    )
    total_visits = total_result.scalar() or 0
    
    # Get users with their visit counts
    result = await db.execute(
        select(
            XrayVisitStats.email,
            XrayVisitStats.visit_count,
            XrayVisitStats.first_seen,
            XrayVisitStats.last_seen
        )
        .where(and_(*conditions))
        .order_by(XrayVisitStats.visit_count.desc())
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
        "destination": destination,
        "period": period,
        "total_visits": total_visits,
        "users": [
            {
                "email": row.email,
                "username": user_cache.get(row.email, {}).get("username"),
                "status": user_cache.get(row.email, {}).get("status"),
                "visits": row.visit_count,
                "percentage": round((row.visit_count / total_visits * 100), 1) if total_visits > 0 else 0,
                "first_seen": row.first_seen.isoformat() if row.first_seen else None,
                "last_seen": row.last_seen.isoformat() if row.last_seen else None
            }
            for row in rows
        ]
    }


@router.get("/stats/ip/destinations")
async def get_ip_destinations(
    source_ip: str = Query(..., description="Source IP address"),
    email: int = Query(..., description="User email/ID"),
    period: str = Query("all", pattern="^(1h|24h|7d|30d|365d|all)$"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get destinations visited from a specific source IP by user"""
    conditions = [
        XrayIpDestinationStats.source_ip == source_ip,
        XrayIpDestinationStats.email == email
    ]
    
    if period != "all":
        start_time = _get_time_filter(period)
        conditions.append(XrayIpDestinationStats.last_seen >= start_time)
    
    # Get total connections from this IP
    total_result = await db.execute(
        select(sql_func.sum(XrayIpDestinationStats.connection_count))
        .where(and_(*conditions))
    )
    total_connections = total_result.scalar() or 0
    
    # Get destinations with connection counts
    result = await db.execute(
        select(
            XrayIpDestinationStats.destination,
            sql_func.sum(XrayIpDestinationStats.connection_count).label('total'),
            sql_func.min(XrayIpDestinationStats.first_seen).label('first_seen'),
            sql_func.max(XrayIpDestinationStats.last_seen).label('last_seen')
        )
        .where(and_(*conditions))
        .group_by(XrayIpDestinationStats.destination)
        .order_by(sql_func.sum(XrayIpDestinationStats.connection_count).desc())
        .limit(limit)
    )
    
    rows = result.fetchall()
    
    return {
        "source_ip": source_ip,
        "email": email,
        "period": period,
        "total_connections": total_connections,
        "destinations": [
            {
                "destination": row.destination,
                "connections": row.total,
                "percentage": round((row.total / total_connections * 100), 1) if total_connections > 0 else 0,
                "first_seen": row.first_seen.isoformat() if row.first_seen else None,
                "last_seen": row.last_seen.isoformat() if row.last_seen else None
            }
            for row in rows
        ]
    }


@router.get("/stats/timeline")
async def get_timeline(
    period: str = Query("24h", pattern="^(1h|24h|7d|30d|365d)$"),
    server_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get timeline of visits for charting (from XrayHourlyStats)"""
    start_time = _get_time_filter(period)
    
    conditions = [XrayHourlyStats.hour >= start_time]
    
    if server_id:
        conditions.append(XrayHourlyStats.server_id == server_id)
    
    result = await db.execute(
        select(
            XrayHourlyStats.hour,
            sql_func.sum(XrayHourlyStats.visit_count).label('total'),
            sql_func.sum(XrayHourlyStats.unique_users).label('users'),
            sql_func.sum(XrayHourlyStats.unique_destinations).label('destinations')
        )
        .where(and_(*conditions))
        .group_by(XrayHourlyStats.hour)
        .order_by(XrayHourlyStats.hour)
    )
    
    rows = result.fetchall()
    
    return {
        "period": period,
        "data": [
            {
                "timestamp": row.hour.isoformat() if row.hour else None,
                "visits": row.total,
                "unique_users": row.users,
                "unique_destinations": row.destinations
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
    """Get cached Remnawave users.
    
    Search works by username or email (ID).
    If user not in cache but has visit stats, returns basic info from stats.
    """
    from sqlalchemy import or_, cast, String
    
    cached_users = []
    
    # Search in cache
    query = select(RemnawaveUserCache).order_by(RemnawaveUserCache.username)
    
    if search:
        # Search by username or email (ID)
        if search.isdigit():
            # Exact match for email ID
            query = query.where(RemnawaveUserCache.email == int(search))
        else:
            query = query.where(
                RemnawaveUserCache.username.ilike(f"%{search}%")
            )
    
    query = query.limit(limit)
    result = await db.execute(query)
    cached_users = result.scalars().all()
    
    # If searching by email ID and not found in cache, check visit stats
    users_from_stats = []
    if search and search.isdigit() and not cached_users:
        email_id = int(search)
        stats_result = await db.execute(
            select(
                XrayVisitStats.email,
                sql_func.sum(XrayVisitStats.visit_count).label('total_visits'),
                sql_func.min(XrayVisitStats.first_seen).label('first_seen'),
                sql_func.max(XrayVisitStats.last_seen).label('last_seen')
            )
            .where(XrayVisitStats.email == email_id)
            .group_by(XrayVisitStats.email)
        )
        stats_row = stats_result.one_or_none()
        if stats_row:
            users_from_stats.append({
                "email": stats_row.email,
                "uuid": None,
                "username": None,
                "telegram_id": None,
                "status": "unknown",
                "from_stats": True,
                "total_visits": stats_row.total_visits,
                "first_seen": stats_row.first_seen.isoformat() if stats_row.first_seen else None,
                "last_seen": stats_row.last_seen.isoformat() if stats_row.last_seen else None
            })
    
    return {
        "count": len(cached_users) + len(users_from_stats),
        "users": [
            {
                "email": u.email,
                "uuid": u.uuid,
                "username": u.username,
                "telegram_id": u.telegram_id,
                "status": u.status
            }
            for u in cached_users
        ] + users_from_stats
    }


@router.get("/stats/db-info")
async def get_db_info(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get database statistics for monitoring"""
    # Count records in each table
    visit_count = await db.execute(select(sql_func.count()).select_from(XrayVisitStats))
    hourly_count = await db.execute(select(sql_func.count()).select_from(XrayHourlyStats))
    user_count = await db.execute(select(sql_func.count()).select_from(RemnawaveUserCache))
    ip_count = await db.execute(select(sql_func.count()).select_from(XrayUserIpStats))
    ip_dest_count = await db.execute(select(sql_func.count()).select_from(XrayIpDestinationStats))
    
    # Get date ranges
    visit_range = await db.execute(
        select(
            sql_func.min(XrayVisitStats.first_seen),
            sql_func.max(XrayVisitStats.last_seen)
        )
    )
    hourly_range = await db.execute(
        select(
            sql_func.min(XrayHourlyStats.hour),
            sql_func.max(XrayHourlyStats.hour)
        )
    )
    
    v_range = visit_range.one()
    h_range = hourly_range.one()
    
    return {
        "tables": {
            "xray_visit_stats": {
                "count": visit_count.scalar() or 0,
                "first_seen": v_range[0].isoformat() if v_range[0] else None,
                "last_seen": v_range[1].isoformat() if v_range[1] else None
            },
            "xray_hourly_stats": {
                "count": hourly_count.scalar() or 0,
                "first_hour": h_range[0].isoformat() if h_range[0] else None,
                "last_hour": h_range[1].isoformat() if h_range[1] else None
            },
            "xray_user_ip_stats": {
                "count": ip_count.scalar() or 0
            },
            "xray_ip_destination_stats": {
                "count": ip_dest_count.scalar() or 0
            },
            "remnawave_user_cache": {
                "count": user_count.scalar() or 0
            }
        }
    }


@router.delete("/stats/clear")
async def clear_stats(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Clear all visit statistics (visits, IPs, IP-destinations, hourly stats).
    
    WARNING: This permanently deletes all collected visit data.
    User cache is NOT deleted (can be refreshed from Remnawave API).
    """
    # Delete all visit stats
    visit_result = await db.execute(delete(XrayVisitStats))
    deleted_visits = visit_result.rowcount
    
    # Delete all IP stats
    ip_result = await db.execute(delete(XrayUserIpStats))
    deleted_ips = ip_result.rowcount
    
    # Delete all IP-destination stats
    ip_dest_result = await db.execute(delete(XrayIpDestinationStats))
    deleted_ip_dests = ip_dest_result.rowcount
    
    # Delete all hourly stats
    hourly_result = await db.execute(delete(XrayHourlyStats))
    deleted_hourly = hourly_result.rowcount
    
    await db.commit()
    
    return {
        "success": True,
        "deleted": {
            "visit_stats": deleted_visits,
            "ip_stats": deleted_ips,
            "ip_destination_stats": deleted_ip_dests,
            "hourly_stats": deleted_hourly
        },
        "message": f"Deleted {deleted_visits} visit records, {deleted_ips} IP records, {deleted_ip_dests} IP-destination records, {deleted_hourly} hourly records"
    }


@router.get("/user/{email}/full")
async def get_user_full_info(
    email: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get full user information from cache and optionally from Remnawave API.
    
    Returns cached user data with all extended fields.
    If uuid is available, also fetches subscription history and bandwidth stats.
    """
    # Get user from cache
    result = await db.execute(
        select(RemnawaveUserCache).where(RemnawaveUserCache.email == email)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found in cache")
    
    # Format response with all cached fields
    response = {
        "email": user.email,
        "uuid": user.uuid,
        "short_uuid": user.short_uuid,
        "username": user.username,
        "telegram_id": user.telegram_id,
        "status": user.status,
        # Subscription info
        "expire_at": user.expire_at.isoformat() if user.expire_at else None,
        "subscription_url": user.subscription_url,
        "sub_revoked_at": user.sub_revoked_at.isoformat() if user.sub_revoked_at else None,
        "sub_last_user_agent": user.sub_last_user_agent,
        "sub_last_opened_at": user.sub_last_opened_at.isoformat() if user.sub_last_opened_at else None,
        # Traffic limits
        "traffic_limit_bytes": user.traffic_limit_bytes,
        "traffic_limit_strategy": user.traffic_limit_strategy,
        "last_traffic_reset_at": user.last_traffic_reset_at.isoformat() if user.last_traffic_reset_at else None,
        # Traffic usage
        "used_traffic_bytes": user.used_traffic_bytes,
        "lifetime_used_traffic_bytes": user.lifetime_used_traffic_bytes,
        "online_at": user.online_at.isoformat() if user.online_at else None,
        "first_connected_at": user.first_connected_at.isoformat() if user.first_connected_at else None,
        "last_connected_node_uuid": user.last_connected_node_uuid,
        # Device limit
        "hwid_device_limit": user.hwid_device_limit,
        # Additional info
        "user_email": user.user_email,
        "description": user.description,
        "tag": user.tag,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        # Extra data from Remnawave API
        "subscription_history": None,
        "bandwidth_stats": None,
        "hwid_devices": None
    }
    
    return response


@router.get("/user/{email}/live")
async def get_user_live_info(
    email: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get live user information directly from Remnawave API.
    
    Fetches fresh data including subscription history and bandwidth stats.
    """
    # Get user UUID from cache
    result = await db.execute(
        select(RemnawaveUserCache).where(RemnawaveUserCache.email == email)
    )
    user = result.scalar_one_or_none()
    
    if not user or not user.uuid:
        raise HTTPException(status_code=404, detail="User UUID not found in cache")
    
    # Get settings
    settings_result = await db.execute(select(RemnawaveSettings).limit(1))
    settings = settings_result.scalar_one_or_none()
    
    if not settings or not settings.api_url or not settings.api_token:
        raise HTTPException(status_code=400, detail="Remnawave API not configured")
    
    api = get_remnawave_api(settings.api_url, settings.api_token, settings.cookie_secret)
    
    try:
        # Fetch user data, subscription history, and bandwidth stats in parallel
        user_data, sub_history, bandwidth_stats, hwid_devices = await asyncio.gather(
            api.get_user_by_uuid(user.uuid),
            api.get_user_subscription_history(user.uuid),
            _get_user_bandwidth_stats(api, user.uuid),
            api.get_user_hwid_devices(user.uuid),
            return_exceptions=True
        )
        
        # Handle exceptions
        if isinstance(user_data, Exception):
            user_data = None
        if isinstance(sub_history, Exception):
            sub_history = None
        if isinstance(bandwidth_stats, Exception):
            bandwidth_stats = None
        if isinstance(hwid_devices, Exception):
            hwid_devices = None
        
        if not user_data:
            raise HTTPException(status_code=404, detail="User not found in Remnawave")
        
        # Parse userTraffic
        user_traffic = user_data.get("userTraffic") or {}
        
        response = {
            "email": user_data.get("id"),
            "uuid": user_data.get("uuid"),
            "short_uuid": user_data.get("shortUuid"),
            "username": user_data.get("username"),
            "telegram_id": user_data.get("telegramId"),
            "status": user_data.get("status"),
            # Subscription info
            "expire_at": user_data.get("expireAt"),
            "subscription_url": user_data.get("subscriptionUrl"),
            "sub_revoked_at": user_data.get("subRevokedAt"),
            "sub_last_user_agent": user_data.get("subLastUserAgent"),
            "sub_last_opened_at": user_data.get("subLastOpenedAt"),
            # Traffic limits
            "traffic_limit_bytes": user_data.get("trafficLimitBytes"),
            "traffic_limit_strategy": user_data.get("trafficLimitStrategy"),
            "last_traffic_reset_at": user_data.get("lastTrafficResetAt"),
            # Traffic usage
            "used_traffic_bytes": user_traffic.get("usedTrafficBytes"),
            "lifetime_used_traffic_bytes": user_traffic.get("lifetimeUsedTrafficBytes"),
            "online_at": user_traffic.get("onlineAt"),
            "first_connected_at": user_traffic.get("firstConnectedAt"),
            "last_connected_node_uuid": user_traffic.get("lastConnectedNodeUuid"),
            # Device limit
            "hwid_device_limit": user_data.get("hwidDeviceLimit"),
            # Additional info
            "user_email": user_data.get("email"),
            "description": user_data.get("description"),
            "tag": user_data.get("tag"),
            "created_at": user_data.get("createdAt"),
            "updated_at": user_data.get("updatedAt"),
            # Internal squads
            "active_internal_squads": user_data.get("activeInternalSquads"),
            # Extra data from Remnawave API
            "subscription_history": sub_history,
            "bandwidth_stats": bandwidth_stats,
            "hwid_devices": hwid_devices
        }
        
        return response
        
    finally:
        await api.close()


async def _get_user_bandwidth_stats(api, uuid: str) -> Optional[dict]:
    """Get user bandwidth stats for last 30 days."""
    from datetime import datetime, timedelta
    
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    
    return await api.get_user_bandwidth_stats(uuid, start_date, end_date)
