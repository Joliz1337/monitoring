"""Torrent blocker API endpoints.

Enable/disable torrent blocking, get status, configure behavior threshold.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.torrent_blocker import get_torrent_blocker

router = APIRouter(prefix="/api/torrent-blocker", tags=["torrent-blocker"])


class UpdateSettingsRequest(BaseModel):
    behavior_threshold: int = Field(..., ge=5, le=1000)


class UpdateWhitelistRequest(BaseModel):
    whitelist: list[str] = Field(..., max_length=500)


@router.get("/status")
async def get_status():
    """Get torrent blocker status, active blocks from ipset, and settings."""
    blocker = get_torrent_blocker()
    return blocker.get_status()


@router.post("/enable")
async def enable():
    """Enable torrent blocker — starts log monitoring and IP blocking."""
    blocker = get_torrent_blocker()
    if blocker._running:
        return {"success": True, "message": "Already running"}
    await blocker.start()
    return {"success": True, "message": "Torrent blocker enabled"}


@router.post("/disable")
async def disable():
    """Disable torrent blocker — stops log monitoring and saves disabled state."""
    blocker = get_torrent_blocker()
    if not blocker._running and not blocker._enabled:
        return {"success": True, "message": "Already stopped"}
    await blocker.disable()
    return {"success": True, "message": "Torrent blocker disabled"}


@router.post("/settings")
async def update_settings(req: UpdateSettingsRequest):
    """Update torrent blocker settings (behavior threshold)."""
    blocker = get_torrent_blocker()
    blocker.set_behavior_threshold(req.behavior_threshold)
    return {
        "success": True,
        "behavior_threshold": blocker.behavior_threshold,
    }


@router.post("/whitelist")
async def update_whitelist(req: UpdateWhitelistRequest):
    """Replace the full whitelist (IPs/CIDRs excluded from blocking)."""
    blocker = get_torrent_blocker()
    blocker.set_whitelist(req.whitelist)
    return {
        "success": True,
        "whitelist": blocker.whitelist,
    }
