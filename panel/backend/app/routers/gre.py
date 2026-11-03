"""GRE-туннели DDoS-Guard: список туннелей сервера, состояние, проверка с CE.

Роутер тонкий: туннели держит нода, доставка и состояние — в services/gre. Здесь
приём списка от UI, номера новым туннелям и защита адреса, через который панель
ходит на ноду.
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import GreNode, Server
from app.routers.proxy import get_server_by_id, require_capability
from app.services.antiddos_manager import get_antiddos_manager
from app.services.gre import node_client
from app.services.gre.node_client import GreNodeError, node_supports_gre, unsupported_message
from app.services.gre.render import MAX_TUNNELS, GreTunnel, assign_slots, check_tunnel_set, dump_tunnels, load_tunnels
from app.services.gre.service import SYNC_PENDING, SYNC_SYNCED, get_gre_service
from app.services.gre.views import node_view
from app.services.haproxy_profile_sync import is_server_online
from app.services.node_capabilities import Capability
from app.services.node_ips import url_host

router = APIRouter(prefix="/gre", tags=["gre"])


class NodeUpdate(BaseModel):
    # Полный список туннелей сервера; у нового туннеля slot не задаётся
    tunnels: Optional[list[GreTunnel]] = Field(None, max_length=MAX_TUNNELS)
    notify: Optional[bool] = None


class CheckRequest(BaseModel):
    slot: int = Field(..., ge=1, le=MAX_TUNNELS)


async def _view_for(db: AsyncSession, server_id: int) -> dict:
    row = (await db.execute(
        select(Server, GreNode)
        .outerjoin(GreNode, GreNode.server_id == Server.id)
        .where(Server.id == server_id)
    )).first()
    if row is None:
        raise HTTPException(status_code=404)
    server, node = row
    return node_view(server, node, is_server_online(server))


def _panel_path_ce(server: Server, before: list[GreTunnel], after: list[GreTunnel]) -> Optional[str]:
    """CE, через который панель ходит на ноду, если правка его снимает или переносит."""
    host = url_host(server.url)
    kept = {(t.slot, t.ce, t.local, t.remote, t.pe) for t in after}
    for tunnel in before:
        if tunnel.ce == host and (tunnel.slot, tunnel.ce, tunnel.local, tunnel.remote, tunnel.pe) not in kept:
            return tunnel.ce
    return None


@router.get("/nodes/{server_id}")
async def get_node(server_id: int, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    return await _view_for(db, server_id)


@router.put("/nodes/{server_id}")
async def update_node(
    server_id: int, body: NodeUpdate, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth),
):
    server = await get_server_by_id(server_id, db)
    node = (await db.execute(select(GreNode).where(GreNode.server_id == server_id))).scalar_one_or_none()
    if node is None:
        # Одна галочка уведомлений без туннелей — ноде везти нечего
        node = GreNode(server_id=server_id, notify=True, sync_status=SYNC_PENDING if body.tunnels is not None else SYNC_SYNCED)
        db.add(node)
    if body.notify is not None:
        node.notify = body.notify

    tunnels_changed = body.tunnels is not None
    if tunnels_changed:
        tunnels = assign_slots(body.tunnels)
        try:
            check_tunnel_set(tunnels)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if tunnels:
            if not node_supports_gre(server.node_version):
                raise HTTPException(status_code=409, detail=unsupported_message(server.node_version))
            require_capability(server, Capability.SYSTEM, write=True)
        locked = _panel_path_ce(server, load_tunnels(node), tunnels)
        if locked:
            raise HTTPException(
                status_code=409,
                detail=f"панель ходит на ноду через {locked} — сначала смените адрес сервера в панели",
            )
        node.tunnels = dump_tunnels(tunnels)
        node.sync_status = SYNC_PENDING
    await db.commit()

    if tunnels_changed:
        # Довезти сразу, не дожидаясь тика: ошибка ложится в sync_error, а офлайн-нода — в очередь
        await get_gre_service().sync_one(server_id)
        if tunnels:
            # Адрес DDoS-Guard и сеть проверок — в белый список анти-DDoS этой ноды сразу,
            # остальным он доедет с ежечасной синхронизацией
            manager = get_antiddos_manager()
            asyncio.create_task(manager.run_bg(manager.push_whitelist_to_servers([server_id])))
    return await _view_for(db, server_id)


@router.post("/nodes/{server_id}/refresh")
async def refresh_node(server_id: int, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    try:
        await get_gre_service().refresh(server_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except GreNodeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return await _view_for(db, server_id)


@router.post("/nodes/{server_id}/check")
async def check_tunnel(
    server_id: int, body: CheckRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth),
):
    server = await get_server_by_id(server_id, db)
    try:
        return await node_client.check_tunnel(server, body.slot)
    except GreNodeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
