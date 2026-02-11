"""Remnawave integration router for Xray visit statistics

Optimized version with cumulative counters:
- XrayVisitStats: total counts per (server, destination, email)
- XrayHourlyStats: timeline data per (server, hour)
"""

import asyncio
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func as sql_func, and_, delete, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import (
    Server, RemnawaveSettings, RemnawaveNode, RemnawaveInfrastructureAddress,
    RemnawaveExcludedDestination, XrayVisitStats, XrayHourlyStats, RemnawaveUserCache,
    XrayUserIpStats, XrayIpDestinationStats, RemnawaveExport, TrafficAnalyzerSettings,
    TrafficAnomalyLog, XrayDestination, XraySourceIp
)
from app.services.remnawave_api import get_remnawave_api
from app.services.xray_stats_collector import get_xray_stats_collector, resolve_infrastructure_address
from app.services.traffic_analyzer import get_traffic_analyzer

router = APIRouter(prefix="/remnawave", tags=["remnawave"])


# === In-memory cache with TTL for heavy queries ===

_stats_cache: dict[str, tuple[Any, float]] = {}
_CACHE_TTL_SHORT = 30  # seconds for stats
_CACHE_TTL_LONG = 300  # seconds for db-info


def _get_cached(key: str) -> Optional[Any]:
    """Get value from cache if not expired."""
    if key in _stats_cache:
        value, timestamp = _stats_cache[key]
        if time.time() - timestamp < (_CACHE_TTL_LONG if key.startswith("db_") else _CACHE_TTL_SHORT):
            return value
        # Expired, remove from cache
        del _stats_cache[key]
    return None


def _set_cached(key: str, value: Any) -> None:
    """Store value in cache with current timestamp."""
    _stats_cache[key] = (value, time.time())


def _invalidate_cache(prefix: str = "") -> None:
    """Invalidate cache entries matching prefix."""
    if not prefix:
        _stats_cache.clear()
    else:
        keys_to_delete = [k for k in _stats_cache if k.startswith(prefix)]
        for k in keys_to_delete:
            del _stats_cache[k]


# === Request/Response Models ===

class UpdateSettingsRequest(BaseModel):
    api_url: Optional[str] = Field(None, max_length=500)
    api_token: Optional[str] = Field(None, max_length=500)
    cookie_secret: Optional[str] = Field(None, max_length=500)
    enabled: Optional[bool] = None
    collection_interval: Optional[int] = Field(None, ge=60, le=900)  # 1-15 minutes
    # Retention settings (days)
    visit_stats_retention_days: Optional[int] = Field(None, ge=7, le=365)
    ip_stats_retention_days: Optional[int] = Field(None, ge=7, le=365)
    ip_destination_retention_days: Optional[int] = Field(None, ge=7, le=365)
    hourly_stats_retention_days: Optional[int] = Field(None, ge=7, le=365)


class AddIgnoredUserRequest(BaseModel):
    user_id: int = Field(..., description="User ID (email) to ignore")


class RemoveIgnoredUserRequest(BaseModel):
    user_id: int = Field(..., description="User ID (email) to remove from ignore list")


class AddNodeRequest(BaseModel):
    server_id: int


class SyncNodesRequest(BaseModel):
    server_ids: list[int]


class AddInfrastructureAddressRequest(BaseModel):
    address: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=255)


class AddExcludedDestinationRequest(BaseModel):
    destination: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = Field(None, max_length=255)


class UpdateAnalyzerSettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    check_interval_minutes: Optional[int] = Field(None, ge=15, le=120)
    traffic_limit_gb: Optional[float] = Field(None, ge=1, le=10000)
    ip_limit_multiplier: Optional[float] = Field(None, ge=1, le=10)
    check_hwid_anomalies: Optional[bool] = None
    telegram_bot_token: Optional[str] = Field(None, max_length=200)
    telegram_chat_id: Optional[str] = Field(None, max_length=100)


class TestTelegramRequest(BaseModel):
    bot_token: str = Field(..., min_length=10, max_length=200)
    chat_id: str = Field(..., min_length=1, max_length=100)


# === Settings Endpoints ===

