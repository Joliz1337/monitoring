"""Настройка резервируемых портов: общий список парка и списки отдельных нод.

Логика доставки — в services/reserved_ports_sync.py; здесь только приём
значений от UI, валидация и запуск рассылки.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import Server
from app.routers.settings import RESERVED_PORTS_KEY, get_setting, set_setting
from app.services.reserved_ports_sync import (
    MIN_NODE_VERSION_RESERVED_PORTS,
    apply_reserved_ports,
    node_supports_reserved_ports,
    parse_ports_value,
)

router = APIRouter(prefix="/reserved-ports", tags=["reserved-ports"])


class PortsUpdate(BaseModel):
    ports: str = ""


@router.get("")
async def get_reserved_ports_config(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    global_value = await get_setting(RESERVED_PORTS_KEY, db) or ""
    result = await db.execute(
        select(Server).where(Server.is_active == True).order_by(Server.position, Server.id)  # noqa: E712
    )
    servers = [
        {
            "id": s.id,
            "name": s.name,
            "ports": s.reserved_ports or "",
            "node_version": s.node_version,
            "supported": node_supports_reserved_ports(s.node_version),
        }
        for s in result.scalars().all()
    ]
    return {
        "global_ports": global_value,
        "min_node_version": MIN_NODE_VERSION_RESERVED_PORTS,
        "servers": servers,
    }


@router.put("/global")
async def set_global_reserved_ports(
    data: PortsUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    try:
        entries = parse_ports_value(data.ports)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    value = ",".join(entries)
    await set_setting(RESERVED_PORTS_KEY, value, db)
    # Рассылка в фоне: парк может быть большим, ответ UI её не ждёт. Офлайн-ноды
    # и сбои закрывает очередь отложенной синхронизации.
    asyncio.ensure_future(apply_reserved_ports(reason="global reserved ports changed"))
    return {"success": True, "ports": value}


@router.put("/servers/{server_id}")
async def set_server_reserved_ports(
    server_id: int,
    data: PortsUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    try:
        entries = parse_ports_value(data.ports)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404)

    value = ",".join(entries)
    server.reserved_ports = value or None
    await db.commit()

    # Одна нода — ждём результата: оператору сразу видно, доехало или встало
    # в очередь (офлайн-нода отсекается быстро, без сетевого таймаута).
    push = await apply_reserved_ports([server_id], reason=f"reserved ports for {server.name}")
    return {
        "success": True,
        "ports": value,
        "queued": push["queued"] > 0,
        "error": next(iter(push["errors"].values()), None),
    }
