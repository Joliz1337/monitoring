"""Anti-DDoS control router (panel side).

Settings + aggregated node status + global actions. Per-node emergency/watchdog
toggles also flow through the /proxy/{id}/antiddos/... routes; here we expose the
global fan-out ("enable on all nodes"), the manual whitelist push, and the
watchdog installer.
"""

import asyncio
import ipaddress
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import AntiDdosSettings, AntiDdosWhitelistSource, Server
from app.services.antiddos_manager import get_antiddos_manager

router = APIRouter(prefix="/antiddos", tags=["antiddos"])


class UpdateSettings(BaseModel):
    enabled: Optional[bool] = None
    whitelist_push_interval: Optional[int] = Field(None, ge=300, le=86400)
    status_poll_interval: Optional[int] = Field(None, ge=15, le=3600)
    watchdog_default_enabled: Optional[bool] = None
    user_cidrs: Optional[list[str]] = None

    @field_validator("user_cidrs")
    @classmethod
    def _validate_cidrs(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        cleaned = []
        for item in value:
            item = str(item).strip()
            if not item:
                continue
            try:
                if "/" in item:
                    ipaddress.ip_network(item, strict=False)
                else:
                    ipaddress.ip_address(item)
            except ValueError:
                raise ValueError(f"invalid IP/CIDR: {item}")
            cleaned.append(item)
        return sorted(set(cleaned))


class EmergencyAllRequest(BaseModel):
    enabled: bool


def _settings_to_dict(s: AntiDdosSettings) -> dict:
    cidrs = []
    if s.user_cidrs:
        try:
            cidrs = json.loads(s.user_cidrs)
        except (json.JSONDecodeError, TypeError):
            cidrs = []
    return {
        "enabled": s.enabled,
        "whitelist_push_interval": s.whitelist_push_interval,
        "status_poll_interval": s.status_poll_interval,
        "watchdog_default_enabled": s.watchdog_default_enabled,
        "user_cidrs": cidrs,
        "last_push_at": s.last_push_at.isoformat() if s.last_push_at else None,
        "last_push_status": s.last_push_status,
        "last_push_count": s.last_push_count,
    }


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    settings = await get_antiddos_manager().get_or_create_settings(db)
    return _settings_to_dict(settings)


@router.put("/settings")
async def update_settings(
    payload: UpdateSettings,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    manager = get_antiddos_manager()
    settings = await manager.get_or_create_settings(db)
    was_enabled = settings.enabled
    data = payload.model_dump(exclude_unset=True)
    if "user_cidrs" in data:
        settings.user_cidrs = json.dumps(data.pop("user_cidrs"))
    for key, value in data.items():
        setattr(settings, key, value)
    await db.commit()
    await db.refresh(settings)

    # Master switch flip → stand the whole fleet up or down in the background
    if settings.enabled != was_enabled:
        asyncio.create_task(manager.apply_master_state(settings.enabled))

    return _settings_to_dict(settings)


@router.get("/status")
async def get_status(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    """Aggregated per-node emergency state from the DB (updated by the poll loop)."""
    servers = (await db.execute(select(Server).where(Server.is_active == True))).scalars().all()  # noqa: E712
    nodes = []
    active = 0
    for srv in servers:
        if srv.antiddos_emergency_mode:
            active += 1
        nodes.append({
            "server_id": srv.id,
            "server_name": srv.name,
            "emergency_mode": bool(srv.antiddos_emergency_mode),
            "source": srv.antiddos_source or "none",
            "reason": srv.antiddos_reason or "",
            "watchdog": bool(srv.antiddos_watchdog),
            "since": srv.antiddos_since.isoformat() if srv.antiddos_since else None,
            "last_sync_at": srv.antiddos_last_sync_at.isoformat() if srv.antiddos_last_sync_at else None,
        })
    return {"nodes": nodes, "active_count": active, "total": len(nodes)}


# Fleet-wide fan-outs can take tens of seconds (dozens of nodes, per-node
# timeouts, some unreachable). Running them inside the request meant nginx/the
# browser cut the connection (499) mid-rollout, leaving the fleet half-applied.
# Fire them in the background and return immediately; the status poll reflects
# progress within a cycle.
@router.post("/emergency-all")
async def emergency_all(payload: EmergencyAllRequest, _: dict = Depends(verify_auth)):
    mgr = get_antiddos_manager()
    asyncio.create_task(mgr.run_bg(mgr.set_emergency_all(payload.enabled)))
    return {"success": True, "started": True}


@router.post("/whitelist/push")
async def whitelist_push_now(_: dict = Depends(verify_auth)):
    mgr = get_antiddos_manager()
    asyncio.create_task(mgr.run_bg(mgr.push_whitelist_all()))
    return {"success": True, "started": True}


@router.post("/install-all")
async def install_all(_: dict = Depends(verify_auth)):
    mgr = get_antiddos_manager()
    asyncio.create_task(mgr.run_bg(mgr.install_all()))
    return {"success": True, "started": True}


# ── whitelist auto-source lists (Cloudflare, Yandex Cloud, …) ───────────────

class AddSourceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    url: str = Field(..., min_length=1, max_length=500)

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        value = value.strip()
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return value


class UpdateSourceRequest(BaseModel):
    enabled: Optional[bool] = None
    name: Optional[str] = Field(None, min_length=1, max_length=100)


def _source_to_dict(s: AntiDdosWhitelistSource) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "url": s.url,
        "enabled": s.enabled,
        "ip_count": s.ip_count,
        "last_updated": s.last_updated.isoformat() if s.last_updated else None,
        "error_message": s.error_message,
    }


def _push_bg():
    mgr = get_antiddos_manager()
    asyncio.create_task(mgr.run_bg(mgr.push_whitelist_all()))


@router.get("/sources")
async def get_sources(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    rows = (await db.execute(
        select(AntiDdosWhitelistSource).order_by(AntiDdosWhitelistSource.name)
    )).scalars().all()
    return {"sources": [_source_to_dict(s) for s in rows]}


@router.post("/sources")
async def add_source(payload: AddSourceRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    exists = (await db.execute(
        select(AntiDdosWhitelistSource).where(AntiDdosWhitelistSource.url == payload.url)
    )).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail="Source with this URL already exists")
    source = AntiDdosWhitelistSource(name=payload.name, url=payload.url, enabled=True)
    db.add(source)
    await db.commit()
    await db.refresh(source)
    _push_bg()
    return {"success": True, "source": _source_to_dict(source)}


@router.put("/sources/{source_id}")
async def update_source(source_id: int, payload: UpdateSourceRequest,
                        db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    source = (await db.execute(
        select(AntiDdosWhitelistSource).where(AntiDdosWhitelistSource.id == source_id)
    )).scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    toggled = payload.enabled is not None and payload.enabled != source.enabled
    if payload.enabled is not None:
        source.enabled = payload.enabled
    if payload.name is not None:
        source.name = payload.name
    await db.commit()
    await db.refresh(source)
    if toggled:
        _push_bg()
    return {"success": True, "source": _source_to_dict(source)}


@router.delete("/sources/{source_id}")
async def delete_source(source_id: int, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    source = (await db.execute(
        select(AntiDdosWhitelistSource).where(AntiDdosWhitelistSource.id == source_id)
    )).scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    await db.delete(source)
    await db.commit()
    _push_bg()
    return {"success": True}


@router.post("/sources/refresh")
async def refresh_sources(_: dict = Depends(verify_auth)):
    mgr = get_antiddos_manager()
    asyncio.create_task(mgr.run_bg(mgr.refresh_sources_and_push()))
    return {"success": True, "started": True}
