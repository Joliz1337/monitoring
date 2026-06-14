"""Blocklist management router for IP/CIDR blocking (incoming + outgoing)

All rule mutations (add/delete global, server, source toggle) trigger
background sync so changes are applied to nodes automatically.
"""

import asyncio
import time
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from app.services.http_client import get_node_client, node_auth_headers
from pydantic import BaseModel, Field
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import BlocklistRule, BlocklistSource, Server, PanelSettings
from app.services.blocklist_manager import get_blocklist_manager

router = APIRouter(prefix="/blocklist", tags=["blocklist"])


# Per-(server_id, direction) кэш для GET /blocklist/server/{id}.
# При 100 серверах фронт открывает страницу и шлёт 200 параллельных запросов
# (in+out на каждый сервер) — кэш с per-key lock защищает БД от лавины и
# сериализует дублирующие запросы к одному ключу.
SERVER_RULES_CACHE_TTL_SEC = 15.0

_server_rules_cache: dict[tuple[int, str], tuple[float, dict]] = {}
_server_rules_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _get_server_rules_lock(key: tuple[int, str]) -> asyncio.Lock:
    lock = _server_rules_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _server_rules_locks[key] = lock
    return lock


def invalidate_server_rules_cache(server_id: int) -> None:
    """Сбросить кэш правил для одного сервера (обоих направлений)."""
    for direction in ("in", "out"):
        _server_rules_cache.pop((server_id, direction), None)


def invalidate_all_server_rules_cache() -> None:
    """Сбросить весь кэш правил серверов (после изменения глобальных правил/источников)."""
    _server_rules_cache.clear()


# Request/Response models
class AddRuleRequest(BaseModel):
    ip_cidr: str = Field(..., description="IP address or CIDR notation")
    is_permanent: bool = Field(True, description="Permanent (True) or temporary (False)")
    direction: str = Field("in", pattern="^(in|out)$", description="Traffic direction: in or out")
    list_type: str = Field("block", pattern="^(block|allow)$", description="block (DROP) or allow (whitelist)")
    comment: Optional[str] = Field(None, max_length=200)


class BulkAddRequest(BaseModel):
    ips: list[str] = Field(..., description="List of IP addresses or CIDR notations")
    is_permanent: bool = Field(True)
    direction: str = Field("in", pattern="^(in|out)$")
    list_type: str = Field("block", pattern="^(block|allow)$")


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


# === Global Rules ===

@router.get("/global")
async def get_global_rules(
    direction: str = Query("in", pattern="^(in|out)$"),
    list_type: str = Query("block", pattern="^(block|allow)$"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get all global blocklist rules filtered by direction and list_type"""
    result = await db.execute(
        select(BlocklistRule).where(
            and_(
                BlocklistRule.server_id.is_(None),
                BlocklistRule.direction == direction,
                BlocklistRule.list_type == list_type
            )
        ).order_by(BlocklistRule.created_at.desc())
    )
    rules = result.scalars().all()

    return {
        "count": len(rules),
        "direction": direction,
        "list_type": list_type,
        "rules": [
            {
                "id": r.id,
                "ip_cidr": r.ip_cidr,
                "is_permanent": r.is_permanent,
                "direction": r.direction or "in",
                "list_type": r.list_type or "block",
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
                BlocklistRule.direction == request.direction,
                BlocklistRule.list_type == request.list_type
            )
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Rule already exists")

    # allow (белый список) не имеет временного режима — всегда постоянное
    is_permanent = True if request.list_type == "allow" else request.is_permanent

    rule = BlocklistRule(
        ip_cidr=normalized,
        server_id=None,
        is_permanent=is_permanent,
        direction=request.direction,
        list_type=request.list_type,
        comment=request.comment,
        source="manual"
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)

    invalidate_all_server_rules_cache()
    bg.add_task(manager.sync_all_nodes)

    return {
        "success": True,
        "rule": {
            "id": rule.id,
            "ip_cidr": rule.ip_cidr,
            "is_permanent": rule.is_permanent,
            "direction": rule.direction,
            "list_type": rule.list_type,
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
                    BlocklistRule.direction == request.direction,
                    BlocklistRule.list_type == request.list_type
                )
            )
        )
        if result.scalar_one_or_none():
            skipped += 1
            continue

        rule = BlocklistRule(
            ip_cidr=normalized,
            server_id=None,
            is_permanent=True if request.list_type == "allow" else request.is_permanent,
            direction=request.direction,
            list_type=request.list_type,
            source="manual"
        )
        db.add(rule)
        added += 1

    await db.commit()

    if added > 0:
        invalidate_all_server_rules_cache()
        bg.add_task(manager.sync_all_nodes)

    return {
        "success": True,
        "added": added,
        "skipped": skipped,
        "invalid": invalid[:10],
        "direction": request.direction,
        "list_type": request.list_type
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

    invalidate_all_server_rules_cache()
    bg.add_task(manager_sync_all)

    return {"success": True, "message": "Rule deleted"}


async def manager_sync_all():
    manager = get_blocklist_manager()
    await manager.sync_all_nodes()


async def manager_sync_single(server_id: int):
    manager = get_blocklist_manager()
    await manager.sync_single_node_by_id(server_id)


# === Server-specific Rules ===

async def _build_server_rules_payload(
    db: AsyncSession, server_id: int, direction: str
) -> dict:
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


@router.get("/server/{server_id}")
async def get_server_rules(
    server_id: int,
    direction: str = Query("in", pattern="^(in|out)$"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get blocklist rules for specific server (cached per (server_id, direction), TTL=15s)."""
    key = (server_id, direction)
    now = time.monotonic()
    cached = _server_rules_cache.get(key)
    if cached and now < cached[0]:
        return cached[1]

    lock = _get_server_rules_lock(key)
    async with lock:
        cached = _server_rules_cache.get(key)
        now = time.monotonic()
        if cached and now < cached[0]:
            return cached[1]

        payload = await _build_server_rules_payload(db, server_id, direction)
        _server_rules_cache[key] = (
            time.monotonic() + SERVER_RULES_CACHE_TTL_SEC,
            payload,
        )
        return payload


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

    invalidate_server_rules_cache(server_id)
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

    invalidate_server_rules_cache(server_id)
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
        client = get_node_client(server)
        response = await client.get(
            f"{server.url}/api/ipset/status",
            headers=node_auth_headers(server),
            timeout=10.0,
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
    """Get blocklist settings."""
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
