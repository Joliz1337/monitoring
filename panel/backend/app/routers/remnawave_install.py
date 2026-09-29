"""Установка ноды Remnawave на уже добавленный сервер.

Обычно — через агента ноды; с via_panel (сервер за ТСПУ) — по SSH, и всё, что
качает установщик, идёт через панель. POST /servers/{id}/install-remnawave
запускает фоновую задачу и возвращает job_id; лог читается через
GET /servers/remnawave-install/{job_id}/stream (NDJSON, переподключаемый),
список задач — через GET /servers/remnawave-install/jobs.
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import Server
from app.routers.node_image import SSHAccessRequest, SSHTargetError, resolve_ssh_target
from app.routers.proxy import get_server_by_id, require_capability
from app.routers.server_deploy import resolve_remnawave_cert
from app.services.deploy_service import build_remnawave_install_command, install_via_panel
from app.services.node_capabilities import Capability
from app.services.remnawave_node_install import get_remnawave_install_manager, run_install_on_node
from app.services.ssh_target import SSHTarget

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/servers", tags=["remnawave-install"])


class RemnawaveInstallRequest(BaseModel):
    remnawave_cert_profile_id: Optional[int] = None
    remnawave_cert_inline: Optional[str] = None
    save_remnawave_cert: bool = False
    save_remnawave_cert_name: Optional[str] = None
    # Сервер за ТСПУ: установка по SSH, всё качается через панель.
    # ssh — разовые креды поверх сохранённых у сервера
    via_panel: bool = False
    ssh: SSHAccessRequest = Field(default_factory=SSHAccessRequest)


def _ndjson(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


@router.post("/{server_id}/install-remnawave")
async def install_remnawave(
    server_id: int,
    req: RemnawaveInstallRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Запустить фоновую установку ноды Remnawave на сервере."""
    server = await get_server_by_id(server_id, db)
    ssh_target = _ssh_target_or_400(server, req.ssh) if req.via_panel else None
    if ssh_target is None:
        # По SSH агент не участвует — право exec нужно только установке через него
        require_capability(server, Capability.EXEC, write=True)

    cert = await resolve_remnawave_cert(
        req.remnawave_cert_inline,
        req.remnawave_cert_profile_id,
        save=req.save_remnawave_cert,
        save_name=req.save_remnawave_cert_name,
    )
    if ssh_target is None:
        events = run_install_on_node(server, build_remnawave_install_command(cert))
    else:
        events = install_via_panel(
            ssh_target,
            server.proxy_url,
            lambda tunnel_proxy: build_remnawave_install_command(cert, tunnel_proxy),
        )
    job_id = get_remnawave_install_manager().start(server, events)
    return {"job_id": job_id}


def _ssh_target_or_400(server: Server, access: SSHAccessRequest) -> SSHTarget:
    try:
        return resolve_ssh_target(server, access)
    except SSHTargetError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/remnawave-install/jobs")
async def list_remnawave_install_jobs(_: dict = Depends(verify_auth)):
    """Активные и недавно завершённые установки — для восстановления UI."""
    return {"jobs": get_remnawave_install_manager().list_jobs()}


@router.get("/remnawave-install/{job_id}/stream")
async def stream_remnawave_install_job(job_id: str, _: dict = Depends(verify_auth)):
    """NDJSON-стрим лога установки. Переподключаемый."""
    manager = get_remnawave_install_manager()
    if manager.get(job_id) is None:
        raise HTTPException(404, "Задача установки не найдена")

    async def generate():
        async for event in manager.subscribe(job_id):
            yield _ndjson(event)

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
