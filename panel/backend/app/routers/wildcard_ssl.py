import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import async_session, get_db
from app.models import WildcardCertificate, Server, PanelSettings
from app.services.wildcard_ssl import get_wildcard_ssl_manager, get_issue_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wildcard-ssl", tags=["wildcard-ssl"])


# ─── Schemas ───

class IssueCertRequest(BaseModel):
    domain: str = Field(..., min_length=1, max_length=253)
    email: str = ""


class ServerConfigUpdate(BaseModel):
    wildcard_ssl_enabled: Optional[bool] = None
    wildcard_ssl_deploy_path: Optional[str] = None
    wildcard_ssl_reload_cmd: Optional[str] = None
    wildcard_ssl_fullchain_name: Optional[str] = None
    wildcard_ssl_privkey_name: Optional[str] = None
    wildcard_ssl_custom_path_enabled: Optional[bool] = None
    wildcard_ssl_custom_fullchain_path: Optional[str] = None
    wildcard_ssl_custom_privkey_path: Optional[str] = None


class SettingsUpdate(BaseModel):
    cloudflare_api_token: Optional[str] = None
    email: Optional[str] = None
    auto_renew_enabled: Optional[bool] = None
    renew_days_before: Optional[int] = None


# ─── Certificates ───

@router.get("/certificates")
async def list_certificates(_: dict = Depends(verify_auth)):
    async with async_session() as db:
        rows = (await db.execute(select(WildcardCertificate))).scalars().all()
        return {
            "certificates": [_cert_to_dict(c) for c in rows]
        }


@router.get("/certificates/{cert_id}")
async def get_certificate(cert_id: int, _: dict = Depends(verify_auth)):
    async with async_session() as db:
        cert = (await db.execute(
            select(WildcardCertificate).where(WildcardCertificate.id == cert_id)
        )).scalar_one_or_none()
        if not cert:
            raise HTTPException(status_code=404)
        return _cert_to_dict(cert)


@router.post("/certificates/issue")
async def issue_certificate(
    req: IssueCertRequest,
    background_tasks: BackgroundTasks,
    _: dict = Depends(verify_auth),
):
    status = get_issue_status()
    if status["in_progress"]:
        raise HTTPException(status_code=409, detail="Issuance already in progress")

    async with async_session() as db:
        token = await _get_setting(db, "wildcard_cloudflare_api_token")
        if not token:
            raise HTTPException(status_code=400, detail="Cloudflare API token not configured")

    manager = get_wildcard_ssl_manager()
    email = req.email

    if not email:
        async with async_session() as db:
            email = await _get_setting(db, "wildcard_email") or ""

    background_tasks.add_task(manager.issue_certificate, req.domain, email, token)
    return {"success": True, "message": "Certificate issuance started"}


@router.get("/issue-status")
async def issue_status(_: dict = Depends(verify_auth)):
    return get_issue_status()


@router.post("/certificates/{cert_id}/renew")
async def renew_certificate(cert_id: int, _: dict = Depends(verify_auth)):
    manager = get_wildcard_ssl_manager()
    ok, msg = await manager.renew_certificate(cert_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg}


@router.delete("/certificates/{cert_id}")
async def delete_certificate(cert_id: int, _: dict = Depends(verify_auth)):
    async with async_session() as db:
        cert = (await db.execute(
            select(WildcardCertificate).where(WildcardCertificate.id == cert_id)
        )).scalar_one_or_none()
        if not cert:
            raise HTTPException(status_code=404)
        await db.delete(cert)
        await db.commit()
    return {"success": True}


# ─── Deploy ───

@router.post("/certificates/{cert_id}/deploy")
async def deploy_to_all(cert_id: int, _: dict = Depends(verify_auth)):
    manager = get_wildcard_ssl_manager()
    results = await manager.deploy_to_all(cert_id)
    return {"results": results}


@router.post("/certificates/{cert_id}/deploy/{server_id}")
async def deploy_to_server(cert_id: int, server_id: int, _: dict = Depends(verify_auth)):
    manager = get_wildcard_ssl_manager()
    result = await manager.deploy_to_server(cert_id, server_id)
    return result


# ─── Settings ───

@router.get("/settings")
async def get_settings(_: dict = Depends(verify_auth)):
    async with async_session() as db:
        token = await _get_setting(db, "wildcard_cloudflare_api_token") or ""
        masked = ("*" * (len(token) - 4) + token[-4:]) if len(token) > 4 else "****" if token else ""
        return {
            "cloudflare_api_token": masked,
            "cloudflare_api_token_set": bool(token),
            "email": await _get_setting(db, "wildcard_email") or "",
            "auto_renew_enabled": (await _get_setting(db, "wildcard_auto_renew_enabled")) == "true",
            "renew_days_before": int(await _get_setting(db, "wildcard_renew_days_before") or "30"),
        }


@router.get("/settings/token")
async def get_token_raw(_: dict = Depends(verify_auth)):
    async with async_session() as db:
        token = await _get_setting(db, "wildcard_cloudflare_api_token") or ""
        return {"cloudflare_api_token": token}


