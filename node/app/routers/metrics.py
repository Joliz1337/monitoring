"""Metrics API endpoints - Simple API returning current values only"""

from typing import Optional

from fastapi import APIRouter, Query

from app.models.metrics import AllMetrics
from app.services.metrics_collector import get_collector
from app.services.rate_sampler import MAX_WINDOW_SEC

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("", response_model=AllMetrics)
async def get_all_metrics(window: Optional[int] = Query(None, ge=1, le=MAX_WINDOW_SEC)):
    """Get all current system metrics; `window` adds averages and peaks over the last N seconds"""
    collector = get_collector()
    return await collector.get_all_metrics(window)
