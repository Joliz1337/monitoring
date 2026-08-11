import secrets

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.database import get_db
from app.auth import (
    ensure_not_banned,
    login,
    register_auth_failure,
    verify_auth,
)
from app.config import get_settings
from app.security import drop_connection, get_client_ip

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()


class LoginRequest(BaseModel):
    password: str


class ValidateUidRequest(BaseModel):
    uid: str


@router.post("/validate-uid")
async def validate_uid(
    data: ValidateUidRequest,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Validate panel UID - timing-safe comparison, drops connection on invalid.

    Неудачные попытки считаются наравне с паролем: без этого UID перебирался бы
    бесконечно — drop connection сам по себе перебор не замедляет.
    """
    ip = get_client_ip(request)
    await ensure_not_banned(ip, db)

    if not secrets.compare_digest(data.uid, settings.panel_uid):
        await register_auth_failure(ip, db)
        drop_connection()

    return {"valid": True}


@router.post("/login")
async def auth_login(
    data: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db)
):
    # Strip whitespace from password (common copy-paste issue)
    return await login(data.password.strip(), request, response, db)


@router.post("/logout")
async def auth_logout(response: Response):
    response.delete_cookie("auth_token")
    return {"success": True}


@router.get("/check")
async def check_auth(_: dict = Depends(verify_auth)):
    return {"authenticated": True}