@router.put("/settings")
async def update_settings(req: SettingsUpdate, _: dict = Depends(verify_auth)):
    async with async_session() as db:
        if req.cloudflare_api_token is not None:
            await _set_setting(db, "wildcard_cloudflare_api_token", req.cloudflare_api_token)
        if req.email is not None:
            await _set_setting(db, "wildcard_email", req.email)
        if req.auto_renew_enabled is not None:
            await _set_setting(db, "wildcard_auto_renew_enabled", "true" if req.auto_renew_enabled else "false")
        if req.renew_days_before is not None:
            await _set_setting(db, "wildcard_renew_days_before", str(req.renew_days_before))
        await db.commit()
    return {"success": True}


# ─── Server config ───

@router.get("/servers")
async def get_servers(_: dict = Depends(verify_auth)):
    async with async_session() as db:
        servers = (await db.execute(
            select(Server).where(Server.is_active == True).order_by(Server.position)
        )).scalars().all()
        return {
            "servers": [
                {
                    "server_id": s.id,
                    "server_name": s.name,
                    "wildcard_ssl_enabled": s.wildcard_ssl_enabled or False,
                    "wildcard_ssl_deploy_path": s.wildcard_ssl_deploy_path or "",
                    "wildcard_ssl_reload_cmd": s.wildcard_ssl_reload_cmd or "",
                    "wildcard_ssl_fullchain_name": s.wildcard_ssl_fullchain_name or "",
                    "wildcard_ssl_privkey_name": s.wildcard_ssl_privkey_name or "",
                    "wildcard_ssl_custom_path_enabled": s.wildcard_ssl_custom_path_enabled or False,
                    "wildcard_ssl_custom_fullchain_path": s.wildcard_ssl_custom_fullchain_path or "",
                    "wildcard_ssl_custom_privkey_path": s.wildcard_ssl_custom_privkey_path or "",
                }
                for s in servers
            ]
        }


@router.put("/servers/{server_id}")
async def update_server_config(
    server_id: int,
    req: ServerConfigUpdate,
    _: dict = Depends(verify_auth),
):
    async with async_session() as db:
        server = (await db.execute(
            select(Server).where(Server.id == server_id)
        )).scalar_one_or_none()
        if not server:
            raise HTTPException(status_code=404)

        if req.wildcard_ssl_enabled is not None:
            server.wildcard_ssl_enabled = req.wildcard_ssl_enabled
        if req.wildcard_ssl_deploy_path is not None:
            server.wildcard_ssl_deploy_path = req.wildcard_ssl_deploy_path
        if req.wildcard_ssl_reload_cmd is not None:
            server.wildcard_ssl_reload_cmd = req.wildcard_ssl_reload_cmd
        if req.wildcard_ssl_fullchain_name is not None:
            server.wildcard_ssl_fullchain_name = req.wildcard_ssl_fullchain_name
        if req.wildcard_ssl_privkey_name is not None:
            server.wildcard_ssl_privkey_name = req.wildcard_ssl_privkey_name
        if req.wildcard_ssl_custom_path_enabled is not None:
            server.wildcard_ssl_custom_path_enabled = req.wildcard_ssl_custom_path_enabled
        if req.wildcard_ssl_custom_fullchain_path is not None:
            server.wildcard_ssl_custom_fullchain_path = req.wildcard_ssl_custom_fullchain_path
        if req.wildcard_ssl_custom_privkey_path is not None:
            server.wildcard_ssl_custom_privkey_path = req.wildcard_ssl_custom_privkey_path

        await db.commit()
    return {"success": True}


# ─── Helpers ───

def _cert_to_dict(cert: WildcardCertificate) -> dict:
    from datetime import datetime, timezone
    days_left = None
    expired = False
    if cert.expiry_date:
        now = datetime.now(timezone.utc)
        expiry = cert.expiry_date if cert.expiry_date.tzinfo else cert.expiry_date.replace(tzinfo=timezone.utc)
        days_left = (expiry - now).days
        expired = days_left <= 0
    return {
        "id": cert.id,
        "domain": cert.domain,
        "base_domain": cert.base_domain,
        "expiry_date": cert.expiry_date.isoformat() if cert.expiry_date else None,
        "days_left": days_left,
        "expired": expired,
        "issued_at": cert.issued_at.isoformat() if cert.issued_at else None,
        "last_renewed": cert.last_renewed.isoformat() if cert.last_renewed else None,
        "auto_renew": cert.auto_renew,
    }


async def _get_setting(db, key: str) -> Optional[str]:
    row = (await db.execute(
        select(PanelSettings).where(PanelSettings.key == key)
    )).scalar_one_or_none()
    return row.value if row else None


async def _set_setting(db, key: str, value: str):
    row = (await db.execute(
        select(PanelSettings).where(PanelSettings.key == key)
    )).scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(PanelSettings(key=key, value=value))
