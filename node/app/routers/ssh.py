"""SSH management router"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from app.services.ssh_config_manager import get_ssh_config_manager

router = APIRouter(prefix="/api/ssh", tags=["ssh"])


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


# --- SSH Config ---

@router.get("/config")
async def get_config():
    manager = get_ssh_config_manager()
    config = manager.read_sshd_config()
    return {"config": config}


@router.post("/config")
async def apply_config(request: SSHConfigUpdate):
    manager = get_ssh_config_manager()
    updates = request.model_dump(exclude_none=True)

    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    success, message, warnings = manager.write_sshd_config(updates)
    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"success": True, "message": message, "warnings": warnings}


@router.post("/config/test")
async def test_config(request: SSHConfigUpdate):
    manager = get_ssh_config_manager()
    updates = request.model_dump(exclude_none=True)

    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    valid, errors = manager.test_sshd_config(updates)
    return {"valid": valid, "errors": errors}


# --- Fail2ban ---

@router.get("/fail2ban/status")
async def get_fail2ban_status():
    manager = get_ssh_config_manager()
    return manager.read_fail2ban_config()


@router.post("/fail2ban/config")
async def update_fail2ban_config(request: Fail2banConfigUpdate):
    manager = get_ssh_config_manager()
    updates = request.model_dump(exclude_none=True)

    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    success, message = manager.write_fail2ban_config(updates)
    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"success": True, "message": message}


@router.get("/fail2ban/banned")
async def get_banned_ips():
    manager = get_ssh_config_manager()
    banned = manager.get_fail2ban_banned()
    return {"count": len(banned), "ips": banned}


@router.post("/fail2ban/unban")
async def unban_ip(request: UnbanRequest):
    manager = get_ssh_config_manager()
    success, message = manager.unban_ip(request.ip)
    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"success": True, "message": message, "ip": request.ip}


@router.post("/fail2ban/unban-all")
async def unban_all():
    manager = get_ssh_config_manager()
    success, message = manager.unban_all()
    if not success:
        raise HTTPException(status_code=500, detail=message)

    return {"success": True, "message": message}


# --- SSH Keys ---

@router.get("/keys")
async def list_keys(user: str = "root"):
    manager = get_ssh_config_manager()
    keys = manager.list_authorized_keys(user)
    return {"user": user, "count": len(keys), "keys": keys}


@router.post("/keys")
async def add_key(request: SSHKeyAdd):
    manager = get_ssh_config_manager()
    success, message, fingerprint = manager.add_authorized_key(request.user, request.public_key)
    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"success": True, "message": message, "fingerprint": fingerprint, "user": request.user}


@router.delete("/keys")
async def remove_key(request: SSHKeyRemove):
    manager = get_ssh_config_manager()
    success, message = manager.remove_authorized_key(request.user, request.fingerprint)
    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"success": True, "message": message, "fingerprint": request.fingerprint}


# --- Password ---

@router.post("/password")
async def change_password(request: ChangePasswordRequest):
    manager = get_ssh_config_manager()
    success, message = manager.change_password(request.user, request.password)
    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"success": True, "message": message, "user": request.user}


# --- Status ---

@router.get("/status")
async def get_status():
    manager = get_ssh_config_manager()
    status = manager.get_status()
    return status
