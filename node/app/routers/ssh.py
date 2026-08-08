"""SSH management router"""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from app.services.ssh_config_manager import SSHConfigManager, get_ssh_config_manager

router = APIRouter(prefix="/api/ssh", tags=["ssh"])

# Перенастройка sshd занимает минуты (ожидание порта, стоп/старт службы,
# установка fail2ban), а менеджер целиком синхронный. Без тред-пула один такой
# запрос заморозил бы event loop вместе с /health, и docker-healthcheck
# (interval 5s, retries 10) убил бы контейнер посреди перенастройки sshd.

# Два параллельных применения чередовали бы бэкап, подмену конфига и рестарт
# службы. Ключи под тем же локом: write_sshd_config перед отключением парольного
# входа проверяет, что authorized_keys не пуст — параллельное удаление ключа
# между проверкой и применением отрезало бы доступ к серверу.
_sshd_lock = asyncio.Lock()

# apt-get (dpkg-lock) и restart fail2ban не переживают параллельного запуска.
_fail2ban_lock = asyncio.Lock()


class SSHConfigUpdate(BaseModel):
    port: Optional[int] = Field(None, ge=1, le=65535)
    permit_root_login: Optional[str] = Field(None, pattern="^(yes|no|prohibit-password)$")
    password_authentication: Optional[bool] = None
    pubkey_authentication: Optional[bool] = None
    max_auth_tries: Optional[int] = Field(None, ge=1, le=10)
    login_grace_time: Optional[int] = Field(None, ge=10, le=600)
    client_alive_interval: Optional[int] = Field(None, ge=0, le=3600)
    client_alive_count_max: Optional[int] = Field(None, ge=1, le=10)
    max_sessions: Optional[int] = Field(None, ge=1, le=20)
    max_startups: Optional[str] = None
    allow_users: Optional[list[str]] = None
    x11_forwarding: Optional[bool] = None


class Fail2banConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    max_retry: Optional[int] = Field(None, ge=1, le=20)
    ban_time: Optional[int] = Field(None, ge=60, le=2592000)
    find_time: Optional[int] = Field(None, ge=60, le=86400)


class SSHKeyAdd(BaseModel):
    public_key: str = Field(..., min_length=20)
    user: str = Field("root")


class SSHKeyRemove(BaseModel):
    fingerprint: str = Field(...)
    user: str = Field("root")


class UnbanRequest(BaseModel):
    ip: str = Field(...)


class ChangePasswordRequest(BaseModel):
    user: str = Field("root")
    password: str = Field(..., min_length=8)


async def _get_manager() -> SSHConfigManager:
    """__init__ менеджера сам по себе делает около десятка subprocess-вызовов."""
    return await asyncio.to_thread(get_ssh_config_manager)


# --- SSH Config ---

@router.get("/config")
async def get_config():
    manager = await _get_manager()
    config = await asyncio.to_thread(manager.read_sshd_config)
    return {"config": config}


@router.post("/config")
async def apply_config(request: SSHConfigUpdate):
    updates = request.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    manager = await _get_manager()
    async with _sshd_lock:
        success, message, warnings = await asyncio.to_thread(manager.write_sshd_config, updates)

    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"success": True, "message": message, "warnings": warnings}


@router.post("/config/test")
async def test_config(request: SSHConfigUpdate):
    updates = request.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    manager = await _get_manager()
    async with _sshd_lock:
        valid, errors = await asyncio.to_thread(manager.test_sshd_config, updates)

    return {"valid": valid, "errors": errors}


# --- Fail2ban ---

@router.get("/fail2ban/status")
async def get_fail2ban_status():
    manager = await _get_manager()
    return await asyncio.to_thread(manager.read_fail2ban_config)


@router.post("/fail2ban/config")
async def update_fail2ban_config(request: Fail2banConfigUpdate):
    updates = request.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    manager = await _get_manager()
    async with _fail2ban_lock:
        success, message = await asyncio.to_thread(manager.write_fail2ban_config, updates)

    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"success": True, "message": message}


@router.get("/fail2ban/banned")
async def get_banned_ips():
    manager = await _get_manager()
    banned = await asyncio.to_thread(manager.get_fail2ban_banned)
    return {"count": len(banned), "ips": banned}


@router.post("/fail2ban/unban")
async def unban_ip(request: UnbanRequest):
    manager = await _get_manager()
    success, message = await asyncio.to_thread(manager.unban_ip, request.ip)
    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"success": True, "message": message, "ip": request.ip}


@router.post("/fail2ban/unban-all")
async def unban_all():
    manager = await _get_manager()
    success, message = await asyncio.to_thread(manager.unban_all)
    if not success:
        raise HTTPException(status_code=500, detail=message)

    return {"success": True, "message": message}


# --- SSH Keys ---

@router.get("/keys")
async def list_keys(user: str = "root"):
    manager = await _get_manager()
    keys = await asyncio.to_thread(manager.list_authorized_keys, user)
    return {"user": user, "count": len(keys), "keys": keys}


@router.post("/keys")
async def add_key(request: SSHKeyAdd):
    manager = await _get_manager()
    async with _sshd_lock:
        success, message, fingerprint = await asyncio.to_thread(
            manager.add_authorized_key, request.user, request.public_key,
        )

    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"success": True, "message": message, "fingerprint": fingerprint, "user": request.user}


@router.delete("/keys")
async def remove_key(request: SSHKeyRemove):
    manager = await _get_manager()
    async with _sshd_lock:
        success, message = await asyncio.to_thread(
            manager.remove_authorized_key, request.user, request.fingerprint,
        )

    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"success": True, "message": message, "fingerprint": request.fingerprint}


# --- Password ---

@router.post("/password")
async def change_password(request: ChangePasswordRequest):
    manager = await _get_manager()
    success, message = await asyncio.to_thread(
        manager.change_password, request.user, request.password,
    )
    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"success": True, "message": message, "user": request.user}


# --- Status ---

@router.get("/status")
async def get_status():
    manager = await _get_manager()
    return await asyncio.to_thread(manager.get_status)
