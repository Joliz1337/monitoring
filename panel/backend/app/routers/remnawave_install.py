"""Установка ноды Remnawave на уже добавленный сервер (через агента ноды).

POST /servers/{id}/install-remnawave запускает фоновую задачу и возвращает
job_id; лог читается через GET /servers/remnawave-install/{job_id}/stream
(NDJSON, переподключаемый), список задач — через GET /servers/remnawave-install/jobs.
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.routers.proxy import get_server_by_id, require_capability
from app.routers.server_deploy import resolve_remnawave_cert
from app.services.deploy_service import build_remnawave_install_command
from app.services.node_capabilities import Capability
from app.services.remnawave_node_install import get_remnawave_install_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/servers", tags=["remnawave-install"])


class RemnawaveInstallRequest(BaseModel):
    remnawave_cert_profile_id: Optional[int] = None
    remnawave_cert_inline: Optional[str] = None
    save_remnawave_cert: bool = False
    save_remnawave_cert_name: Optional[str] = None


def _ndjson(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


@router.post("/{server_id}/install-remnawave")
async def install_remnawave(
    server_id: int,
    req: RemnawaveInstallRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Запустить фоновую установку ноды Remnawave на сервере через агента."""
    server = await get_server_by_id(server_id, db)
    require_capability(server, Capability.EXEC, write=True)

    cert = await resolve_remnawave_cert(
        req.remnawave_cert_inline,
        req.remnawave_cert_profile_id,
        save=req.save_remnawave_cert,
        save_name=req.save_remnawave_cert_name,
    )
    command = build_remnawave_install_command(cert)
    job_id = get_remnawave_install_manager().start(server, command)
    return {"job_id": job_id}


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
