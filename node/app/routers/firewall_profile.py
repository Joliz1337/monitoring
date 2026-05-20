"""Firewall profile endpoints — атомарное применение шаблона UFW от панели."""

import asyncio
import logging

from fastapi import APIRouter

from app.models.firewall_profile import (
    ProfileApplyRequest,
    ProfileApplyResponse,
    ProfileStateResponse,
)
from app.services.firewall_manager import get_firewall_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/firewall", tags=["firewall-profile"])

# Два apply одновременно не должны накладываться: UFW не атомарен между командами.
_apply_lock = asyncio.Lock()


@router.post("/profile/apply", response_model=ProfileApplyResponse)
async def apply_profile(request: ProfileApplyRequest) -> ProfileApplyResponse:
    """Атомарно заменить UFW-конфигурацию правилами профиля."""
    manager = get_firewall_manager()
    rules_payload = [r.model_dump() for r in request.rules]

    async with _apply_lock:
        result = await asyncio.to_thread(
            manager.apply_profile,
            rules_payload,
            request.default_incoming,
            request.default_outgoing,
            request.force,
        )

    return ProfileApplyResponse(**result)


@router.get("/profile/state", response_model=ProfileStateResponse)
async def get_profile_state() -> ProfileStateResponse:
    """Текущее состояние firewall на ноде (для drift-детекции)."""
    manager = get_firewall_manager()
    state = await asyncio.to_thread(manager.get_full_state)
    return ProfileStateResponse(**state)
