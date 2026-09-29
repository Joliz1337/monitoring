"""Обновление HAProxy на ноде до новейшей официальной LTS-сборки.

POST /servers/{id}/haproxy-upgrade запускает install.sh на хосте через агента
фоновой задачей и возвращает job_id; лог читается через
GET /servers/haproxy-upgrade/{job_id}/stream (NDJSON, переподключаемый),
список задач — через GET /servers/haproxy-upgrade/jobs.
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.routers.proxy import get_server_by_id, require_capability
from app.services.haproxy_upgrade import get_haproxy_upgrade_manager, running_job_id, start_upgrade
from app.services.node_capabilities import Capability

router = APIRouter(prefix="/servers", tags=["haproxy-upgrade"])


def _ndjson(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


@router.post("/{server_id}/haproxy-upgrade")
async def start_haproxy_upgrade(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await get_server_by_id(server_id, db)
    require_capability(server, Capability.EXEC, write=True)
    if running_job_id(server.id):
        raise HTTPException(409, "Обновление HAProxy на этом сервере уже идёт")
    return {"job_id": start_upgrade(server)}


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
