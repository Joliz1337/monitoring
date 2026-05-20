import logging

from fastapi import APIRouter, Query

from app.models.ssl import WildcardDeployRequest, WildcardDeployResponse, WildcardStatusResponse
from app.services.ssl_manager import get_ssl_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ssl", tags=["ssl"])


@router.post("/wildcard/deploy", response_model=WildcardDeployResponse)
async def deploy_wildcard(request: WildcardDeployRequest):
    manager = get_ssl_manager()
    return await manager.deploy_wildcard(request)


@router.get("/wildcard/status", response_model=WildcardStatusResponse)
async def wildcard_status(deploy_path: str = Query(..., min_length=1)):
    manager = get_ssl_manager()
    return await manager.get_status(deploy_path)
