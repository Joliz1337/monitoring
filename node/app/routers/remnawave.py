"""Remnawave Xray log statistics API endpoints."""

from fastapi import APIRouter

from app.services.xray_log_collector import get_xray_log_collector

router = APIRouter(prefix="/api/remnawave", tags=["remnawave"])


@router.get("/status")
async def get_status():
    """Get Xray log collector status."""
    collector = get_xray_log_collector()
    return collector.get_status()


@router.post("/stats/collect")
async def collect_stats():
    """
    Collect accumulated Xray statistics and clear memory.
    
    Called by the panel periodically to fetch stats.
    After this call, the node's memory is cleared.
    
    Returns:
        collected_at: ISO timestamp of collection
        period_start: When stats accumulation started
        entries_count: Total log entries processed
        stats: List of {destination, email, count} aggregated visits
    """
    collector = get_xray_log_collector()
    return collector.collect_and_clear()
