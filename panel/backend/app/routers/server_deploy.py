"""Авторазвёртывание ноды по SSH и хранилище сертификатов Remnawave.

POST /servers/deploy — запускает фоновую задачу установки ноды (+опции) и
возвращает её job_id. Сама установка идёт независимо от HTTP-соединения: лог
читается через GET /servers/deploy/{job_id}/stream (NDJSON), список активных
и недавних задач — через GET /servers/deploy/jobs.
"""
import ipaddress
import json
import logging
import socket

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from typing import Optional

from app.auth import verify_auth
from app.config import get_settings
from app.database import async_session_maker, get_db
from app.models import RemnawaveCertProfile
from app.services.deploy_job_manager import PostDeployOptions, get_deploy_job_manager
from app.services.deploy_service import DeployParams
from app.services.pki import build_installer_token
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/servers", tags=["deploy"])


def _resolve_panel_ip() -> Optional[str]:
    domain = get_settings().domain
    if not domain:
        return None
    try:
        return socket.gethostbyname(domain)
    except socket.gaierror:
        return None


def _validate_host(raw: str) -> str:
    """Хост ноды: непустой, без управляющих символов, не loopback/link-local."""
    host = raw.strip()
    if not host:
        raise HTTPException(400, "Host is empty")
    if any(ord(c) < 0x20 or ord(c) > 0x7E for c in host):
        raise HTTPException(400, "Host contains invalid characters")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise HTTPException(400, f"Disallowed host: {host}")
    return host


