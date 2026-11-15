"""Metrics API endpoints - Simple API returning current values only"""

from typing import Optional

from fastapi import APIRouter, Query

from app.models.metrics import AllMetrics
from app.services.metrics_collector import get_collector
from app.services.rate_sampler import MAX_WINDOW_SEC

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("", response_model=AllMetrics)
async def get_all_metrics(
    window: Optional[int] = Query(None, ge=1, le=MAX_WINDOW_SEC),
    client_probe: bool = Query(False),
):
    """Get all current system metrics; `window` adds averages and peaks over the last N seconds.
    `client_probe` — панель просит проверять адреса назначения как клиент (см. services/loss_probe)."""
    collector = get_collector()
    if client_probe:
        collector.request_client_probe()
    return await collector.get_all_metrics(window)
