"""Пул исходящих адресов: включение на ноде, исключения, состояние раскладки, сниппет Xray.

Роутер тонкий: раскладку меток по адресам делает нода, доставка и состояние — в
services/source_pool. Здесь только приём значений от UI и взаимоисключение с
exit-прокси: оба управляют тем, с какого IP нода выходит наружу.
"""

import ipaddress
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import ExitProxyNode, Server, SourcePoolNode
from app.routers.proxy import get_server_by_id, require_capability
from app.services.haproxy_profile_sync import is_server_online
from app.services.node_capabilities import Capability
from app.services.source_pool.node_client import (
    MIN_NODE_VERSION_SOURCE_POOL,
    SourcePoolNodeError,
    node_supports_source_pool,
)
from app.services.source_pool.render import xray_snippet
from app.services.source_pool.service import SYNC_PENDING, get_source_pool_service
from app.services.source_pool.views import node_view

router = APIRouter(prefix="/source-pool", tags=["source-pool"])

MAX_EXCLUDED = 64
EXIT_PROXY_CONFLICT = "на этой ноде включён exit-прокси — он сам выбирает исходящий адрес; выключите его в разделе Exit-прокси"


class NodeUpdate(BaseModel):
    enabled: Optional[bool] = None
    excluded: Optional[list[str]] = Field(None, max_length=MAX_EXCLUDED)

    @field_validator("excluded")
    @classmethod
    def _ipv4_only(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        cleaned = set()
        for item in value:
            try:
                cleaned.add(str(ipaddress.IPv4Address(item.strip())))
            except ValueError:
                raise ValueError(f"'{item}' is not an IPv4 address")
        return sorted(cleaned)


async def _view_for(db: AsyncSession, server_id: int) -> dict:
    row = (await db.execute(
        select(Server, SourcePoolNode)
        .outerjoin(SourcePoolNode, SourcePoolNode.server_id == Server.id)
        .where(Server.id == server_id)
    )).first()
    if row is None:
        raise HTTPException(status_code=404)
    server, node = row
    return node_view(server, node, is_server_online(server))


async def exit_proxy_enabled_on(db: AsyncSession, server_id: int) -> bool:
    row = (await db.execute(select(ExitProxyNode).where(ExitProxyNode.server_id == server_id))).scalar_one_or_none()
    return bool(row and row.enabled)


async def source_pool_enabled_on(db: AsyncSession, server_id: int) -> bool:
    """Встречная проверка для роутера exit-прокси."""
    row = (await db.execute(select(SourcePoolNode).where(SourcePoolNode.server_id == server_id))).scalar_one_or_none()
    return bool(row and row.enabled)


@router.get("/nodes")
async def list_nodes(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    rows = (await db.execute(
        select(Server, SourcePoolNode)
        .outerjoin(SourcePoolNode, SourcePoolNode.server_id == Server.id)
        .where(Server.is_active == True)  # noqa: E712
        .order_by(Server.position, Server.id)
    )).all()
    return {"nodes": [node_view(server, node, is_server_online(server)) for server, node in rows]}


@router.get("/nodes/{server_id}")
async def get_node(server_id: int, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    return await _view_for(db, server_id)


@router.put("/nodes/{server_id}")
async def update_node(
    server_id: int, body: NodeUpdate, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth),
):
    server = await get_server_by_id(server_id, db)
    node = (await db.execute(select(SourcePoolNode).where(SourcePoolNode.server_id == server_id))).scalar_one_or_none()
    enabling = body.enabled is True and (node is None or not node.enabled)
    if enabling:
        if not node_supports_source_pool(server.node_version):
            raise HTTPException(
                status_code=409,
                detail=f"агент {server.node_version or 'unknown'} старше {MIN_NODE_VERSION_SOURCE_POOL} — обновите ноду",
            )
        require_capability(server, Capability.SYSTEM, write=True)
        if await exit_proxy_enabled_on(db, server_id):
            raise HTTPException(status_code=409, detail=EXIT_PROXY_CONFLICT)

    if node is None:
        if body.enabled is not True:
            raise HTTPException(status_code=409, detail="сначала включите пул исходящих адресов на этой ноде")
        node = SourcePoolNode(server_id=server_id, enabled=True, sync_status=SYNC_PENDING)
        db.add(node)
    if body.enabled is not None:
        node.enabled = body.enabled
    if body.excluded is not None:
        node.excluded = json.dumps(body.excluded)
    node.sync_status = SYNC_PENDING
    await db.commit()

    # Довезти сразу, не дожидаясь тика: ошибка ложится в sync_error, а офлайн-нода — в очередь
    await get_source_pool_service().sync_one(server_id)
    return await _view_for(db, server_id)


@router.post("/nodes/{server_id}/refresh")
async def refresh_node(server_id: int, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    try:
        await get_source_pool_service().refresh(server_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SourcePoolNodeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return await _view_for(db, server_id)


@router.get("/snippet")
async def get_snippet(_: dict = Depends(verify_auth)):
    return xray_snippet()
