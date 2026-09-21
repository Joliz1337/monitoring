"""Дополнительные IP-адреса интерфейса: состояние, транзакция, подтверждение, откат."""

import logging

from fastapi import APIRouter, HTTPException

from app.models.network import (
    NetworkActionResponse,
    NetworkApplyRequest,
    NetworkApplyResponse,
    NetworkStateResponse,
    TransactionRequest,
)
from app.services.extra_ips import (
    ExtraIpBusyError,
    ExtraIpUnsupportedError,
    ExtraIpValidationError,
    get_extra_ip_manager,
)

logger = logging.getLogger(__name__)

# Префикс /api/system/ даёт домен `system` в capabilities без правки карт
router = APIRouter(prefix="/api/system/network", tags=["network"])


@router.get("/state", response_model=NetworkStateResponse)
async def get_state() -> NetworkStateResponse:
    """Интерфейсы с адресами, бэкенд сетевого конфига, текущая транзакция и история."""
    return await get_extra_ip_manager().state()


@router.post("/apply", response_model=NetworkApplyResponse)
async def apply_addresses(request: NetworkApplyRequest) -> NetworkApplyResponse:
    """Добавить/убрать адреса транзакцией с таймером отката; провал применения — 200 с success=false."""
    try:
        return await get_extra_ip_manager().apply(request)
    except ExtraIpBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (ExtraIpValidationError, ExtraIpUnsupportedError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/confirm", response_model=NetworkActionResponse)
async def confirm_transaction(request: TransactionRequest) -> NetworkActionResponse:
    """Панель снова достучалась до ноды — снять таймер отката. Идемпотентно."""
    try:
        return await get_extra_ip_manager().confirm(request.transaction_id)
    except ExtraIpValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/rollback", response_model=NetworkActionResponse)
async def rollback_transaction(request: TransactionRequest) -> NetworkActionResponse:
    """Ручной откат транзакции, ждущей подтверждения."""
    try:
        return await get_extra_ip_manager().rollback(request.transaction_id)
    except ExtraIpValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
