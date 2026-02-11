"""Torrent blocker API endpoints.

Enable/disable torrent blocking and get status.
"""

from fastapi import APIRouter

from app.services.torrent_blocker import get_torrent_blocker

router = APIRouter(prefix="/api/torrent-blocker", tags=["torrent-blocker"])


@router.get("/status")
async def get_status():
    """Get torrent blocker status and statistics."""
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
    """Disable torrent blocker — stops log monitoring."""
    blocker = get_torrent_blocker()
    if not blocker._running:
        return {"success": True, "message": "Already stopped"}
    await blocker.stop()
    return {"success": True, "message": "Torrent blocker disabled"}
