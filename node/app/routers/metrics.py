"""Metrics API endpoints - Simple API returning current values only"""

from fastapi import APIRouter

from app.models.metrics import AllMetrics
from app.services.metrics_collector import get_collector

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("", response_model=AllMetrics)
async def get_all_metrics():
    """Get all current system metrics"""
    collector = get_collector()
    return await collector.get_all_metrics()
