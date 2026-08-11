"""Authentication with connection drop security

All auth failures result in connection drop - no HTTP error responses.
This gives attackers zero information about what went wrong.
"""

import logging
import secrets
import time
import jwt
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, Request, Response
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import FailedLogin
from app.security import drop_connection, get_client_ip, get_security_manager

settings = get_settings()
logger = logging.getLogger(__name__)


async def check_ip_banned(ip: str, db: AsyncSession) -> bool:
    """Check if IP is banned in database"""
    result = await db.execute(
        select(FailedLogin).where(FailedLogin.ip_address == ip)
    )
    record = result.scalar_one_or_none()
    
    if record and record.banned_until:
        if time.time() < record.banned_until:
            return True
        else:
            await db.delete(record)
            await db.commit()
    
    return False


async def record_failed_attempt(ip: str, db: AsyncSession) -> bool:
    """Record failed login - returns True if now banned"""
    result = await db.execute(
        select(FailedLogin).where(FailedLogin.ip_address == ip)
    )
    record = result.scalar_one_or_none()
    
    now = time.time()
    
    if record:
        record.attempts += 1
        record.last_attempt = now
        
        if record.attempts >= settings.max_failed_attempts:
            record.banned_until = now + settings.ban_duration_seconds
            await db.commit()
            return True
    else:
        record = FailedLogin(
            ip_address=ip,
            attempts=1,
            last_attempt=now
        )
        db.add(record)
    
    await db.commit()
    return False


async def clear_failed_attempts(ip: str, db: AsyncSession):
    """Clear failed attempts on successful login"""
    await db.execute(
        delete(FailedLogin).where(FailedLogin.ip_address == ip)
    )
    await db.commit()


def create_token(data: dict) -> str:
    """Create JWT token"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    to_encode = data.copy()
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_token(token: str) -> dict | None:
    """Verify JWT token"""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return payload
    except jwt.PyJWTError:
        return None


async def ensure_not_banned(ip: str, db: AsyncSession):
    """Бан живёт в памяти и в БД: память теряет его при рестарте, БД — переживает."""
    memory_banned = get_security_manager().is_banned(ip)
    db_banned = await check_ip_banned(ip, db)

    if memory_banned or db_banned:
        logger.warning(f"Request blocked for {ip}: memory_banned={memory_banned}, db_banned={db_banned}")
        drop_connection()


async def register_auth_failure(ip: str, db: AsyncSession):
    """Неудачная попытка входа или подбор UID — считаем в обоих хранилищах бана."""
    await record_failed_attempt(ip, db)
    await get_security_manager().record_auth_failure(ip)


async def verify_auth(request: Request):
    """Verify authentication - drops connection on failure"""
    token = request.cookies.get("auth_token")
    
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]
    
    # No token - drop connection (caught by middleware)
    if not token:
        raise HTTPException(status_code=401)
    
    # Invalid token - drop connection
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401)
    
    return payload


async def login(password: str, request: Request, response: Response, db: AsyncSession) -> dict:
    """Login - drops connection on any failure"""
    ip = get_client_ip(request)
    await ensure_not_banned(ip, db)

    # Timing-safe password comparison
    if not secrets.compare_digest(password, settings.panel_password):
        logger.warning(f"Invalid password from {ip}")
        await register_auth_failure(ip, db)
        drop_connection()

    # Success - clear failed attempts
    logger.info(f"Successful login from {ip}")
    await clear_failed_attempts(ip, db)
    await get_security_manager().record_auth_success(ip)

    token = create_token({"sub": "panel_user", "ip": ip})
    
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=settings.jwt_expire_minutes * 60
    )
    
    return {"success": True, "token": token}
