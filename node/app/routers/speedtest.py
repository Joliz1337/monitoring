"""Speed test endpoints — runs iperf3 tests on demand from panel."""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.speedtest_runner import run_speedtest

router = APIRouter(prefix="/api/speedtest", tags=["speedtest"])
logger = logging.getLogger(__name__)

_test_lock = asyncio.Lock()
_running = False
_last_result: Optional[dict] = None


class SpeedtestRequest(BaseModel):
    servers: list[dict] = Field(..., min_length=1)
    duration: int = Field(default=3, ge=1, le=30)
    streams: int = Field(default=4, ge=1, le=16)
    threshold_mbps: float = Field(default=500.0, ge=0)


@router.post("")
async def run_test(request: SpeedtestRequest):
    global _running, _last_result

    if _running:
        raise HTTPException(status_code=409, detail="Test already in progress")

    async with _test_lock:
        _running = True
        try:
            result = await run_speedtest(
                servers=request.servers,
                duration=request.duration,
                streams=request.streams,
                threshold_mbps=request.threshold_mbps,
            )
            _last_result = result
            return result
        finally:
            _running = False


@router.get("/status")
async def get_status():
    return {
        "running": _running,
        "last_result": _last_result,
    }
