"""Обновление HAProxy на ноде до новейшей официальной LTS-сборки.

POST /servers/{id}/haproxy-upgrade запускает install.sh на хосте через агента
(или, с via_panel, по SSH с загрузкой пакетов через панель) фоновой задачей и
возвращает job_id; лог читается через
GET /servers/haproxy-upgrade/{job_id}/stream (NDJSON, переподключаемый),
список задач — через GET /servers/haproxy-upgrade/jobs.
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.routers.node_image import SSHAccessRequest
from app.routers.proxy import get_server_by_id, require_capability
from app.services.haproxy_upgrade import get_haproxy_upgrade_manager, running_job_id, start_upgrade
from app.services.node_capabilities import Capability
from app.services.ssh_target import SSHTargetError, resolve_ssh_target

router = APIRouter(prefix="/servers", tags=["haproxy-upgrade"])


class HAProxyUpgradeRequest(BaseModel):
    # Сервер за ТСПУ: обновление по SSH, пакеты качаются через панель.
    # ssh — разовые креды поверх сохранённых у сервера
    via_panel: bool = False
    ssh: SSHAccessRequest = Field(default_factory=SSHAccessRequest)


def _ndjson(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


@router.post("/{server_id}/haproxy-upgrade")
async def start_haproxy_upgrade(
    server_id: int,
    req: Optional[HAProxyUpgradeRequest] = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await get_server_by_id(server_id, db)
    req = req or HAProxyUpgradeRequest()
    ssh_target = None
    if req.via_panel:
        try:
            ssh_target = resolve_ssh_target(server, **req.ssh.model_dump())
        except SSHTargetError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        # По SSH агент не участвует — право exec нужно только обновлению через него
        require_capability(server, Capability.EXEC, write=True)
    if running_job_id(server.id):
        raise HTTPException(409, "Обновление HAProxy на этом сервере уже идёт")
    return {"job_id": start_upgrade(server, ssh_target)}


@router.get("/haproxy-upgrade/jobs")
async def list_haproxy_upgrade_jobs(_: dict = Depends(verify_auth)):
    """Идущие и недавно завершённые обновления — статусы на карточках нод."""
    return {"jobs": get_haproxy_upgrade_manager().list_jobs()}


@router.get("/haproxy-upgrade/{job_id}/stream")
async def stream_haproxy_upgrade_job(job_id: str, _: dict = Depends(verify_auth)):
    """NDJSON-стрим лога обновления. Переподключаемый."""
    manager = get_haproxy_upgrade_manager()
    if manager.get(job_id) is None:
        raise HTTPException(404, "Задача обновления HAProxy не найдена")

    async def generate():
        async for event in manager.subscribe(job_id):
            yield _ndjson(event)

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
