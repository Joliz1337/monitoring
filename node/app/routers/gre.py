"""GRE-туннели DDoS-Guard: состояние, полный список туннелей от панели, проверка с CE."""

from fastapi import APIRouter, HTTPException

from app.models.gre import GreCheckRequest, GreCheckResult, GreConfig, GreState
from app.services.gre_tunnels import GreError, get_gre_manager

# Префикс /api/system/ даёт домен `system` в capabilities без правки карт
router = APIRouter(prefix="/api/system/gre", tags=["gre"])


@router.get("/state", response_model=GreState)
async def get_state() -> GreState:
    return await get_gre_manager().state()


@router.put("/config", response_model=GreState)
async def put_config(config: GreConfig) -> GreState:
    """Заменить список туннелей целиком; пустой список снимает всё и выключает службу."""
    try:
        return await get_gre_manager().apply(config)
    except GreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/check", response_model=GreCheckResult)
async def check_tunnel(request: GreCheckRequest) -> GreCheckResult:
    """Пинг PE с адреса CE и запрос наружу с CE."""
    try:
        return await get_gre_manager().check(request.slot)
    except GreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
