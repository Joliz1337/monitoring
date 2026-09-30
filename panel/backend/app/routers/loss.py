"""Страница «Потери»: сводка по адресам назначения релеев, ручная проверка адреса
и правка бэкенда с этим адресом во всех профилях HAProxy и DNAT."""

import ipaddress
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import Server
from app.services.backend_address import (
    Action,
    AddressEdit,
    AddressEditError,
    plan_edit,
    suggest_addresses,
    sync_changed_profiles,
    validate_edit,
)
from app.services.loss_overview import TargetParseError, build_overview, check_from_servers, parse_target
from app.services.loss_registry import get_loss_registry
from app.services.server_alerter import get_server_alerter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/loss", tags=["loss"], dependencies=[Depends(verify_auth)])

MAX_CHECK_SERVERS = 500


class LossCheckRequest(BaseModel):
    target: str = Field(..., min_length=1, max_length=64)
    server_ids: list[int] = Field(..., min_length=1, max_length=MAX_CHECK_SERVERS)


class BackendEditRequest(BaseModel):
    ip: str
    port: int = Field(..., ge=1, le=65535)
    all_ports: bool = False
    action: Action
    new_ip: Optional[str] = None
    new_port: Optional[int] = Field(None, ge=1, le=65535)

    @field_validator("ip", "new_ip")
    @classmethod
    def _unicast_ipv4(cls, value: Optional[str]) -> Optional[str]:
        # DNAT-профили держат только IPv4 — правка работает в тех же рамках
        if value is None:
            return None
        address = ipaddress.IPv4Address(value.strip())
        if address.is_unspecified or address.is_multicast:
            raise ValueError("must be a unicast IPv4 address")
        return str(address)

    def to_edit(self) -> AddressEdit:
        return AddressEdit(
            ip=self.ip, port=self.port, all_ports=self.all_ports, action=self.action,
            new_ip=self.new_ip, new_port=self.new_port,
        )


@router.get("/overview")
async def get_overview():
    registry = get_loss_registry()
    return {
        "targets": build_overview(registry.fresh(), registry.owners(), get_server_alerter().loss_episodes()),
    }


@router.post("/check")
async def check_target(data: LossCheckRequest, db: AsyncSession = Depends(get_db)):
    try:
        ip, port = parse_target(data.target)
    except TargetParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result = await db.execute(
        select(Server).where(Server.id.in_(data.server_ids), Server.is_active == True)  # noqa: E712
    )
    servers = list(result.scalars().all())
    # Сессию отпускаем до похода по нодам: медленная нода не держит коннект пула
    await db.commit()
    return {"ip": ip, "port": port, "results": await check_from_servers(servers, ip, port)}


@router.post("/backends/preview")
async def preview_backend_edit(data: BackendEditRequest, db: AsyncSession = Depends(get_db)):
    """Где стоит адрес и что с каждым правилом сделает правка; ничего не сохраняет."""
    edit = data.to_edit()
    changes = await plan_edit(db, edit)
    return {
        "profiles": [change.to_dict() for change in changes],
        "suggestions": suggest_addresses(get_loss_registry().fresh(), edit.ip),
    }


@router.post("/backends/apply")
async def apply_backend_edit(data: BackendEditRequest, bg: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    edit = data.to_edit()
    try:
        validate_edit(edit)
    except AddressEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    changes = await plan_edit(db, edit, save=True)
    await db.commit()
    changed = [change for change in changes if change.changed]
    logger.info(
        "backend_address_edited action=%s ip=%s port=%s all_ports=%s new_ip=%s new_port=%s profiles=%s",
        edit.action.value, edit.ip, edit.port, edit.all_ports, edit.new_ip, edit.new_port,
        [f"{c.kind}:{c.profile_id}" for c in changed],
    )
    bg.add_task(sync_changed_profiles, changed)
    return {"profiles": [change.to_dict() for change in changes]}
