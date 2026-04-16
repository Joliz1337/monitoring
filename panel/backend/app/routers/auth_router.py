import secrets
import time

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete
from pydantic import BaseModel

from app.database import get_db
from app.auth import login, verify_auth, get_client_ip, clear_failed_attempts
from app.config import get_settings
from app.security import drop_connection, get_security_manager
from app.models import FailedLogin

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
    # Strip whitespace from password (common copy-paste issue)
    return await login(data.password.strip(), request, response, db)


@router.post("/logout")
async def auth_logout(response: Response):
    response.delete_cookie("auth_token")
    return {"success": True}


@router.get("/check")
async def check_auth(_: dict = Depends(verify_auth)):
    return {"authenticated": True}


@router.post("/clear-ban")
async def clear_ban(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Clear ban for current IP (requires authentication)"""
    ip = get_client_ip(request)
    security = get_security_manager()
    
    # Clear from memory
    await security.unban_ip(ip)
    
    # Clear from database
    await clear_failed_attempts(ip, db)
    
    return {"success": True, "message": f"Cleared ban for IP {ip}"}


@router.delete("/clear-all-bans")
async def clear_all_bans(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Clear all IP bans (requires authentication)"""
    security = get_security_manager()
    
    # Clear all from memory
    async with security._lock:
        security._records.clear()
    
    # Clear all from database
    await db.execute(delete(FailedLogin))
    await db.commit()
    
    return {"success": True, "message": "Cleared all IP bans"}


@router.get("/ban-status")
async def ban_status(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Check ban status for current IP (no auth required for debugging)"""
    from sqlalchemy import select
    
    ip = get_client_ip(request)
    security = get_security_manager()
    
    # Check memory
    memory_banned = security.is_banned(ip)
    memory_record = security._records.get(ip)
    memory_info = None
    if memory_record:
        memory_info = {
            "failed_attempts": memory_record.failed_attempts,
            "banned_until": memory_record.banned_until,
            "remaining_seconds": max(0, int(memory_record.banned_until - time.time())) if memory_record.banned_until else 0
        }
    
    # Check database
    result = await db.execute(
        select(FailedLogin).where(FailedLogin.ip_address == ip)
    )
    db_record = result.scalar_one_or_none()
    db_info = None
    if db_record:
        db_info = {
            "attempts": db_record.attempts,
            "banned_until": db_record.banned_until,
            "remaining_seconds": max(0, int(db_record.banned_until - time.time())) if db_record.banned_until else 0
        }
    
    return {
        "ip": ip,
        "memory_banned": memory_banned,
        "memory_info": memory_info,
        "db_info": db_info
    }
