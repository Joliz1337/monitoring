"""DNAT-маршрутизация: применение набора правил от панели и живое состояние."""

import logging

from fastapi import APIRouter

from app.models.dnat import (
    DnatActionResponse,
    DnatApplyRequest,
    DnatApplyResponse,
    DnatStateResponse,
)
from app.services.dnat_manager import get_dnat_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dnat", tags=["dnat"])


@router.get("/state", response_model=DnatStateResponse)
async def get_state() -> DnatStateResponse:
    """Желаемые правила, их наличие в ядре и счётчики соединений/байт."""
    return DnatStateResponse(**await get_dnat_manager().state_async())


@router.post("/apply", response_model=DnatApplyResponse)
async def apply_rules(request: DnatApplyRequest) -> DnatApplyResponse:
    """Атомарно заменить набор DNAT-правил ноды."""
    result = await get_dnat_manager().apply_async(request.rules)
    if result["success"]:
        logger.info("DNAT profile applied from panel: %s rules", len(request.rules))
    return DnatApplyResponse(**result)


@router.post("/reapply", response_model=DnatActionResponse)
async def reapply_rules() -> DnatActionResponse:
    """Вернуть в ядро сохранённые правила, если они потерялись (ручное самолечение)."""
    action = await get_dnat_manager().ensure_async()
    return DnatActionResponse(success=True, message=action or "Rules are in place")


@router.post("/clear", response_model=DnatActionResponse)
async def clear_rules() -> DnatActionResponse:
    """Снять все DNAT-правила и забыть их."""
    result = await get_dnat_manager().clear_async()
    return DnatActionResponse(**result)
