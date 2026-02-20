"""Blocklist management router for IP/CIDR blocking (incoming + outgoing)

All rule mutations (add/delete global, server, source toggle) trigger
background sync so changes are applied to nodes automatically.
"""

import asyncio
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import BlocklistRule, BlocklistSource, Server, PanelSettings
from app.services.blocklist_manager import get_blocklist_manager

router = APIRouter(prefix="/blocklist", tags=["blocklist"])


# Request/Response models
class AddRuleRequest(BaseModel):
    ip_cidr: str = Field(..., description="IP address or CIDR notation")
    is_permanent: bool = Field(True, description="Permanent (True) or temporary (False)")
    direction: str = Field("in", pattern="^(in|out)$", description="Traffic direction: in or out")
    comment: Optional[str] = Field(None, max_length=200)


class BulkAddRequest(BaseModel):
    ips: list[str] = Field(..., description="List of IP addresses or CIDR notations")
    is_permanent: bool = Field(True)
    direction: str = Field("in", pattern="^(in|out)$")


class AddSourceRequest(BaseModel):
    name: str = Field(..., max_length=100)
    url: str = Field(..., max_length=500)
    direction: str = Field("in", pattern="^(in|out)$")


class UpdateSourceRequest(BaseModel):
    enabled: Optional[bool] = None
    name: Optional[str] = Field(None, max_length=100)


class UpdateSettingsRequest(BaseModel):
    temp_timeout: Optional[int] = Field(None, ge=1, le=2592000)
    auto_update_enabled: Optional[bool] = None
    auto_update_interval: Optional[int] = Field(None, ge=3600, le=604800)


class TorrentBlockerSettingsRequest(BaseModel):
    behavior_threshold: int = Field(..., ge=5, le=1000)


class GlobalTorrentSettingsRequest(BaseModel):
    behavior_threshold: int = Field(..., ge=5, le=1000)


class TorrentWhitelistRequest(BaseModel):
    whitelist: list[str] = Field(..., max_length=500)


# === Global Rules ===

