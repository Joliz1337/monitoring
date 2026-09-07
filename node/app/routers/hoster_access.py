"""Разведка и вырезание средств доступа хостера внутрь ВМ."""

import logging

from fastapi import APIRouter, HTTPException

from app.models.hoster_access import (
    HosterPurgeRequest,
    HosterPurgeResponse,
    HosterScanResponse,
)
from app.services.hoster_access import get_hoster_access_manager

logger = logging.getLogger(__name__)

# Префикс /api/system/ даёт домен `system` в capabilities без правки карт.
router = APIRouter(prefix="/api/system/hoster-access", tags=["hoster-access"])


@router.get("/scan", response_model=HosterScanResponse)
async def scan() -> HosterScanResponse:
    """Read-only: что из средств доступа хостера найдено на хосте."""
    return await get_hoster_access_manager().scan()


@router.post("/purge", response_model=HosterPurgeResponse)
async def purge(request: HosterPurgeRequest) -> HosterPurgeResponse:
    """Вырезать выбранные находки. Необратимо — требует confirm."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm required")
    if not request.finding_ids:
        raise HTTPException(status_code=400, detail="nothing selected")
    return await get_hoster_access_manager().purge(request.finding_ids)
