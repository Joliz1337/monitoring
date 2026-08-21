"""Доставка образа ноды с панели по SSH (ноды под ТСПУ) + метод обновления и креды.

Метод обновления ноды и SSH-креды доставки живут на записи сервера; креды
шифруются (EncryptedString) и наружу не отдаются — только флаг «заданы».
Сама доставка — фоновая задача с NDJSON-стримом лога (как авторазвёртывание).
"""
import json
import logging
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import Server
from app.services import update_channel
from app.services.node_image_delivery import SSHTarget, get_image_delivery_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/servers", tags=["node-image"])


def _ndjson(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


def _host_from_url(url: str) -> str:
    return urlparse(url).hostname or ""


def _target_tag_ref() -> tuple[str, str]:
    """Тег образа и git-ref по текущему каналу обновлений: dev → :dev, иначе :latest."""
    branch = update_channel.current_branch()
    tag = "dev" if branch == update_channel.DEV_BRANCH else "latest"
    return tag, branch


class ImageDeliverySettings(BaseModel):
    image_delivery: Optional[str] = None  # auto | ssh
    ssh_host: Optional[str] = None
    ssh_port: Optional[int] = None
    ssh_user: Optional[str] = None
    ssh_password: Optional[str] = None
    ssh_private_key: Optional[str] = None
    ssh_passphrase: Optional[str] = None


class DeliverImageRequest(BaseModel):
    """Разовые SSH-креды, если у сервера не сохранены."""
    ssh_host: Optional[str] = None
    ssh_port: Optional[int] = None
    ssh_user: Optional[str] = None
    ssh_password: Optional[str] = None
    ssh_private_key: Optional[str] = None
    ssh_passphrase: Optional[str] = None


async def _get_server(server_id: int, db: AsyncSession) -> Server:
    server = (await db.execute(select(Server).where(Server.id == server_id))).scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Сервер не найден")
    return server


@router.get("/{server_id}/image-delivery")
async def get_image_delivery(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)
    return {
        "image_delivery": server.image_delivery or "auto",
        "ssh_host": server.ssh_host or _host_from_url(server.url),
        "ssh_port": server.ssh_port or 22,
        "ssh_user": server.ssh_user or "root",
        # секреты не отдаём — только факт наличия
        "has_ssh_password": bool(server.ssh_password),
        "has_ssh_private_key": bool(server.ssh_private_key),
    }


@router.patch("/{server_id}/image-delivery")
async def set_image_delivery(
    server_id: int,
    req: ImageDeliverySettings,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    server = await _get_server(server_id, db)

    if req.image_delivery is not None:
        if req.image_delivery not in ("auto", "ssh"):
            raise HTTPException(400, "image_delivery: auto | ssh")
        server.image_delivery = req.image_delivery

    # Write-only: обновляем только переданные поля; пустая строка — очистить
    if req.ssh_host is not None:
        server.ssh_host = req.ssh_host.strip() or None
    if req.ssh_port is not None:
        server.ssh_port = req.ssh_port or None
    if req.ssh_user is not None:
        server.ssh_user = req.ssh_user.strip() or None
    if req.ssh_password is not None:
        server.ssh_password = req.ssh_password or None
    if req.ssh_private_key is not None:
        server.ssh_private_key = req.ssh_private_key or None
    if req.ssh_passphrase is not None:
        server.ssh_passphrase = req.ssh_passphrase or None

    await db.commit()
    return {"success": True}


@router.post("/{server_id}/deliver-image")
async def deliver_image_to_server(
    server_id: int,
    req: DeliverImageRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Доставить образ ноды по SSH и обновить её. Возвращает job_id — лог в стриме."""
    server = await _get_server(server_id, db)

    host = (req.ssh_host or server.ssh_host or _host_from_url(server.url)).strip()
    port = req.ssh_port or server.ssh_port or 22
    user = (req.ssh_user or server.ssh_user or "root").strip()
    password = req.ssh_password if req.ssh_password is not None else server.ssh_password
    private_key = req.ssh_private_key if req.ssh_private_key is not None else server.ssh_private_key
    passphrase = req.ssh_passphrase if req.ssh_passphrase is not None else server.ssh_passphrase

    if not host:
        raise HTTPException(400, "Не удалось определить SSH-хост ноды")
    if user != "root":
        raise HTTPException(400, "Доставка образа поддерживает только root-доступ по SSH")
    if not password and not private_key:
        raise HTTPException(400, "Нет SSH-кредов: сохраните их у сервера или укажите в запросе")

    tag, _ = _target_tag_ref()
    target = SSHTarget(
        host=host, port=port, user=user,
        password=password, private_key=private_key, passphrase=passphrase,
    )
    job_id = get_image_delivery_manager().start(server.name, target, tag)
    return {"job_id": job_id}


@router.get("/deliver-image/{job_id}/stream")
async def stream_delivery(job_id: str, _: dict = Depends(verify_auth)):
    """NDJSON-стрим лога доставки. Переподключаемый."""
    manager = get_image_delivery_manager()
    if manager.get(job_id) is None:
        raise HTTPException(404, "Задача доставки не найдена")

    async def generate():
        async for event in manager.subscribe(job_id):
            yield _ndjson(event)

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
