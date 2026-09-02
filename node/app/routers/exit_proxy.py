"""Exit-прокси: статус, конфиг от панели, прогон проверок и ручное переключение выхода."""

from fastapi import APIRouter, HTTPException, Query

from app.services.exit_proxy.manager import ExitProxyValidationError, get_exit_proxy_manager
from app.services.exit_proxy.models import (
    CheckStartResponse,
    ExitEvent,
    ExitProxyConfig,
    ExitProxyStatus,
    SwitchRequest,
)

# Префикс /api/system/ даёт домен `system` в capabilities без правки карт
router = APIRouter(prefix="/api/system/exit-proxy", tags=["exit-proxy"])


@router.get("/status", response_model=ExitProxyStatus)
async def get_status() -> ExitProxyStatus:
    """Текущий выход, кандидаты с результатами проверок, self-test и хвост событий."""
    return get_exit_proxy_manager().status()


@router.put("/config", response_model=ExitProxyStatus)
async def put_config(config: ExitProxyConfig) -> ExitProxyStatus:
    """Полный конфиг от панели: поднимает/гасит socks, при смене проверок запускает прогон."""
    return await get_exit_proxy_manager().apply_config(config)


@router.post("/check", response_model=CheckStartResponse, status_code=202)
async def start_check() -> CheckStartResponse:
    """Прогон проверок в фоне; результат забирается через /status."""
    if not get_exit_proxy_manager().start_check():
        raise HTTPException(status_code=409, detail="check already in progress")
    return CheckStartResponse(started=True)


@router.post("/switch", response_model=ExitProxyStatus)
async def switch_exit(request: SwitchRequest) -> ExitProxyStatus:
    """Ручное переключение: в режиме manual закрепляет кандидата, в auto держится, пока он здоров."""
    try:
        return await get_exit_proxy_manager().switch(request.candidate)
    except ExitProxyValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/events", response_model=list[ExitEvent])
async def get_events(limit: int = Query(50, ge=1, le=200)) -> list[ExitEvent]:
    return get_exit_proxy_manager().events(limit)
