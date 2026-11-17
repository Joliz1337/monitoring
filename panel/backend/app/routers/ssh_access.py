"""SSH-доступ панели к серверам: общий, для папки, свой у сервера.

Им пользуются запасной путь обновления ноды, доставка образа, обновление HAProxy
и установка Remnawave «через панель». Сохранение правит выбранный уровень и
никуда не подключается; пароли, ключи и пароль прокси write-only — наружу только
признак «задан».
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.services import ssh_access
from app.services.http_client import validate_proxy_input
from app.services.ssh_access import AccessChange, AccessPartName, AccessScope, FolderWithoutServers
from app.services.ssh_access_check import CheckAlreadyRunning, get_ssh_access_check_manager
from app.services.ssh_target import ProxyMode

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ssh-access", tags=["ssh-access"])

MAX_SCOPE_FOLDERS = 1000
MAX_SCOPE_SERVERS = 5000
MAX_PORT = 65535
SSH_USER_MAX_LENGTH = 100  # длина колонки ssh_user


class ScopeRequest(BaseModel):
    """Уровни, к которым относится запрос: общий, папки или серверы."""
    scope: AccessScope
    folders: list[str] = Field(default_factory=list, max_length=MAX_SCOPE_FOLDERS)
    server_ids: list[int] = Field(default_factory=list, max_length=MAX_SCOPE_SERVERS)


class SaveRequest(ScopeRequest):
    """Поле не передано — не менять; пустая строка или 0 — стереть."""
    ssh_user: Optional[str] = Field(None, max_length=SSH_USER_MAX_LENGTH)
    ssh_port: Optional[int] = Field(None, ge=0, le=MAX_PORT)
    ssh_password: Optional[str] = None
    ssh_private_key: Optional[str] = None
    ssh_passphrase: Optional[str] = None
    ssh_proxy: Optional[str] = None
    ssh_proxy_mode: Optional[ProxyMode] = None

    @field_validator("ssh_proxy")
    @classmethod
    def _check_proxy_format(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return value
        return validate_proxy_input(value)

    def to_change(self) -> AccessChange:
        return AccessChange(
            user=self.ssh_user,
            port=self.ssh_port,
            password=self.ssh_password,
            private_key=self.ssh_private_key,
            passphrase=self.ssh_passphrase,
            proxy=self.ssh_proxy,
            proxy_mode=self.ssh_proxy_mode,
        )


class ClearRequest(ScopeRequest):
    parts: list[AccessPartName] = Field(min_length=1)


class HostKeysRequest(BaseModel):
    server_ids: list[int] = Field(min_length=1, max_length=MAX_SCOPE_SERVERS)


@router.get("")
async def overview(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    """Что задано на каждом уровне и откуда серверы берут вход."""
    return await ssh_access.describe(db)


@router.put("")
async def save(req: SaveRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    change = req.to_change()
    if change.is_empty:
        raise HTTPException(400, "Нечего сохранять: заполните хотя бы одно поле")
    try:
        changed = await ssh_access.save_access(db, req.scope, req.folders, req.server_ids, change)
    except FolderWithoutServers as exc:
        raise HTTPException(400, str(exc)) from exc
    logger.info("SSH access saved: scope=%s levels=%d", req.scope.value, changed)
    return {"changed": changed}


@router.post("/clear")
async def clear(req: ClearRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    changed = await ssh_access.clear_access(db, req.scope, req.folders, req.server_ids, req.parts)
    logger.info(
        "SSH access cleared: scope=%s parts=%s levels=%d",
        req.scope.value, ",".join(part.value for part in req.parts), changed,
    )
    return {"changed": changed}


@router.post("/check")
async def start_check(req: ScopeRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    """Проверить сохранённый доступ: панель пробует войти на серверы области.
    Идёт фоном, ход — в GET /ssh-access/check."""
    manager = get_ssh_access_check_manager()
    if manager.is_running:
        raise HTTPException(409, "Проверка доступа уже идёт")
    subjects = await ssh_access.check_subjects(db, req.scope, req.folders, req.server_ids)
    try:
        job_id = manager.start(subjects)
    except CheckAlreadyRunning as exc:
        raise HTTPException(409, "Проверка доступа уже идёт") from exc
    logger.info("SSH access check started: scope=%s servers=%d", req.scope.value, len(subjects))
    return {"job_id": job_id}


@router.get("/check")
async def check_state(_: dict = Depends(verify_auth)):
    """Ход идущей проверки или итог последней; None — проверок не было или итог устарел."""
    return {"check": get_ssh_access_check_manager().state()}


@router.post("/check/cancel")
async def cancel_check(_: dict = Depends(verify_auth)):
    return {"cancelled": get_ssh_access_check_manager().cancel()}


@router.post("/forget-host-keys")
async def forget_host_keys(
    req: HostKeysRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth),
):
    """Принять новый ключ хоста: закреплённый сбрасывается, следующее подключение запомнит новый."""
    forgotten = await ssh_access.forget_host_keys(db, req.server_ids)
    logger.info("SSH host keys forgotten: servers=%d", forgotten)
    return {"forgotten": forgotten}
