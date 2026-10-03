"""Ручная проверка потерь и трасса до адреса с этой ноды (страница «Потери» в панели)."""

from fastapi import APIRouter, HTTPException

from app.models.loss_probe import LossCheckRequest, ProbeStats, TargetRequest
from app.services.loss_probe import check_target
from app.services.path_trace import TraceBusyError, get_path_trace_manager

router = APIRouter(prefix="/api/loss-probe", tags=["loss-probe"])


@router.post("/check", response_model=ProbeStats)
async def check(request: LossCheckRequest) -> ProbeStats:
    """Серия попыток подключения к ip:port прямо сейчас."""
    return await check_target(request.ip, request.port, request.attempts)


@router.post("/trace")
async def start_trace(request: TargetRequest) -> dict:
    """Запустить mtr до ip:port в фоне; ход — GET /trace/{id}."""
    try:
        trace = get_path_trace_manager().start(request.ip, request.port)
    except TraceBusyError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    return {"id": trace.id}


@router.get("/trace/{trace_id}")
async def get_trace(trace_id: str) -> dict:
    manager = get_path_trace_manager()
    trace = manager.get(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return manager.view(trace)
