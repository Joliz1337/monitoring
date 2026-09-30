"""Ручная проверка потерь до адреса с этой ноды (страница «Потери» в панели)."""

from fastapi import APIRouter

from app.models.loss_probe import LossCheckRequest, ProbeStats
from app.services.loss_probe import check_target

router = APIRouter(prefix="/api/loss-probe", tags=["loss-probe"])


@router.post("/check", response_model=ProbeStats)
async def check(request: LossCheckRequest) -> ProbeStats:
    """Серия попыток подключения к ip:port прямо сейчас."""
    return await check_target(request.ip, request.port, request.attempts)
