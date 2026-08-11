"""Ключи установки под один сервер — выдача для нод, к которым нет своего доступа."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import NodeInstallKey
from app.services.net_utils import resolve_panel_ip
from app.services.pki import PKIKeygenData
from app.services.node_install_keys import (
    DEFAULT_VALIDITY_DAYS,
    MAX_VALIDITY_DAYS,
    MIN_VALIDITY_DAYS,
    build_install_command,
    build_key_token,
    create_install_key,
    delete_install_key,
    list_install_keys,
)

router = APIRouter(prefix="/install-keys", tags=["install-keys"])


class InstallKeyCreate(BaseModel):
    name: str = Field(max_length=200)
    validity_days: int = Field(
        default=DEFAULT_VALIDITY_DAYS,
        ge=MIN_VALIDITY_DAYS,
        le=MAX_VALIDITY_DAYS,
    )

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        name = v.strip()
        if not name:
            raise ValueError("name must not be empty")
        return name


class InstallKeyResponse(BaseModel):
    id: int
    name: str
    fingerprint: str
    expires_at: str
    created_at: str
    token: str
    install_command: str


def _iso_utc(dt: datetime) -> str:
    """Постгрес отдаёт timestamptz в tz сессии — приводим к UTC явно."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _to_response(
    key: NodeInstallKey,
    keygen: PKIKeygenData,
    panel_ip: str | None,
) -> InstallKeyResponse:
    token = build_key_token(keygen, key, panel_ip=panel_ip)
    return InstallKeyResponse(
        id=key.id,
        name=key.name,
        fingerprint=key.fingerprint,
        expires_at=_iso_utc(key.expires_at),
        created_at=_iso_utc(key.created_at),
        token=token,
        install_command=build_install_command(token),
    )


@router.get("")
async def get_install_keys(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    keys = await list_install_keys(db)
    panel_ip = await resolve_panel_ip()
    keygen = request.app.state.pki
    return {"keys": [_to_response(key, keygen, panel_ip) for key in keys]}


@router.post("")
async def post_install_key(
    data: InstallKeyCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    keygen = request.app.state.pki
    key = await create_install_key(db, keygen, data.name, data.validity_days)
    panel_ip = await resolve_panel_ip()
    return _to_response(key, keygen, panel_ip)


@router.delete("/{key_id}")
async def remove_install_key(
    key_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    if not await delete_install_key(db, key_id):
        raise HTTPException(status_code=404, detail="Install key not found")
    return {"success": True}