def _parse_ignored_user_ids(json_str: Optional[str]) -> list[int]:
    """Parse ignored_user_ids JSON string to list of integers."""
    if not json_str:
        return []
    try:
        import json
        data = json.loads(json_str)
        if isinstance(data, list):
            return [int(x) for x in data if isinstance(x, (int, str)) and str(x).isdigit()]
        return []
    except (json.JSONDecodeError, ValueError):
        return []


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
            "collection_interval": 300,  # 5 minutes default
            "ignored_user_ids": [],
            "visit_stats_retention_days": 365,
            "ip_stats_retention_days": 90,
            "ip_destination_retention_days": 90,
            "hourly_stats_retention_days": 365
        }
    
    return {
        "api_url": settings.api_url,
        "api_token": "***" if settings.api_token else None,
        "cookie_secret": "***" if settings.cookie_secret else None,
        "enabled": settings.enabled,
        "collection_interval": settings.collection_interval,
        "ignored_user_ids": _parse_ignored_user_ids(settings.ignored_user_ids),
        "visit_stats_retention_days": settings.visit_stats_retention_days or 365,
        "ip_stats_retention_days": settings.ip_stats_retention_days or 90,
        "ip_destination_retention_days": settings.ip_destination_retention_days or 90,
        "hourly_stats_retention_days": settings.hourly_stats_retention_days or 365
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
    # Retention settings
    if request.visit_stats_retention_days is not None:
        settings.visit_stats_retention_days = request.visit_stats_retention_days
    if request.ip_stats_retention_days is not None:
        settings.ip_stats_retention_days = request.ip_stats_retention_days
    if request.ip_destination_retention_days is not None:
        settings.ip_destination_retention_days = request.ip_destination_retention_days
    if request.hourly_stats_retention_days is not None:
        settings.hourly_stats_retention_days = request.hourly_stats_retention_days
    
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


# === Ignored Users ===

@router.get("/ignored-users")
async def get_ignored_users(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get list of ignored user IDs.
    
    Ignored users are excluded from:
    - Log collection
    - Anomaly analyzer notifications
    - All checks and statistics
    """
    result = await db.execute(select(RemnawaveSettings).limit(1))
    settings = result.scalar_one_or_none()
    
    ignored_ids = _parse_ignored_user_ids(settings.ignored_user_ids if settings else None)
    
    # Get user info from cache for display
    user_info = []
    if ignored_ids:
        cache_result = await db.execute(
            select(RemnawaveUserCache).where(RemnawaveUserCache.email.in_(ignored_ids))
        )
        user_cache = {u.email: u for u in cache_result.scalars().all()}
        
        for user_id in ignored_ids:
            cached = user_cache.get(user_id)
            user_info.append({
                "user_id": user_id,
                "username": cached.username if cached else None,
                "status": cached.status if cached else None,
                "telegram_id": cached.telegram_id if cached else None
            })
    
    return {
        "ignored_users": user_info,
        "count": len(ignored_ids)
    }


@router.post("/ignored-users")
async def add_ignored_user(
    request: AddIgnoredUserRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add user to ignored list.
    
    Ignored users will be excluded from log collection, anomaly notifications,
    and all statistics processing.
    """
    import json
    
    result = await db.execute(select(RemnawaveSettings).limit(1))
    settings = result.scalar_one_or_none()
    
    if not settings:
        settings = RemnawaveSettings()
        db.add(settings)
    
    current_ids = _parse_ignored_user_ids(settings.ignored_user_ids)
    
    if request.user_id in current_ids:
        return {"success": False, "error": "User already in ignore list"}
    
    current_ids.append(request.user_id)
    settings.ignored_user_ids = json.dumps(current_ids)
    
    await db.commit()
    
    # Get user info for response
    cache_result = await db.execute(
        select(RemnawaveUserCache).where(RemnawaveUserCache.email == request.user_id)
    )
    cached = cache_result.scalar_one_or_none()
    
    return {
        "success": True,
        "message": "User added to ignore list",
        "user": {
            "user_id": request.user_id,
            "username": cached.username if cached else None,
            "status": cached.status if cached else None
        }
    }


@router.delete("/ignored-users/{user_id}")
async def remove_ignored_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Remove user from ignored list."""
    import json
    
    result = await db.execute(select(RemnawaveSettings).limit(1))
    settings = result.scalar_one_or_none()
    
    if not settings:
        return {"success": False, "error": "Settings not found"}
    
    current_ids = _parse_ignored_user_ids(settings.ignored_user_ids)
    
    if user_id not in current_ids:
        return {"success": False, "error": "User not in ignore list"}
    
    current_ids.remove(user_id)
    settings.ignored_user_ids = json.dumps(current_ids) if current_ids else None
    
    await db.commit()
    
    return {
        "success": True,
        "message": "User removed from ignore list"
    }


# === Infrastructure Addresses ===

@router.get("/infrastructure-ips")
async def get_infrastructure_addresses(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get list of infrastructure addresses (IPs/domains)"""
    result = await db.execute(
        select(RemnawaveInfrastructureAddress).order_by(RemnawaveInfrastructureAddress.address)
    )
    addresses = result.scalars().all()
    
    return {
        "addresses": [
            {
                "id": addr.id,
                "address": addr.address,
                "resolved_ips": addr.resolved_ips,
                "last_resolved": addr.last_resolved.isoformat() if addr.last_resolved else None,
                "description": addr.description,
                "created_at": addr.created_at.isoformat() if addr.created_at else None
            }
            for addr in addresses
        ]
    }


@router.post("/infrastructure-ips")
async def add_infrastructure_address(
    request: AddInfrastructureAddressRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add infrastructure address (IP or domain)"""
    import json
    
    # Check for duplicate
    existing = await db.execute(
        select(RemnawaveInfrastructureAddress).where(
            RemnawaveInfrastructureAddress.address == request.address.strip()
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Address already exists")
    
    # Resolve address
    address = request.address.strip()
    resolved = await resolve_infrastructure_address(address, use_cache=False)
    resolved_json = json.dumps(sorted(resolved)) if resolved else None
    
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    
    new_addr = RemnawaveInfrastructureAddress(
        address=address,
        resolved_ips=resolved_json,
        last_resolved=now if resolved else None,
        description=request.description
    )
    db.add(new_addr)
    await db.commit()
    await db.refresh(new_addr)
    
    return {
        "success": True,
        "address": {
            "id": new_addr.id,
            "address": new_addr.address,
            "resolved_ips": new_addr.resolved_ips,
            "last_resolved": new_addr.last_resolved.isoformat() if new_addr.last_resolved else None,
            "description": new_addr.description,
            "created_at": new_addr.created_at.isoformat() if new_addr.created_at else None
        }
    }


@router.delete("/infrastructure-ips/{address_id}")
async def delete_infrastructure_address(
    address_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete infrastructure address"""
    result = await db.execute(
        select(RemnawaveInfrastructureAddress).where(
            RemnawaveInfrastructureAddress.id == address_id
        )
    )
    addr = result.scalar_one_or_none()
    
    if not addr:
        raise HTTPException(status_code=404, detail="Address not found")
    
    await db.delete(addr)
    await db.commit()
    
    return {"success": True, "message": "Address deleted"}


@router.post("/infrastructure-ips/resolve")
async def resolve_all_infrastructure_addresses(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Force re-resolve all infrastructure addresses"""
    import json
    
    result = await db.execute(select(RemnawaveInfrastructureAddress))
    addresses = result.scalars().all()
    
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    updated = 0
    
    for addr in addresses:
        resolved = await resolve_infrastructure_address(addr.address, use_cache=False)
        resolved_json = json.dumps(sorted(resolved)) if resolved else None
        
        if resolved_json != addr.resolved_ips:
            addr.resolved_ips = resolved_json
            addr.last_resolved = now
            updated += 1
    
    await db.commit()
    
    return {
        "success": True,
        "total": len(addresses),
        "updated": updated
    }


@router.post("/infrastructure-ips/rescan")
async def rescan_existing_ip_stats(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Rescan all existing IP stats and update is_infrastructure flag.
    
    Uses JOIN with xray_source_ips for bulk UPDATE.
    """
    import json
    from urllib.parse import urlparse
    from sqlalchemy import update, text
    
    # Build set of all infrastructure IPs
    infrastructure_ips = set()
    
    server_result = await db.execute(select(Server.url))
    for row in server_result.fetchall():
        url = row[0]
        if url:
            try:
                parsed = urlparse(url)
                if parsed.hostname:
                    infrastructure_ips.add(parsed.hostname)
            except Exception:
                pass
    
    infra_result = await db.execute(select(RemnawaveInfrastructureAddress))
    addresses = infra_result.scalars().all()
    
    for addr in addresses:
        infrastructure_ips.add(addr.address)
        if addr.resolved_ips:
            try:
                resolved = json.loads(addr.resolved_ips)
                infrastructure_ips.update(resolved)
            except json.JSONDecodeError:
                pass
    
    infra_list = list(infrastructure_ips)
    
    # Get source_ip_ids that match infrastructure IPs
    infra_ip_ids_result = await db.execute(
        select(XraySourceIp.id).where(XraySourceIp.ip.in_(infra_list))
    )
    infra_ip_ids = [row[0] for row in infra_ip_ids_result.fetchall()]
    
    updated_to_infra = 0
    updated_to_client = 0
    
    if infra_ip_ids:
        result_infra = await db.execute(
            update(XrayUserIpStats)
            .where(XrayUserIpStats.source_ip_id.in_(infra_ip_ids))
            .where(XrayUserIpStats.is_infrastructure == False)
            .values(is_infrastructure=True)
        )
        updated_to_infra = result_infra.rowcount
    
    # Mark non-matching as client
    non_infra_result = await db.execute(
        select(XraySourceIp.id).where(XraySourceIp.ip.notin_(infra_list)) if infra_list else select(XraySourceIp.id)
    )
    non_infra_ip_ids = [row[0] for row in non_infra_result.fetchall()]
    
    if non_infra_ip_ids:
        result_client = await db.execute(
            update(XrayUserIpStats)
            .where(XrayUserIpStats.source_ip_id.in_(non_infra_ip_ids))
            .where(XrayUserIpStats.is_infrastructure == True)
            .values(is_infrastructure=False)
        )
        updated_to_client = result_client.rowcount
    
    await db.commit()
    
    return {
        "success": True,
        "infrastructure_ips_count": len(infrastructure_ips),
        "updated_to_infrastructure": updated_to_infra,
        "updated_to_client": updated_to_client
    }


# === Excluded Destinations ===

@router.get("/excluded-destinations")
async def get_excluded_destinations(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get list of excluded destinations (sites excluded from statistics)"""
    result = await db.execute(
        select(RemnawaveExcludedDestination).order_by(RemnawaveExcludedDestination.destination)
    )
    destinations = result.scalars().all()
    
    return {
        "destinations": [
            {
                "id": dest.id,
                "destination": dest.destination,
                "description": dest.description,
                "created_at": dest.created_at.isoformat() if dest.created_at else None
            }
            for dest in destinations
        ]
    }


@router.get("/excluded-destinations/list")
async def get_excluded_destinations_list(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get simple list of excluded destinations (for node filtering)"""
    result = await db.execute(
        select(RemnawaveExcludedDestination.destination)
    )
    destinations = [row[0] for row in result.fetchall()]
    
    return {"destinations": destinations}


@router.post("/excluded-destinations")
async def add_excluded_destination(
    request: AddExcludedDestinationRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add destination to exclusion list and delete all existing data for that host"""
    destination = request.destination.strip()
    
    # Check for duplicate
    existing = await db.execute(
        select(RemnawaveExcludedDestination).where(
            RemnawaveExcludedDestination.destination == destination
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Destination already exists")
    
    new_dest = RemnawaveExcludedDestination(
        destination=destination,
        description=request.description
    )
    db.add(new_dest)
    
    # Extract host (strip port) for matching all related destinations
    host = re.sub(r':\d+$', '', destination)
    
    # Find all destination_ids matching this host (any port)
    dest_result = await db.execute(
        select(XrayDestination.id).where(XrayDestination.host == host)
    )
    dest_ids = [row[0] for row in dest_result.fetchall()]
    
    deleted_count = 0
    if dest_ids:
        # Delete visit stats for these destinations
        r1 = await db.execute(
            delete(XrayVisitStats).where(XrayVisitStats.destination_id.in_(dest_ids))
        )
        deleted_count += r1.rowcount
        
        # Delete IP-destination stats
        r2 = await db.execute(
            delete(XrayIpDestinationStats).where(XrayIpDestinationStats.destination_id.in_(dest_ids))
        )
        deleted_count += r2.rowcount
        
        # Delete the destination entries themselves
        await db.execute(
            delete(XrayDestination).where(XrayDestination.id.in_(dest_ids))
        )
    
    await db.commit()
    await db.refresh(new_dest)
    
    # Invalidate stats cache so changes reflect immediately
    _stats_cache.clear()
    
    return {
        "success": True,
        "destination": {
            "id": new_dest.id,
            "destination": new_dest.destination,
            "description": new_dest.description,
            "created_at": new_dest.created_at.isoformat() if new_dest.created_at else None
        },
        "deleted_records": deleted_count
    }


@router.delete("/excluded-destinations/{destination_id}")
async def delete_excluded_destination(
    destination_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete destination from exclusion list"""
    result = await db.execute(
        select(RemnawaveExcludedDestination).where(
            RemnawaveExcludedDestination.id == destination_id
        )
    )
    dest = result.scalar_one_or_none()
    
    if not dest:
        raise HTTPException(status_code=404, detail="Destination not found")
    
    await db.delete(dest)
    await db.commit()
    
    return {"success": True, "message": "Destination deleted"}


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
    # Check cache first
    cache_key = f"summary_{period}_{server_ids or 'all'}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached
    
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
        
        # Unique destinations (now using destination_id)
        dest_query = select(sql_func.count(sql_func.distinct(XrayVisitStats.destination_id)))
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
    
    response = {
        "period": period,
        "total_visits": total_visits,
        "unique_users": unique_users,
        "unique_destinations": unique_destinations
    }
    _set_cached(cache_key, response)
    return response


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
    # Cache only for unfiltered requests (most common case)
    cache_key = None
    if not email and not server_id:
        cache_key = f"top_dest_{period}_{limit}"
        cached = _get_cached(cache_key)
        if cached is not None:
            return cached
    
    conditions = []
    
    if period != "all":
        start_time = _get_time_filter(period)
        conditions.append(XrayVisitStats.last_seen >= start_time)
    
    if email:
        conditions.append(XrayVisitStats.email == email)
    if server_id:
        conditions.append(XrayVisitStats.server_id == server_id)
    
    # JOIN with XrayDestination, aggregate by host (pre-computed, no regexp)
    query = select(
        XrayDestination.host.label('destination'),
        sql_func.sum(XrayVisitStats.visit_count).label('total')
    ).join(XrayDestination, XrayVisitStats.destination_id == XrayDestination.id)
    
    if conditions:
        query = query.where(and_(*conditions))
    
    query = query.group_by(XrayDestination.host) \
                 .order_by(sql_func.sum(XrayVisitStats.visit_count).desc()) \
                 .limit(limit)
    
    result = await db.execute(query)
    rows = result.fetchall()
    
    response = {
        "period": period,
        "destinations": [
            {
                "destination": row.destination,
                "visits": row.total
            }
            for row in rows
        ]
    }
    
    if cache_key:
        _set_cached(cache_key, response)
    return response


@router.get("/stats/top-users")
async def get_top_users(
    period: str = Query("all", pattern="^(1h|24h|7d|30d|365d|all)$"),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    server_id: Optional[int] = Query(None, description="Filter by server"),
    search: Optional[str] = Query(None, min_length=1, description="Search by email ID or username"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get top active users with search and pagination"""
    # Cache only for standard requests (first page, no search, no server filter)
    cache_key = None
    if not search and not server_id and offset == 0:
        cache_key = f"top_users_{period}_{limit}"
        cached = _get_cached(cache_key)
        if cached is not None:
            return cached
    
    conditions = []
    start_time = None
    
    if period != "all":
        start_time = _get_time_filter(period)
        conditions.append(XrayVisitStats.last_seen >= start_time)
    
    if server_id:
        conditions.append(XrayVisitStats.server_id == server_id)
    
    # If searching, find matching user IDs by email or username
    search_user_ids = set()
    if search:
        search = search.strip()
        
        # Always search by username (works for both text and numbers like "772199094" in "tg_1772199094_...")
        cache_search = await db.execute(
            select(RemnawaveUserCache.email)
            .where(RemnawaveUserCache.username.ilike(f"%{search}%"))
            .limit(1000)
        )
        for row in cache_search.fetchall():
            search_user_ids.add(row[0])
        
        # If search is a number, also search by exact email ID
        if search.isdigit():
            search_user_ids.add(int(search))
        
        if not search_user_ids:
            # No matches found
            return {"period": period, "users": [], "total": 0, "offset": offset, "limit": limit}
        
        conditions.append(XrayVisitStats.email.in_(list(search_user_ids)))
    
    # Count total users matching criteria
    count_query = select(sql_func.count(sql_func.distinct(XrayVisitStats.email)))
    if conditions:
        count_query = count_query.where(and_(*conditions))
    total_result = await db.execute(count_query)
    total_count = total_result.scalar() or 0
    
    # Get users with stats (unique_sites counted by host, no regexp)
    query = select(
        XrayVisitStats.email,
        sql_func.sum(XrayVisitStats.visit_count).label('total'),
        sql_func.count(sql_func.distinct(XrayDestination.host)).label('unique_sites')
    ).join(XrayDestination, XrayVisitStats.destination_id == XrayDestination.id)
    
    if conditions:
        query = query.where(and_(*conditions))
    
    query = query.group_by(XrayVisitStats.email) \
                 .order_by(sql_func.sum(XrayVisitStats.visit_count).desc()) \
                 .offset(offset) \
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
    
    # Get unique client IP counts and infrastructure IP counts in a SINGLE query
    ip_counts = {}
    infra_ip_counts = {}
    if user_ids:
        ip_conditions = [XrayUserIpStats.email.in_(user_ids)]
        if period != "all" and start_time:
            ip_conditions.append(XrayUserIpStats.last_seen >= start_time)
        if server_id:
            ip_conditions.append(XrayUserIpStats.server_id == server_id)
        
        ip_result = await db.execute(
            select(
                XrayUserIpStats.email,
                sql_func.count(sql_func.distinct(
                    case((XrayUserIpStats.is_infrastructure == False, XrayUserIpStats.source_ip_id), else_=None)
                )).label('client_ips'),
                sql_func.count(sql_func.distinct(
                    case((XrayUserIpStats.is_infrastructure == True, XrayUserIpStats.source_ip_id), else_=None)
                )).label('infra_ips')
            )
            .where(and_(*ip_conditions))
            .group_by(XrayUserIpStats.email)
        )
        for ip_row in ip_result.fetchall():
            ip_counts[ip_row.email] = ip_row.client_ips
            infra_ip_counts[ip_row.email] = ip_row.infra_ips
    
    response = {
        "period": period,
        "total": total_count,
        "offset": offset,
        "limit": limit,
        "users": [
            {
                "email": row.email,
                "username": user_cache.get(row.email, {}).get("username"),
                "status": user_cache.get(row.email, {}).get("status"),
                "total_visits": row.total,
                "unique_sites": row.unique_sites,
                "unique_ips": ip_counts.get(row.email, 0),
                "infrastructure_ips": infra_ip_counts.get(row.email, 0)
            }
            for row in rows
        ]
    }
    
    if cache_key:
        _set_cached(cache_key, response)
    return response


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
    
    # Top destinations for this user (aggregated by host, across servers)
    dest_result = await db.execute(
        select(
            XrayDestination.host.label('destination'),
            sql_func.sum(XrayVisitStats.visit_count).label('visit_count'),
            sql_func.min(XrayVisitStats.first_seen).label('first_seen'),
            sql_func.max(XrayVisitStats.last_seen).label('last_seen')
        )
        .join(XrayDestination, XrayVisitStats.destination_id == XrayDestination.id)
        .where(and_(*conditions))
        .group_by(XrayDestination.host)
        .order_by(sql_func.sum(XrayVisitStats.visit_count).desc())
        .limit(limit)
    )
    
    destinations = dest_result.fetchall()
    
    # Get unique client IPs count (excluding infrastructure)
    unique_client_ips_result = await db.execute(
        select(sql_func.count(sql_func.distinct(XrayUserIpStats.source_ip_id)))
        .where(and_(*ip_conditions, XrayUserIpStats.is_infrastructure == False))
    )
    unique_client_ips = unique_client_ips_result.scalar() or 0
    
    # Get total unique IPs count
    unique_ips_result = await db.execute(
        select(sql_func.count(sql_func.distinct(XrayUserIpStats.source_ip_id)))
        .where(and_(*ip_conditions))
    )
    unique_ips = unique_ips_result.scalar() or 0
    
    # Get IP details with server info and infrastructure flag (JOIN with XraySourceIp)
    ip_result = await db.execute(
        select(
            XraySourceIp.ip.label('source_ip'),
            XrayUserIpStats.server_id,
            XrayUserIpStats.connection_count,
            XrayUserIpStats.is_infrastructure,
            XrayUserIpStats.first_seen,
            XrayUserIpStats.last_seen,
            Server.name.label('server_name')
        )
        .join(XraySourceIp, XrayUserIpStats.source_ip_id == XraySourceIp.id)
        .join(Server, XrayUserIpStats.server_id == Server.id)
        .where(and_(*ip_conditions))
        .order_by(XrayUserIpStats.connection_count.desc())
    )
    ip_rows = ip_result.fetchall()
    
    # Aggregate IPs across servers, separating client and infrastructure
    client_ip_map: dict[str, dict] = {}
    infra_ip_map: dict[str, dict] = {}
    
    for row in ip_rows:
        target_map = infra_ip_map if row.is_infrastructure else client_ip_map
        
        if row.source_ip not in target_map:
            target_map[row.source_ip] = {
                "source_ip": row.source_ip,
                "servers": [],
                "total_count": 0,
                "first_seen": row.first_seen,
                "last_seen": row.last_seen
            }
        target_map[row.source_ip]["servers"].append({
            "server_id": row.server_id,
            "server_name": row.server_name,
            "count": row.connection_count
        })
        target_map[row.source_ip]["total_count"] += row.connection_count
        if row.first_seen and (not target_map[row.source_ip]["first_seen"] or row.first_seen < target_map[row.source_ip]["first_seen"]):
            target_map[row.source_ip]["first_seen"] = row.first_seen
        if row.last_seen and (not target_map[row.source_ip]["last_seen"] or row.last_seen > target_map[row.source_ip]["last_seen"]):
            target_map[row.source_ip]["last_seen"] = row.last_seen
    
    # Sort IPs by total count
    client_ips = sorted(client_ip_map.values(), key=lambda x: x["total_count"], reverse=True)
    infra_ips = sorted(infra_ip_map.values(), key=lambda x: x["total_count"], reverse=True)
    
    def format_ip_list(ips_list, max_items=50):
        return [
            {
                "source_ip": ip["source_ip"],
                "servers": ip["servers"],
                "total_count": ip["total_count"],
                "first_seen": ip["first_seen"].isoformat() if ip["first_seen"] else None,
                "last_seen": ip["last_seen"].isoformat() if ip["last_seen"] else None
            }
            for ip in ips_list[:max_items]
        ]
    
    return {
        "email": email,
        "username": user.username if user else None,
        "status": user.status if user else None,
        "period": period,
        "total_visits": total_visits,
        "unique_ips": unique_ips,
        "unique_client_ips": unique_client_ips,
        "destinations": [
            {
                "destination": row.destination,
                "visits": row.visit_count,
                "first_seen": row.first_seen.isoformat() if row.first_seen else None,
                "last_seen": row.last_seen.isoformat() if row.last_seen else None
            }
            for row in destinations
        ],
        # For backwards compatibility, "ips" contains only client IPs
        "ips": format_ip_list(client_ips),
        # New fields for separated display
        "client_ips": format_ip_list(client_ips),
        "infrastructure_ips": format_ip_list(infra_ips)
    }


@router.delete("/stats/user/{email}/ips/{source_ip}")
async def delete_user_ip(
    email: int,
    source_ip: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete a specific IP address from user's statistics."""
    from urllib.parse import unquote
    
    source_ip = unquote(source_ip)
    
    # Find source_ip_id
    ip_lookup = await db.execute(
        select(XraySourceIp.id).where(XraySourceIp.ip == source_ip)
    )
    ip_row = ip_lookup.scalar_one_or_none()
    
    if not ip_row:
        return {
            "success": True,
            "email": email,
            "source_ip": source_ip,
            "deleted_ip_records": 0,
            "deleted_destination_records": 0,
            "message": "IP not found"
        }
    
    source_ip_id = ip_row
    
    ip_result = await db.execute(
        delete(XrayUserIpStats).where(
            and_(
                XrayUserIpStats.email == email,
                XrayUserIpStats.source_ip_id == source_ip_id
            )
        )
    )
    deleted_ip_count = ip_result.rowcount
    
    dest_result = await db.execute(
        delete(XrayIpDestinationStats).where(
            and_(
                XrayIpDestinationStats.email == email,
                XrayIpDestinationStats.source_ip_id == source_ip_id
            )
        )
    )
    deleted_dest_count = dest_result.rowcount
    
    await db.commit()
    _invalidate_cache()
    
    return {
        "success": True,
        "email": email,
        "source_ip": source_ip,
        "deleted_ip_records": deleted_ip_count,
        "deleted_destination_records": deleted_dest_count,
        "message": f"Deleted IP {source_ip} from user {email}"
    }


@router.delete("/stats/user/{email}/ips")
async def delete_user_all_ips(
    email: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete all IP addresses from user's statistics."""
    ip_result = await db.execute(
        delete(XrayUserIpStats).where(XrayUserIpStats.email == email)
    )
    deleted_ip_count = ip_result.rowcount
    
    dest_result = await db.execute(
        delete(XrayIpDestinationStats).where(XrayIpDestinationStats.email == email)
    )
    deleted_dest_count = dest_result.rowcount
    
    await db.commit()
    _invalidate_cache()
    
    return {
        "success": True,
        "email": email,
        "deleted_ip_records": deleted_ip_count,
        "deleted_destination_records": deleted_dest_count,
        "message": f"Deleted all IPs for user {email}"
    }


@router.get("/stats/destination/users")
async def get_destination_users(
    destination: str = Query(..., description="Destination to get users for"),
    period: str = Query("all", pattern="^(1h|24h|7d|30d|365d|all)$"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get users who visited a specific destination (matches by host, ignoring port)"""
    host = re.sub(r':\d+$', '', destination)
    
    dest_result = await db.execute(
        select(XrayDestination.id).where(XrayDestination.host == host)
    )
    dest_ids = [row[0] for row in dest_result.fetchall()]
    
    if not dest_ids:
        return {
            "destination": destination,
            "period": period,
            "total_visits": 0,
            "users": []
        }
    
    conditions = [XrayVisitStats.destination_id.in_(dest_ids)]
    
    if period != "all":
        start_time = _get_time_filter(period)
        conditions.append(XrayVisitStats.last_seen >= start_time)
    
    # Get total visits for this destination
    total_result = await db.execute(
        select(sql_func.sum(XrayVisitStats.visit_count))
        .where(and_(*conditions))
    )
    total_visits = total_result.scalar() or 0
    
    # Get users with their visit counts (aggregated across destination ports and servers)
    result = await db.execute(
        select(
            XrayVisitStats.email,
            sql_func.sum(XrayVisitStats.visit_count).label('visit_count'),
            sql_func.min(XrayVisitStats.first_seen).label('first_seen'),
            sql_func.max(XrayVisitStats.last_seen).label('last_seen')
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
        "destination": host,
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
    # Find source_ip_id
    ip_lookup = await db.execute(
        select(XraySourceIp.id).where(XraySourceIp.ip == source_ip)
    )
    ip_id = ip_lookup.scalar_one_or_none()
    
    if not ip_id:
        return {
            "source_ip": source_ip,
            "email": email,
            "period": period,
            "total_connections": 0,
            "destinations": []
        }
    
    conditions = [
        XrayIpDestinationStats.source_ip_id == ip_id,
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
    
    # Get destinations with connection counts (JOIN with XrayDestination)
    result = await db.execute(
        select(
            XrayDestination.destination,
            sql_func.sum(XrayIpDestinationStats.connection_count).label('total'),
            sql_func.max(XrayIpDestinationStats.last_seen).label('last_seen')
        )
        .join(XrayDestination, XrayIpDestinationStats.destination_id == XrayDestination.id)
        .where(and_(*conditions))
        .group_by(XrayDestination.destination)
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


@router.post("/users/refresh")
async def refresh_user_cache(
    _: dict = Depends(verify_auth)
):
    """Force immediate refresh of Remnawave user cache.
    
    This fetches all users from Remnawave API and updates the local cache.
    Useful when users are added/removed in Remnawave and you want to see
    updated statuses immediately without waiting for the hourly sync.
    """
    collector = get_xray_stats_collector()
    result = await collector.refresh_user_cache_now()
    return result


@router.get("/users/cache-status")
async def get_user_cache_status(
    _: dict = Depends(verify_auth)
):
    """Get user cache status (last update time, update in progress)."""
    collector = get_xray_stats_collector()
    return collector.get_user_cache_status()


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
    """Get database statistics for monitoring including table sizes"""
    # Cache db-info for longer (5 minutes) as it changes rarely
    cache_key = "db_info"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached
    
    from sqlalchemy import text
    
    # Count records in each table
    visit_count = await db.execute(select(sql_func.count()).select_from(XrayVisitStats))
    hourly_count = await db.execute(select(sql_func.count()).select_from(XrayHourlyStats))
    user_count = await db.execute(select(sql_func.count()).select_from(RemnawaveUserCache))
    ip_count = await db.execute(select(sql_func.count()).select_from(XrayUserIpStats))
    ip_dest_count = await db.execute(select(sql_func.count()).select_from(XrayIpDestinationStats))
    dest_count = await db.execute(select(sql_func.count()).select_from(XrayDestination))
    source_ip_count = await db.execute(select(sql_func.count()).select_from(XraySourceIp))
    
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
    
    # Get table sizes from PostgreSQL
    table_sizes = {}
    total_size = 0
    try:
        # Query to get table sizes including indexes
        size_query = text("""
            SELECT 
                relname as table_name,
                pg_total_relation_size(relid) as total_size
            FROM pg_catalog.pg_statio_user_tables
            WHERE relname IN (
                'xray_visit_stats', 
                'xray_hourly_stats', 
                'xray_user_ip_stats',
                'xray_ip_destination_stats',
                'xray_destinations',
                'xray_source_ips',
                'remnawave_user_cache'
            )
        """)
        size_result = await db.execute(size_query)
        for row in size_result.fetchall():
            table_sizes[row[0]] = row[1]
            total_size += row[1]
    except Exception:
        # Fallback if query fails
        pass
    
    response = {
        "tables": {
            "xray_visit_stats": {
                "count": visit_count.scalar() or 0,
                "first_seen": v_range[0].isoformat() if v_range[0] else None,
                "last_seen": v_range[1].isoformat() if v_range[1] else None,
                "size_bytes": table_sizes.get("xray_visit_stats")
            },
            "xray_hourly_stats": {
                "count": hourly_count.scalar() or 0,
                "first_hour": h_range[0].isoformat() if h_range[0] else None,
                "last_hour": h_range[1].isoformat() if h_range[1] else None,
                "size_bytes": table_sizes.get("xray_hourly_stats")
            },
            "xray_user_ip_stats": {
                "count": ip_count.scalar() or 0,
                "size_bytes": table_sizes.get("xray_user_ip_stats")
            },
            "xray_ip_destination_stats": {
                "count": ip_dest_count.scalar() or 0,
                "size_bytes": table_sizes.get("xray_ip_destination_stats")
            },
            "xray_destinations": {
                "count": dest_count.scalar() or 0,
                "size_bytes": table_sizes.get("xray_destinations")
            },
            "xray_source_ips": {
                "count": source_ip_count.scalar() or 0,
                "size_bytes": table_sizes.get("xray_source_ips")
            },
            "remnawave_user_cache": {
                "count": user_count.scalar() or 0,
                "size_bytes": table_sizes.get("remnawave_user_cache")
            }
        },
        "total_size_bytes": total_size if total_size > 0 else None
    }
    _set_cached(cache_key, response)
    return response


@router.delete("/stats/clear")
async def clear_stats(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Clear all visit statistics (visits, IPs, IP-destinations, hourly stats, destinations).
    
    WARNING: This permanently deletes all collected visit data.
    User cache is NOT deleted (can be refreshed from Remnawave API).
    
    Uses TRUNCATE CASCADE for fast deletion of large tables.
    """
    from sqlalchemy import text
    
    # Get counts before truncate for response
    visit_count = await db.execute(select(sql_func.count()).select_from(XrayVisitStats))
    deleted_visits = visit_count.scalar() or 0
    
    ip_dest_count = await db.execute(select(sql_func.count()).select_from(XrayIpDestinationStats))
    deleted_ip_dests = ip_dest_count.scalar() or 0
    
    ip_count = await db.execute(select(sql_func.count()).select_from(XrayUserIpStats))
    deleted_ips = ip_count.scalar() or 0
    
    hourly_count = await db.execute(select(sql_func.count()).select_from(XrayHourlyStats))
    deleted_hourly = hourly_count.scalar() or 0
    
    dest_count = await db.execute(select(sql_func.count()).select_from(XrayDestination))
    deleted_dests = dest_count.scalar() or 0
    
    source_ips_count = await db.execute(select(sql_func.count()).select_from(XraySourceIp))
    deleted_source_ips = source_ips_count.scalar() or 0
    
    # TRUNCATE — instant for millions of rows, CASCADE handles FK dependencies
    await db.execute(text("""
        TRUNCATE TABLE 
            xray_visit_stats, 
            xray_ip_destination_stats, 
            xray_user_ip_stats, 
            xray_hourly_stats, 
            xray_destinations,
            xray_source_ips 
        CASCADE
    """))
    
    await db.commit()
    
    # Invalidate all stats cache
    _invalidate_cache()
    
    return {
        "success": True,
        "deleted": {
            "visit_stats": deleted_visits,
            "ip_stats": deleted_ips,
            "ip_destination_stats": deleted_ip_dests,
            "hourly_stats": deleted_hourly,
            "destinations": deleted_dests,
            "source_ips": deleted_source_ips
        },
        "message": f"Deleted {deleted_visits} visit records, {deleted_ips} IP records, {deleted_ip_dests} IP-destination records, {deleted_hourly} hourly records, {deleted_dests} destinations, {deleted_source_ips} source IPs"
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


# === Traffic Analyzer Endpoints ===

@router.get("/analyzer/settings")
async def get_analyzer_settings(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get traffic analyzer settings"""
    result = await db.execute(select(TrafficAnalyzerSettings).limit(1))
    settings = result.scalar_one_or_none()
    
    if not settings:
        return {
            "enabled": False,
            "check_interval_minutes": 30,
            "traffic_limit_gb": 100.0,
            "ip_limit_multiplier": 2.0,
            "check_hwid_anomalies": True,
            "telegram_bot_token": None,
            "telegram_chat_id": None,
            "last_check_at": None,
            "last_error": None
        }
    
    return {
        "enabled": settings.enabled,
        "check_interval_minutes": settings.check_interval_minutes,
        "traffic_limit_gb": settings.traffic_limit_gb,
        "ip_limit_multiplier": settings.ip_limit_multiplier,
        "check_hwid_anomalies": settings.check_hwid_anomalies,
        "telegram_bot_token": "***" if settings.telegram_bot_token else None,
        "telegram_chat_id": settings.telegram_chat_id,
        "last_check_at": settings.last_check_at.isoformat() if settings.last_check_at else None,
        "last_error": settings.last_error
    }


@router.put("/analyzer/settings")
async def update_analyzer_settings(
    request: UpdateAnalyzerSettingsRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Update traffic analyzer settings"""
    result = await db.execute(select(TrafficAnalyzerSettings).limit(1))
    settings = result.scalar_one_or_none()
    
    if not settings:
        settings = TrafficAnalyzerSettings()
        db.add(settings)
    
    if request.enabled is not None:
        settings.enabled = request.enabled
    if request.check_interval_minutes is not None:
        settings.check_interval_minutes = request.check_interval_minutes
    if request.traffic_limit_gb is not None:
        settings.traffic_limit_gb = request.traffic_limit_gb
    if request.ip_limit_multiplier is not None:
        settings.ip_limit_multiplier = request.ip_limit_multiplier
    if request.check_hwid_anomalies is not None:
        settings.check_hwid_anomalies = request.check_hwid_anomalies
    if request.telegram_bot_token is not None:
        settings.telegram_bot_token = request.telegram_bot_token if request.telegram_bot_token != "***" else settings.telegram_bot_token
    if request.telegram_chat_id is not None:
        settings.telegram_chat_id = request.telegram_chat_id
    
    await db.commit()
    
    return {"success": True, "message": "Analyzer settings updated"}


@router.get("/analyzer/status")
async def get_analyzer_status(
    _: dict = Depends(verify_auth)
):
    """Get traffic analyzer status"""
    analyzer = get_traffic_analyzer()
    return analyzer.get_status()


@router.post("/analyzer/check")
async def force_analyzer_check(
    _: dict = Depends(verify_auth)
):
    """Force immediate analyzer check"""
    analyzer = get_traffic_analyzer()
    return await analyzer.analyze_now()


@router.post("/analyzer/test-telegram")
async def test_telegram_notification(
    request: TestTelegramRequest,
    _: dict = Depends(verify_auth)
):
    """Test Telegram notification"""
    analyzer = get_traffic_analyzer()
    return await analyzer.test_telegram(request.bot_token, request.chat_id)


@router.get("/analyzer/anomalies")
async def get_anomalies(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    anomaly_type: Optional[str] = Query(None, pattern="^(traffic|ip_count|hwid)$"),
    resolved: Optional[bool] = Query(None),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get list of anomaly logs with pagination and filters"""
    import json as json_module
    
    conditions = []
    
    if anomaly_type:
        conditions.append(TrafficAnomalyLog.anomaly_type == anomaly_type)
    if resolved is not None:
        conditions.append(TrafficAnomalyLog.resolved == resolved)
    
    # Count total
    count_query = select(sql_func.count()).select_from(TrafficAnomalyLog)
    if conditions:
        count_query = count_query.where(and_(*conditions))
    total_result = await db.execute(count_query)
    total_count = total_result.scalar() or 0
    
    # Get anomalies
    query = select(TrafficAnomalyLog).order_by(TrafficAnomalyLog.created_at.desc())
    if conditions:
        query = query.where(and_(*conditions))
    query = query.offset(offset).limit(limit)
    
    result = await db.execute(query)
    anomalies = result.scalars().all()
    
    # Get user info from cache
    user_ids = [a.user_email for a in anomalies]
    user_cache = {}
    if user_ids:
        cache_result = await db.execute(
            select(RemnawaveUserCache).where(RemnawaveUserCache.email.in_(user_ids))
        )
        for user in cache_result.scalars().all():
            user_cache[user.email] = {
                "username": user.username,
                "telegram_id": user.telegram_id
            }
    
    return {
        "total": total_count,
        "offset": offset,
        "limit": limit,
        "anomalies": [
            {
                "id": a.id,
                "user_email": a.user_email,
                "username": a.username or user_cache.get(a.user_email, {}).get("username"),
                "telegram_id": user_cache.get(a.user_email, {}).get("telegram_id"),
                "anomaly_type": a.anomaly_type,
                "severity": a.severity,
                "details": json_module.loads(a.details) if a.details else None,
                "notified": a.notified,
                "resolved": a.resolved,
                "created_at": a.created_at.isoformat() if a.created_at else None
            }
            for a in anomalies
        ]
    }


@router.delete("/analyzer/anomalies/all")
async def delete_all_anomalies(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete all anomalies"""
    result = await db.execute(delete(TrafficAnomalyLog))
    deleted = result.rowcount
    await db.commit()
    
    return {
        "success": True,
        "deleted": deleted,
        "message": f"Deleted {deleted} anomalies"
    }


@router.delete("/analyzer/anomalies/clear")
async def clear_old_anomalies(
    days: int = Query(30, ge=1, le=365, description="Delete anomalies older than N days"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Clear old anomaly logs"""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    
    result = await db.execute(
        delete(TrafficAnomalyLog).where(TrafficAnomalyLog.created_at < cutoff)
    )
    deleted = result.rowcount
    await db.commit()
    
    return {
        "success": True,
        "deleted": deleted,
        "message": f"Deleted {deleted} anomalies older than {days} days"
    }


@router.put("/analyzer/anomalies/{anomaly_id}/resolve")
async def resolve_anomaly(
    anomaly_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Mark anomaly as resolved"""
    result = await db.execute(
        select(TrafficAnomalyLog).where(TrafficAnomalyLog.id == anomaly_id)
    )
    anomaly = result.scalar_one_or_none()
    
    if not anomaly:
        raise HTTPException(status_code=404, detail="Anomaly not found")
    
    anomaly.resolved = True
    await db.commit()
    
    return {"success": True, "message": "Anomaly marked as resolved"}


@router.delete("/analyzer/anomalies/{anomaly_id}")
async def delete_anomaly(
    anomaly_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete single anomaly by ID"""
    result = await db.execute(
        select(TrafficAnomalyLog).where(TrafficAnomalyLog.id == anomaly_id)
    )
    anomaly = result.scalar_one_or_none()
    
    if not anomaly:
        raise HTTPException(status_code=404, detail="Anomaly not found")
    
    await db.delete(anomaly)
    await db.commit()
    
    return {"success": True, "message": "Anomaly deleted"}


# === Export Endpoints ===

class ExportSettingsRequest(BaseModel):
    period: str = Field("all", pattern="^(1h|24h|7d|30d|365d|all)$")
    include_user_id: bool = True
    include_username: bool = True
    include_status: bool = True
    include_telegram_id: bool = False
    include_destinations: bool = True
    include_visits_count: bool = True
    include_first_seen: bool = True
    include_last_seen: bool = True
    include_client_ips: bool = False
    include_infra_ips: bool = False
    include_traffic: bool = False


@router.post("/export/create")
async def create_export(
    request: ExportSettingsRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Create a new export task."""
    import json
    import subprocess
    import sys
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"remnawave_export_{timestamp}.xlsx"
    
    settings_dict = request.model_dump()
    
    export_record = RemnawaveExport(
        filename=filename,
        format="xlsx",
        status="pending",
        settings=json.dumps(settings_dict)
    )
    db.add(export_record)
    await db.commit()
    await db.refresh(export_record)
    
    # Start export in separate process to avoid blocking event loop
    worker_path = os.path.join(os.path.dirname(__file__), "..", "export_worker.py")
    
    # Worker has its own logging, just spawn it
    subprocess.Popen(
        [sys.executable, worker_path, str(export_record.id), json.dumps(settings_dict)],
        start_new_session=True
    )
    
    return {
        "success": True,
        "export_id": export_record.id,
        "filename": filename,
        "status": "pending"
    }


@router.get("/export/list")
async def list_exports(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get list of exports."""
    result = await db.execute(
        select(RemnawaveExport).order_by(RemnawaveExport.created_at.desc()).limit(20)
    )
    exports = result.scalars().all()
    
    return {
        "exports": [
            {
                "id": e.id,
                "filename": e.filename,
                "format": e.format,
                "status": e.status,
                "file_size": e.file_size,
                "rows_count": e.rows_count,
                "error_message": e.error_message,
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "completed_at": e.completed_at.isoformat() if e.completed_at else None
            }
            for e in exports
        ]
    }


@router.get("/export/{export_id}/download")
async def download_export(
    export_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Download export file."""
    import os
    from fastapi.responses import FileResponse
    
    export_record = await db.get(RemnawaveExport, export_id)
    if not export_record:
        raise HTTPException(status_code=404, detail="Export not found")
    
    if export_record.status != "completed":
        raise HTTPException(status_code=400, detail=f"Export is {export_record.status}")
    
    exports_dir = os.path.join(os.path.dirname(__file__), "..", "exports")
    file_path = os.path.join(exports_dir, export_record.filename)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    media_types = {
        "csv": "text/csv",
        "json": "application/json",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    }
    
    return FileResponse(
        file_path,
        media_type=media_types.get(export_record.format, "application/octet-stream"),
        filename=export_record.filename
    )


@router.delete("/export/{export_id}")
async def delete_export(
    export_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete export and its file."""
    import os
    
    export_record = await db.get(RemnawaveExport, export_id)
    if not export_record:
        raise HTTPException(status_code=404, detail="Export not found")
    
    # Delete file if exists
    exports_dir = os.path.join(os.path.dirname(__file__), "..", "exports")
    file_path = os.path.join(exports_dir, export_record.filename)
    
    if os.path.exists(file_path):
        os.remove(file_path)
    
    await db.delete(export_record)
    await db.commit()
    
    return {"success": True, "message": "Export deleted"}
