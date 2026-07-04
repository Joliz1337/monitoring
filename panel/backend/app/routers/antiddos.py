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

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import AntiDdosSettings, Server
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


@router.post("/emergency-all")
async def emergency_all(payload: EmergencyAllRequest, _: dict = Depends(verify_auth)):
    result = await get_antiddos_manager().set_emergency_all(payload.enabled)
    return {"success": True, **result}


@router.post("/whitelist/push")
async def whitelist_push_now(_: dict = Depends(verify_auth)):
    result = await get_antiddos_manager().push_whitelist_all()
    return {"success": True, **result}


@router.post("/install-all")
async def install_all(_: dict = Depends(verify_auth)):
    result = await get_antiddos_manager().install_all()
    return {"success": True, **result}