def _ndjson(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


# ==================== Профили сертификатов Remnawave ====================


class CertProfileCreate(BaseModel):
    name: str
    secret_key: str


@router.get("/remnawave-certs")
async def list_remnawave_certs(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Список сохранённых сертификатов — без самих секретов."""
    result = await db.execute(
        select(RemnawaveCertProfile).order_by(RemnawaveCertProfile.name)
    )
    profiles = result.scalars().all()
    return {
        "profiles": [
            {
                "id": p.id,
                "name": p.name,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in profiles
        ]
    }


@router.post("/remnawave-certs")
async def create_remnawave_cert(
    data: CertProfileCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    name = data.name.strip()
    secret = data.secret_key.strip()
    if not name or not secret:
        raise HTTPException(400, "Name and secret_key are required")

    profile = RemnawaveCertProfile(name=name, secret_key=secret)
    db.add(profile)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Профиль с таким именем уже существует")
    await db.refresh(profile)
    return {"success": True, "id": profile.id, "name": profile.name}


@router.delete("/remnawave-certs/{profile_id}")
async def delete_remnawave_cert(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    result = await db.execute(
        select(RemnawaveCertProfile).where(RemnawaveCertProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(404, "Profile not found")
    await db.delete(profile)
    await db.commit()
    return {"success": True}


# ==================== Авторазвёртывание ноды ====================


class DeployRequest(BaseModel):
    name: str
    host: str
    monitoring_port: int = 9100
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_password: Optional[str] = None
    ssh_private_key: Optional[str] = None
    ssh_key_passphrase: Optional[str] = None
    install_warp: bool = False
    install_optimizations: bool = False
    opt_profile: str = "vpn"
    nic_mode: str = "auto"  # auto | multiqueue | hybrid | rps
    install_remnawave: bool = False
    remnawave_cert_profile_id: Optional[int] = None
    remnawave_cert_inline: Optional[str] = None
    save_remnawave_cert: bool = False
    save_remnawave_cert_name: Optional[str] = None
    install_proxy: bool = False
    proxy_url: Optional[str] = None
    ssh_preset: Optional[str] = None  # None | "recommended" | "maximum"
    new_root_password: Optional[str] = None
    haproxy_profile_id: Optional[int] = None
    firewall_profile_id: Optional[int] = None


async def _resolve_remnawave_cert(req: DeployRequest) -> str:
    """Достаёт сертификат Remnawave из inline-поля или сохранённого профиля,
    при необходимости сохраняет новый профиль."""
    cert = (req.remnawave_cert_inline or "").strip()

    if not cert and req.remnawave_cert_profile_id is not None:
        async with async_session_maker() as db:
            result = await db.execute(
                select(RemnawaveCertProfile).where(
                    RemnawaveCertProfile.id == req.remnawave_cert_profile_id
                )
            )
            profile = result.scalar_one_or_none()
        if not profile:
            raise HTTPException(404, "Сохранённый сертификат не найден")
        cert = profile.secret_key

    if not cert:
        raise HTTPException(400, "Не указан сертификат ноды Remnawave")

    if req.save_remnawave_cert and req.remnawave_cert_inline:
        name = (req.save_remnawave_cert_name or "").strip()
        if not name:
            raise HTTPException(400, "Не указано имя для сохранения сертификата")
        async with async_session_maker() as db:
            db.add(RemnawaveCertProfile(name=name, secret_key=cert))
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                raise HTTPException(409, "Профиль с таким именем уже существует")

    return cert


@router.post("/deploy")
async def deploy_server(
    req: DeployRequest,
    request: Request,
    _: dict = Depends(verify_auth),
):
    """Запустить фоновую установку ноды на удалённом сервере по SSH.

    Возвращает job_id — лог читается отдельным запросом к /deploy/{job_id}/stream.
    """
    host = _validate_host(req.host)

    if not req.ssh_password and not req.ssh_private_key:
        raise HTTPException(400, "Укажите пароль или приватный SSH-ключ")

    remnawave_cert: Optional[str] = None
    if req.install_remnawave:
        remnawave_cert = await _resolve_remnawave_cert(req)

    proxy_url: Optional[str] = None
    if req.install_proxy:
        proxy_url = (req.proxy_url or "").strip()
        if not proxy_url:
            raise HTTPException(400, "Не указан адрес прокси")

    if req.ssh_preset and req.ssh_preset not in ("recommended", "maximum"):
        raise HTTPException(400, "Некорректный SSH-пресет")
    if req.new_root_password is not None and len(req.new_root_password) < 8:
        raise HTTPException(400, "Пароль root: минимум 8 символов")

    panel_ip = _resolve_panel_ip()
    node_secret = build_installer_token(request.app.state.pki, panel_ip=panel_ip)
    server_url = f"https://{host}:{req.monitoring_port}"

    params = DeployParams(
        host=host,
        ssh_port=req.ssh_port,
        ssh_user=req.ssh_user.strip() or "root",
        node_secret=node_secret,
        panel_ip=panel_ip,
        ssh_password=req.ssh_password,
        ssh_private_key=req.ssh_private_key,
        ssh_key_passphrase=req.ssh_key_passphrase,
        install_warp=req.install_warp,
        install_optimizations=req.install_optimizations,
        opt_profile="panel" if req.opt_profile == "panel" else "vpn",
        nic_mode=req.nic_mode if req.nic_mode in ("multiqueue", "hybrid", "rps") else "auto",
        install_remnawave=req.install_remnawave,
        remnawave_cert=remnawave_cert,
        proxy_url=proxy_url,
    )

    post_opts = PostDeployOptions(
        ssh_preset=req.ssh_preset,
        new_root_password=req.new_root_password,
        haproxy_profile_id=req.haproxy_profile_id,
        firewall_profile_id=req.firewall_profile_id,
    )

    job_id = get_deploy_job_manager().start(params, req.name, server_url, post_opts)
    return {"job_id": job_id}


@router.get("/deploy/jobs")
async def list_deploy_jobs(_: dict = Depends(verify_auth)):
    """Активные и недавно завершённые задачи установки — для восстановления UI."""
    return {"jobs": get_deploy_job_manager().list_jobs()}


@router.get("/deploy/{job_id}/stream")
async def stream_deploy_job(job_id: str, _: dict = Depends(verify_auth)):
    """NDJSON-стрим лога задачи установки. Переподключаемый."""
    manager = get_deploy_job_manager()
    if manager.get(job_id) is None:
        raise HTTPException(404, "Задача установки не найдена")

    async def generate():
        async for event in manager.subscribe(job_id):
            yield _ndjson(event)

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
