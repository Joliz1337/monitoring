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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_maker
from app.models import FailedLogin, PanelSettings
from app.security import drop_connection, get_client_ip, get_security_manager

settings = get_settings()
logger = logging.getLogger(__name__)

SESSION_EPOCH_KEY = "session_epoch"
SESSION_EPOCH_CLAIM = "epoch"
# Поколение сессий в памяти: процесс бэкенда один, и меняет поколение только выход.
# None — из базы ещё не читали
_session_epoch: int | None = None


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


async def current_session_epoch() -> int:
    """Поколение сессий: токен другого поколения не принимается. Из базы читается
    один раз, дальше берётся из памяти — запроса на каждый вызов API нет."""
    global _session_epoch
    if _session_epoch is not None:
        return _session_epoch
    async with async_session_maker() as db:
        stored = await db.scalar(
            select(PanelSettings.value).where(PanelSettings.key == SESSION_EPOCH_KEY)
        )
    # Пока шёл запрос, поколение мог сменить выход — его значение новее прочитанного
    if _session_epoch is None:
        _session_epoch = int(stored) if stored else 0
    return _session_epoch


async def revoke_sessions(db: AsyncSession) -> None:
    """Начать новое поколение сессий: все выданные токены перестают приниматься."""
    global _session_epoch
    # Не ниже текущего времени: после восстановления базы из бэкапа простой счётчик
    # пошёл бы по уже отозванным номерам и вернул бы силу старым токенам
    epoch = max(await current_session_epoch() + 1, int(time.time()))
    row = await db.scalar(select(PanelSettings).where(PanelSettings.key == SESSION_EPOCH_KEY))
    if row:
        row.value = str(epoch)
    else:
        db.add(PanelSettings(key=SESSION_EPOCH_KEY, value=str(epoch)))
    await db.commit()
    _session_epoch = epoch


async def revoke_sessions_after_restore() -> None:
    """База из бэкапа несёт поколение сессий на момент бэкапа: токены, отозванные
    позже, после рестарта панели снова стали бы действовать. Новое поколение
    завершает все сессии. Сбой записи восстановление не отменяет."""
    try:
        async with async_session_maker() as db:
            await revoke_sessions(db)
    except (SQLAlchemyError, OSError) as exc:
        logger.error(f"Sessions were not revoked after restore: {exc}")
        return
    logger.info("All sessions revoked after restore")


async def _session_payload(request: Request) -> dict | None:
    token = request.cookies.get("auth_token")

    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if not token:
        return None

    payload = verify_token(token)
    if not payload:
        return None

    # Токен, выданный до появления поколений, идёт как нулевое: действует до первого выхода
    if payload.get(SESSION_EPOCH_CLAIM, 0) != await current_session_epoch():
        return None

    return payload


async def verify_auth(request: Request):
    """Verify authentication - drops connection on failure"""
    payload = await _session_payload(request)
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

    token = create_token({
        "sub": "panel_user", "ip": ip, SESSION_EPOCH_CLAIM: await current_session_epoch(),
    })
    
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=settings.jwt_expire_minutes * 60
    )
    
    return {"success": True}


async def logout(request: Request, response: Response, db: AsyncSession) -> None:
    """Выход завершает все сессии: администратор один, а украденный токен иначе
    действовал бы до истечения срока. Поколение меняет только запрос с действующим
    токеном — иначе администратора разлогинивал бы любой, кто знает адрес панели."""
    response.delete_cookie("auth_token")
    if await _session_payload(request):
        await revoke_sessions(db)
        logger.info(f"Logout from {get_client_ip(request)}: all sessions revoked")