@router.get("/global")
async def get_global_rules(
    direction: str = Query("in", pattern="^(in|out)$"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get all global blocklist rules filtered by direction"""
    result = await db.execute(
        select(BlocklistRule).where(
            and_(
                BlocklistRule.server_id.is_(None),
                BlocklistRule.direction == direction
            )
        ).order_by(BlocklistRule.created_at.desc())
    )
    rules = result.scalars().all()

    return {
        "count": len(rules),
        "direction": direction,
        "rules": [
            {
                "id": r.id,
                "ip_cidr": r.ip_cidr,
                "is_permanent": r.is_permanent,
                "direction": r.direction or "in",
                "comment": r.comment,
                "source": r.source,
                "created_at": r.created_at.isoformat() if r.created_at else None
            }
            for r in rules
        ]
    }


@router.post("/global")
async def add_global_rule(
    request: AddRuleRequest,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add global blocklist rule (applies to all servers). Auto-syncs."""
    manager = get_blocklist_manager()

    if not manager._validate_ip_cidr(request.ip_cidr):
        raise HTTPException(status_code=400, detail="Invalid IP/CIDR format")

    normalized = manager._normalize_ip(request.ip_cidr)

    result = await db.execute(
        select(BlocklistRule).where(
            and_(
                BlocklistRule.ip_cidr == normalized,
                BlocklistRule.server_id.is_(None),
                BlocklistRule.direction == request.direction
            )
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Rule already exists")

    rule = BlocklistRule(
        ip_cidr=normalized,
        server_id=None,
        is_permanent=request.is_permanent,
        direction=request.direction,
        comment=request.comment,
        source="manual"
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)

    bg.add_task(manager.sync_all_nodes)

    return {
        "success": True,
        "rule": {
            "id": rule.id,
            "ip_cidr": rule.ip_cidr,
            "is_permanent": rule.is_permanent,
            "direction": rule.direction,
            "comment": rule.comment
        }
    }


@router.post("/global/bulk")
async def add_global_rules_bulk(
    request: BulkAddRequest,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add multiple global blocklist rules. Auto-syncs."""
    manager = get_blocklist_manager()

    added = 0
    skipped = 0
    invalid = []

    for ip in request.ips:
        if not manager._validate_ip_cidr(ip):
            invalid.append(ip)
            continue

        normalized = manager._normalize_ip(ip)

        result = await db.execute(
            select(BlocklistRule).where(
                and_(
                    BlocklistRule.ip_cidr == normalized,
                    BlocklistRule.server_id.is_(None),
                    BlocklistRule.direction == request.direction
                )
            )
        )
        if result.scalar_one_or_none():
            skipped += 1
            continue

        rule = BlocklistRule(
            ip_cidr=normalized,
            server_id=None,
            is_permanent=request.is_permanent,
            direction=request.direction,
            source="manual"
        )
        db.add(rule)
        added += 1

    await db.commit()

    if added > 0:
        bg.add_task(manager.sync_all_nodes)

    return {
        "success": True,
        "added": added,
        "skipped": skipped,
        "invalid": invalid[:10],
        "direction": request.direction
    }


@router.delete("/global/{rule_id}")
async def delete_global_rule(
    rule_id: int,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete global blocklist rule. Auto-syncs."""
    result = await db.execute(
        select(BlocklistRule).where(
            and_(
                BlocklistRule.id == rule_id,
                BlocklistRule.server_id.is_(None)
            )
        )
    )
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    await db.delete(rule)
    await db.commit()

    bg.add_task(manager_sync_all)

    return {"success": True, "message": "Rule deleted"}


async def manager_sync_all():
    manager = get_blocklist_manager()
    await manager.sync_all_nodes()


async def manager_sync_single(server_id: int):
    manager = get_blocklist_manager()
    await manager.sync_single_node_by_id(server_id)


# === Server-specific Rules ===

@router.get("/server/{server_id}")
async def get_server_rules(
    server_id: int,
    direction: str = Query("in", pattern="^(in|out)$"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get blocklist rules for specific server"""
    result = await db.execute(select(Server).where(Server.id == server_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Server not found")

    result = await db.execute(
        select(BlocklistRule).where(
            and_(
                BlocklistRule.server_id == server_id,
                BlocklistRule.direction == direction
            )
        ).order_by(BlocklistRule.created_at.desc())
    )
    rules = result.scalars().all()

    global_result = await db.execute(
        select(BlocklistRule).where(
            and_(
                BlocklistRule.server_id.is_(None),
                BlocklistRule.direction == direction
            )
        )
    )
    global_count = len(global_result.scalars().all())

    return {
        "server_id": server_id,
        "direction": direction,
        "count": len(rules),
        "global_count": global_count,
        "rules": [
            {
                "id": r.id,
                "ip_cidr": r.ip_cidr,
                "is_permanent": r.is_permanent,
                "direction": r.direction or "in",
                "comment": r.comment,
                "source": r.source,
                "created_at": r.created_at.isoformat() if r.created_at else None
            }
            for r in rules
        ]
    }


@router.post("/server/{server_id}")
async def add_server_rule(
    server_id: int,
    request: AddRuleRequest,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add blocklist rule for specific server. Auto-syncs that server."""
    result = await db.execute(select(Server).where(Server.id == server_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Server not found")

    manager = get_blocklist_manager()

    if not manager._validate_ip_cidr(request.ip_cidr):
        raise HTTPException(status_code=400, detail="Invalid IP/CIDR format")

    normalized = manager._normalize_ip(request.ip_cidr)

    result = await db.execute(
        select(BlocklistRule).where(
            and_(
                BlocklistRule.ip_cidr == normalized,
                BlocklistRule.server_id == server_id,
                BlocklistRule.direction == request.direction
            )
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Rule already exists for this server")

    rule = BlocklistRule(
        ip_cidr=normalized,
        server_id=server_id,
        is_permanent=request.is_permanent,
        direction=request.direction,
        comment=request.comment,
        source="manual"
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)

    bg.add_task(manager_sync_single, server_id)

    return {
        "success": True,
        "rule": {
            "id": rule.id,
            "ip_cidr": rule.ip_cidr,
            "server_id": server_id,
            "is_permanent": rule.is_permanent,
            "direction": rule.direction,
            "comment": rule.comment
        }
    }


@router.delete("/server/{server_id}/{rule_id}")
async def delete_server_rule(
    server_id: int,
    rule_id: int,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete server-specific blocklist rule. Auto-syncs that server."""
    result = await db.execute(
        select(BlocklistRule).where(
            and_(
                BlocklistRule.id == rule_id,
                BlocklistRule.server_id == server_id
            )
        )
    )
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    await db.delete(rule)
    await db.commit()

    bg.add_task(manager_sync_single, server_id)

    return {"success": True, "message": "Rule deleted"}


@router.get("/server/{server_id}/status")
async def get_server_blocklist_status(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get ipset status from node"""
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()

    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            response = await client.get(
                f"{server.url}/api/ipset/status",
                headers={"X-API-Key": server.api_key}
            )
            if response.status_code == 200:
                return response.json()
            else:
                raise HTTPException(status_code=response.status_code, detail="Failed to get status from node")
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Node unreachable: {str(e)}")


# === Blocklist Sources ===

@router.get("/sources")
async def get_sources(
    direction: Optional[str] = Query(None, pattern="^(in|out)$"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get all blocklist sources, optionally filtered by direction"""
    query = select(BlocklistSource).order_by(BlocklistSource.is_default.desc(), BlocklistSource.name)
    if direction:
        query = query.where(BlocklistSource.direction == direction)

    result = await db.execute(query)
    sources = result.scalars().all()

    return {
        "count": len(sources),
        "sources": [
            {
                "id": s.id,
                "name": s.name,
                "url": s.url,
                "enabled": s.enabled,
                "is_default": s.is_default,
                "direction": s.direction or "in",
                "last_updated": s.last_updated.isoformat() if s.last_updated else None,
                "ip_count": s.ip_count,
                "error_message": s.error_message
            }
            for s in sources
        ]
    }


@router.post("/sources")
async def add_source(
    request: AddSourceRequest,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add new blocklist source. Auto-syncs."""
    result = await db.execute(
        select(BlocklistSource).where(BlocklistSource.url == request.url)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Source with this URL already exists")

    source = BlocklistSource(
        name=request.name,
        url=request.url,
        enabled=True,
        is_default=False,
        direction=request.direction,
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)

    bg.add_task(manager_sync_all)

    return {
        "success": True,
        "source": {
            "id": source.id,
            "name": source.name,
            "url": source.url,
            "enabled": source.enabled,
            "direction": source.direction
        }
    }


@router.put("/sources/{source_id}")
async def update_source(
    source_id: int,
    request: UpdateSourceRequest,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Update blocklist source. Auto-syncs on enable/disable toggle."""
    result = await db.execute(
        select(BlocklistSource).where(BlocklistSource.id == source_id)
    )
    source = result.scalar_one_or_none()

    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    toggled = request.enabled is not None and request.enabled != source.enabled

    if request.enabled is not None:
        source.enabled = request.enabled
    if request.name is not None:
        source.name = request.name

    await db.commit()

    if toggled:
        bg.add_task(manager_sync_all)

    return {
        "success": True,
        "source": {
            "id": source.id,
            "name": source.name,
            "enabled": source.enabled,
            "direction": source.direction or "in"
        }
    }


@router.delete("/sources/{source_id}")
async def delete_source(
    source_id: int,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete blocklist source. Auto-syncs."""
    result = await db.execute(
        select(BlocklistSource).where(BlocklistSource.id == source_id)
    )
    source = result.scalar_one_or_none()

    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    if source.is_default:
        raise HTTPException(status_code=400, detail="Cannot delete default source")

    was_enabled = source.enabled
    await db.delete(source)
    await db.commit()

    if was_enabled:
        bg.add_task(manager_sync_all)

    return {"success": True, "message": "Source deleted"}


@router.post("/sources/{source_id}/refresh")
async def refresh_source(
    source_id: int,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Refresh single source from GitHub. Auto-syncs if changed."""
    manager = get_blocklist_manager()
    success, message, ip_count, changed = await manager.refresh_source(source_id)

    if not success:
        raise HTTPException(status_code=400, detail=message)

    if changed:
        bg.add_task(manager_sync_all)

    return {
        "success": True,
        "message": message,
        "ip_count": ip_count,
        "changed": changed
    }


@router.post("/sources/refresh-all")
async def refresh_all_sources(
    bg: BackgroundTasks,
    _: dict = Depends(verify_auth)
):
    """Refresh all enabled sources. Auto-syncs if any changed."""
    manager = get_blocklist_manager()
    results, any_changed = await manager.refresh_all_sources()

    if any_changed:
        bg.add_task(manager_sync_all)

    return {
        "success": True,
        "results": results,
        "any_changed": any_changed
    }


# === Settings ===

@router.get("/settings")
async def get_blocklist_settings(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get blocklist settings including global torrent threshold"""
    manager = get_blocklist_manager()
    settings = await manager.get_blocklist_settings(db)

    threshold = await manager.get_setting("torrent_behavior_threshold", db)
    settings["torrent_behavior_threshold"] = int(threshold) if threshold else 50

    return {"settings": settings}


@router.put("/settings")
async def update_blocklist_settings(
    request: UpdateSettingsRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Update blocklist settings"""
    updates = {}

    if request.temp_timeout is not None:
        result = await db.execute(
            select(PanelSettings).where(PanelSettings.key == "blocklist_temp_timeout")
        )
        setting = result.scalar_one_or_none()
        if setting:
            setting.value = str(request.temp_timeout)
        else:
            db.add(PanelSettings(key="blocklist_temp_timeout", value=str(request.temp_timeout)))
        updates["temp_timeout"] = request.temp_timeout

    if request.auto_update_enabled is not None:
        result = await db.execute(
            select(PanelSettings).where(PanelSettings.key == "blocklist_auto_update_enabled")
        )
        setting = result.scalar_one_or_none()
        value = "true" if request.auto_update_enabled else "false"
        if setting:
            setting.value = value
        else:
            db.add(PanelSettings(key="blocklist_auto_update_enabled", value=value))
        updates["auto_update_enabled"] = request.auto_update_enabled

    if request.auto_update_interval is not None:
        result = await db.execute(
            select(PanelSettings).where(PanelSettings.key == "blocklist_auto_update_interval")
        )
        setting = result.scalar_one_or_none()
        if setting:
            setting.value = str(request.auto_update_interval)
        else:
            db.add(PanelSettings(key="blocklist_auto_update_interval", value=str(request.auto_update_interval)))
        updates["auto_update_interval"] = request.auto_update_interval

    await db.commit()

    return {
        "success": True,
        "updated": updates
    }


# === Sync ===

@router.post("/sync")
async def sync_all_nodes(
    _: dict = Depends(verify_auth)
):
    """Sync blocklists to all active nodes (parallel, both directions)"""
    manager = get_blocklist_manager()
    results = await manager.sync_all_nodes()
    return {
        "success": True,
        "results": results
    }


@router.post("/sync/{server_id}")
async def sync_single_node(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Sync blocklist to single node (both directions)"""
    result = await db.execute(select(Server).where(Server.id == server_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Server not found")

    manager = get_blocklist_manager()
    sync_result = await manager.sync_single_node_by_id(server_id)
    return sync_result or {"success": False, "message": "Server not found"}


@router.get("/sync/status")
async def get_sync_status(
    _: dict = Depends(verify_auth)
):
    """Get status of last sync operation (per-server results)."""
    manager = get_blocklist_manager()
    return manager.get_sync_status()


# === Torrent Blocker ===

async def _fetch_torrent_status(server: Server) -> dict:
    """Fetch torrent blocker status from a single node."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            response = await client.get(
                f"{server.url}/api/torrent-blocker/status",
                headers={"X-API-Key": server.api_key}
            )
            if response.status_code == 200:
                data = response.json()
                data["server_id"] = server.id
                data["server_name"] = server.name
                return data
            return {
                "server_id": server.id,
                "server_name": server.name,
                "enabled": False,
                "running": False,
                "error": f"HTTP {response.status_code}"
            }
    except Exception as e:
        return {
            "server_id": server.id,
            "server_name": server.name,
            "enabled": False,
            "running": False,
            "error": str(e)
        }


@router.get("/torrent-blocker")
async def get_torrent_blocker_status(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get torrent blocker status from active servers with xray node (parallel)."""
    result = await db.execute(
        select(Server).where(
            Server.is_active == True,
            Server.has_xray_node == True
        )
    )
    servers = result.scalars().all()

    if not servers:
        return {"servers": []}

    tasks = [_fetch_torrent_status(s) for s in servers]
    statuses = await asyncio.gather(*tasks)

    return {"servers": list(statuses)}


@router.post("/torrent-blocker/{server_id}/enable")
async def enable_torrent_blocker(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Enable torrent blocker on a specific server."""
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    try:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            response = await client.post(
                f"{server.url}/api/torrent-blocker/enable",
                headers={"X-API-Key": server.api_key}
            )
            if response.status_code == 200:
                return response.json()
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Node returned {response.status_code}"
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Node unreachable: {str(e)}")


@router.post("/torrent-blocker/{server_id}/disable")
async def disable_torrent_blocker(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Disable torrent blocker on a specific server."""
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    try:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            response = await client.post(
                f"{server.url}/api/torrent-blocker/disable",
                headers={"X-API-Key": server.api_key}
            )
            if response.status_code == 200:
                return response.json()
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Node returned {response.status_code}"
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Node unreachable: {str(e)}")


@router.post("/torrent-blocker/{server_id}/settings")
async def update_torrent_blocker_settings(
    server_id: int,
    request: TorrentBlockerSettingsRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Update torrent blocker settings on a specific server."""
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    try:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            response = await client.post(
                f"{server.url}/api/torrent-blocker/settings",
                headers={"X-API-Key": server.api_key},
                json={"behavior_threshold": request.behavior_threshold}
            )
            if response.status_code == 200:
                return response.json()
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Node returned {response.status_code}"
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Node unreachable: {str(e)}")


async def _push_threshold_to_server(server: Server, threshold: int) -> dict:
    """Push behavior_threshold to one node, return result dict."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            response = await client.post(
                f"{server.url}/api/torrent-blocker/settings",
                headers={"X-API-Key": server.api_key},
                json={"behavior_threshold": threshold}
            )
            if response.status_code == 200:
                return {"server_id": server.id, "server_name": server.name, "success": True}
            return {"server_id": server.id, "server_name": server.name, "success": False, "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"server_id": server.id, "server_name": server.name, "success": False, "error": str(e)}


@router.post("/torrent-blocker/global-settings")
async def update_global_torrent_settings(
    request: GlobalTorrentSettingsRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Set global behavior_threshold, save to PanelSettings, push to ALL active servers."""
    # Save to panel DB
    result = await db.execute(
        select(PanelSettings).where(PanelSettings.key == "torrent_behavior_threshold")
    )
    setting = result.scalar_one_or_none()
    if setting:
        setting.value = str(request.behavior_threshold)
    else:
        db.add(PanelSettings(key="torrent_behavior_threshold", value=str(request.behavior_threshold)))
    await db.commit()

    # Push to all active servers in parallel
    srv_result = await db.execute(select(Server).where(Server.is_active == True))
    servers = srv_result.scalars().all()

    results = []
    if servers:
        tasks = [_push_threshold_to_server(s, request.behavior_threshold) for s in servers]
        results = list(await asyncio.gather(*tasks))

    return {
        "success": True,
        "behavior_threshold": request.behavior_threshold,
        "servers": results
    }


# === Torrent Blocker Whitelist ===

DEFAULT_TORRENT_WHITELIST = [
    "127.0.0.1",
    "::1",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
]


@router.get("/torrent-blocker/whitelist")
async def get_torrent_whitelist(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get the torrent blocker IP whitelist."""
    import json as _json
    result = await db.execute(
        select(PanelSettings).where(PanelSettings.key == "torrent_whitelist")
    )
    setting = result.scalar_one_or_none()
    if setting:
        whitelist = _json.loads(setting.value)
    else:
        whitelist = list(DEFAULT_TORRENT_WHITELIST)
    return {"whitelist": whitelist}


async def _push_whitelist_to_server(server: Server, whitelist: list[str]) -> dict:
    """Push whitelist to one node, return result dict."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            response = await client.post(
                f"{server.url}/api/torrent-blocker/whitelist",
                headers={"X-API-Key": server.api_key},
                json={"whitelist": whitelist}
            )
            if response.status_code == 200:
                return {"server_id": server.id, "server_name": server.name, "success": True}
            return {"server_id": server.id, "server_name": server.name, "success": False, "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"server_id": server.id, "server_name": server.name, "success": False, "error": str(e)}


@router.put("/torrent-blocker/whitelist")
async def update_torrent_whitelist(
    request: TorrentWhitelistRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Save whitelist and push to all active xray servers in parallel."""
    import json as _json

    result = await db.execute(
        select(PanelSettings).where(PanelSettings.key == "torrent_whitelist")
    )
    setting = result.scalar_one_or_none()
    if setting:
        setting.value = _json.dumps(request.whitelist)
    else:
        db.add(PanelSettings(key="torrent_whitelist", value=_json.dumps(request.whitelist)))
    await db.commit()

    srv_result = await db.execute(
        select(Server).where(Server.is_active == True, Server.has_xray_node == True)
    )
    servers = srv_result.scalars().all()

    results = []
    if servers:
        tasks = [_push_whitelist_to_server(s, request.whitelist) for s in servers]
        results = list(await asyncio.gather(*tasks))

    return {
        "success": True,
        "whitelist": request.whitelist,
        "servers": results
    }
