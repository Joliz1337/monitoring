from fastapi import APIRouter

from app.capabilities import exec_denial_response
from app.models.ssl import WildcardDeployRequest, WildcardDeployResponse
from app.services.ssl_manager import get_ssl_manager

router = APIRouter(prefix="/api/ssl", tags=["ssl"])


@router.post("/wildcard/deploy", response_model=WildcardDeployResponse)
async def deploy_wildcard(request: WildcardDeployRequest):
    manager = get_ssl_manager()
    if manager.needs_exec(request) and (denied := exec_denial_response()):
        return denied
    return await manager.deploy_wildcard(request)
