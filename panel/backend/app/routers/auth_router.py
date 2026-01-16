import secrets

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.database import get_db
from app.auth import login, verify_auth
from app.config import get_settings
from app.security import drop_connection

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()


class LoginRequest(BaseModel):
    password: str


class ValidateUidRequest(BaseModel):
    uid: str


@router.post("/validate-uid")
async def validate_uid(data: ValidateUidRequest):
    """Validate panel UID - timing-safe comparison, drops connection on invalid"""
    is_valid = secrets.compare_digest(data.uid, settings.panel_uid)
    if not is_valid:
        drop_connection()
    return {"valid": True}


@router.post("/login")
async def auth_login(
    data: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db)
):
    return await login(data.password, request, response, db)


@router.post("/logout")
async def auth_logout(response: Response):
    response.delete_cookie("auth_token")
    return {"success": True}


@router.get("/check")
async def check_auth(_: dict = Depends(verify_auth)):
    return {"authenticated": True}
