"""Blocklist management router for IP/CIDR blocking (incoming + outgoing)"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
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


class RuleResponse(BaseModel):
    id: int
    ip_cidr: str
    server_id: Optional[int]
    is_permanent: bool
    direction: str
    comment: Optional[str]
    source: str
    created_at: datetime


class SourceResponse(BaseModel):
    id: int
    name: str
    url: str
    enabled: bool
    is_default: bool
    direction: str
    last_updated: Optional[datetime]
    ip_count: int
    error_message: Optional[str]


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
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add global blocklist rule (applies to all servers)"""
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
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add multiple global blocklist rules"""
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
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete global blocklist rule"""
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
    
    return {"success": True, "message": "Rule deleted"}


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
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add blocklist rule for specific server"""
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
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete server-specific blocklist rule"""
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
    
    return {"success": True, "message": "Rule deleted"}


@router.get("/server/{server_id}/status")
async def get_server_blocklist_status(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get ipset status from node"""
    import httpx
    
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
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Add new blocklist source"""
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
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Update blocklist source"""
    result = await db.execute(
        select(BlocklistSource).where(BlocklistSource.id == source_id)
    )
    source = result.scalar_one_or_none()
    
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    
    if request.enabled is not None:
        source.enabled = request.enabled
    if request.name is not None:
        source.name = request.name
    
    await db.commit()
    
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
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Delete blocklist source"""
    result = await db.execute(
        select(BlocklistSource).where(BlocklistSource.id == source_id)
    )
    source = result.scalar_one_or_none()
    
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    
    if source.is_default:
        raise HTTPException(status_code=400, detail="Cannot delete default source")
    
    await db.delete(source)
    await db.commit()
    
    return {"success": True, "message": "Source deleted"}


@router.post("/sources/{source_id}/refresh")
async def refresh_source(
    source_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Refresh single source from GitHub"""
    manager = get_blocklist_manager()
    success, message, ip_count, changed = await manager.refresh_source(source_id)
    
    if not success:
        raise HTTPException(status_code=400, detail=message)
    
    return {
        "success": True,
        "message": message,
        "ip_count": ip_count,
        "changed": changed
    }


@router.post("/sources/refresh-all")
async def refresh_all_sources(
    _: dict = Depends(verify_auth)
):
    """Refresh all enabled sources"""
    manager = get_blocklist_manager()
    results, any_changed = await manager.refresh_all_sources()
    
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
    """Get blocklist settings"""
    manager = get_blocklist_manager()
    settings = await manager.get_blocklist_settings(db)
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
    """Sync blocklists to all active nodes (both directions)"""
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
    server = result.scalar_one_or_none()
    
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    manager = get_blocklist_manager()
    
    sync_result = {"server_id": server_id, "success": True}
    
    for direction in ("in", "out"):
        ips = await manager.get_combined_ips_for_server(server_id, db, direction)
        success, message, data = await manager.sync_to_node(server, ips, direction=direction)
        
        sync_result[direction] = {
            "success": success,
            "message": message,
            "ip_count": len(ips),
            "added": data.get("added", 0),
            "removed": data.get("removed", 0),
        }
        if not success:
            sync_result["success"] = False
    
    return sync_result
