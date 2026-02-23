"""Remnawave Xray log statistics API endpoints.

Collector starts lazily on first /stats/collect call from panel.
"""

from fastapi import APIRouter

from app.services.xray_log_collector import get_xray_log_collector

router = APIRouter(prefix="/api/remnawave", tags=["remnawave"])


@router.get("/status")
async def get_status():
    """Get Xray log collector status.
    
    Always does a live container check so the panel gets accurate
    xray availability even when the collector hasn't been started yet.
    """
    collector = get_xray_log_collector()
    status = collector.get_status()

    # Live check: don't rely on the lazy-started collector's _available flag
    if not status["available"] and not collector._running:
        container_up = await collector._check_container_available()
        status["available"] = container_up

    return status


@router.post("/stats/collect")
async def collect_stats():
    """
    Collect accumulated Xray statistics and clear memory.
    
    Called by the panel periodically to fetch stats.
    Starts collector on first call if not running.
    Processes remaining buffer before returning stats.
    After this call, the node's memory is cleared.
    
    Returns:
        collected_at: ISO timestamp of collection
        period_start: When stats accumulation started
        entries_count: Total log entries processed
        stats: List of {destination, email, count} aggregated visits
    """
    collector = get_xray_log_collector()
    
    # Lazy start: collector starts on first request from panel
    if not collector._running:
        await collector.start()
    
    return await collector.collect_and_clear()
