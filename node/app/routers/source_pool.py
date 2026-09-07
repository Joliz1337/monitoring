"""Пул исходящих адресов: раскладка меток по IP ноды и конфиг от панели."""

from fastapi import APIRouter, HTTPException

from app.models.source_pool import SourcePoolConfig, SourcePoolState
from app.services.source_pool import EXIT_PROXY_CONFLICT, exit_proxy_enabled, get_source_pool_manager

# Префикс /api/system/ даёт домен `system` в capabilities без правки карт
router = APIRouter(prefix="/api/system/source-pool", tags=["source-pool"])


@router.get("/state", response_model=SourcePoolState)
async def get_state() -> SourcePoolState:
    """Адреса интерфейса, раскладка меток и сходится ли она с тем, что стоит в ядре."""
    return await get_source_pool_manager().state()


@router.put("/config", response_model=SourcePoolState)
async def put_config(config: SourcePoolConfig) -> SourcePoolState:
    """Конфиг от панели. Включение при работающем exit-прокси отклоняется: оба
    управляют исходящим адресом, и вместе они дали бы неопределённый результат."""
    if config.enabled and exit_proxy_enabled():
        raise HTTPException(status_code=409, detail=EXIT_PROXY_CONFLICT)
    return await get_source_pool_manager().apply_config(config)
