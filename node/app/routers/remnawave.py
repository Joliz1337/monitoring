"""Remnawave: статус remnanode и управление nginx установки.

No auth dependency: nginx терминирует mTLS панельным CA до того,
как запрос попадает в uvicorn.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query

from app.models.remnawave_nginx import (
    NginxActionResponse,
    NginxApplyRequest,
    NginxApplyResponse,
    NginxConfigResponse,
    NginxDiscoverResponse,
    NginxStatusResponse,
    NginxValidateRequest,
    NginxValidateResponse,
)
from app.services.remnawave_nginx_manager import (
    DEFAULT_INSTALL_PATH,
    InvalidInstallPathError,
    get_remnawave_nginx_manager,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/remnawave", tags=["remnawave"])

CONTAINER_NAME = "remnanode"


async def _check_container_available() -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return stdout.decode().strip().lower() == "true"
    except Exception:
        return False


@router.get("/status")
async def get_status():
    """Check if remnanode container is running."""
    available = await _check_container_available()
    return {"available": available}


PathParam = Query(default=DEFAULT_INSTALL_PATH, min_length=1)


@router.get("/nginx/discover", response_model=NginxDiscoverResponse)
async def nginx_discover(path: str = PathParam):
    try:
        return await get_remnawave_nginx_manager().discover(path)
    except InvalidInstallPathError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/nginx/config", response_model=NginxConfigResponse)
async def nginx_get_config(path: str = PathParam):
    try:
        return await get_remnawave_nginx_manager().get_config(path)
    except InvalidInstallPathError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/nginx/status", response_model=NginxStatusResponse)
async def nginx_status(path: str = PathParam):
    try:
        return await get_remnawave_nginx_manager().status(path)
    except InvalidInstallPathError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/nginx/logs")
async def nginx_logs(tail: int = Query(default=100, ge=1, le=1000)):
    logs = await get_remnawave_nginx_manager().logs(tail)
    return {"logs": logs}


@router.post("/nginx/validate", response_model=NginxValidateResponse)
async def nginx_validate(request: NginxValidateRequest):
    try:
        valid, output = await get_remnawave_nginx_manager().validate_content(
            request.path, request.content
        )
    except InvalidInstallPathError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return NginxValidateResponse(valid=valid, output=output)


@router.post("/nginx/config/apply", response_model=NginxApplyResponse)
async def nginx_apply_config(request: NginxApplyRequest):
    try:
        result = await get_remnawave_nginx_manager().apply_config(
            path=request.path,
            content=request.content,
            reload_after=request.reload_after,
            restart=request.restart,
            ensure_started=request.ensure_started,
        )
    except InvalidInstallPathError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return NginxApplyResponse(**result)


@router.post("/nginx/reload", response_model=NginxActionResponse)
async def nginx_reload():
    success, message = await get_remnawave_nginx_manager().reload()
    return NginxActionResponse(success=success, message=message)


@router.post("/nginx/restart", response_model=NginxActionResponse)
async def nginx_restart():
    success, message = await get_remnawave_nginx_manager().restart()
    return NginxActionResponse(success=success, message=message)
