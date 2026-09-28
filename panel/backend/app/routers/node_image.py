"""Доставка образа ноды с панели по SSH (ноды под ТСПУ) + метод обновления и креды.

Метод обновления ноды и SSH-креды доставки живут на записи сервера; креды
шифруются (EncryptedString) и наружу не отдаются — только флаг «заданы».
Сама доставка — фоновая задача с NDJSON-стримом лога (как авторазвёртывание):
окно лога можно закрыть, список задач (GET /servers/deliver-image/jobs) даёт
фронту статусы по серверам. Массовый запуск — POST /servers/deliver-image/bulk.
"""
import json
import logging
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import Server
from app.services import update_channel
from app.services.node_image_delivery import SSHTarget, get_image_delivery_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/servers", tags=["node-image"])

MAX_BULK_SERVERS = 1000


def _ndjson(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


def _host_from_url(url: str) -> str:
    return urlparse(url).hostname or ""


def _target_tag() -> str:
    """Тег образа по текущему каналу обновлений: dev → :dev, иначе :latest."""
    return "dev" if update_channel.current_branch() == update_channel.DEV_BRANCH else "latest"


class SSHCreds(BaseModel):
    ssh_port: Optional[int] = None
    ssh_user: Optional[str] = None
    ssh_password: Optional[str] = None
    ssh_private_key: Optional[str] = None
    ssh_passphrase: Optional[str] = None


class DeliverImageRequest(SSHCreds):
    """Разовые SSH-креды, если у сервера не сохранены."""
    ssh_host: Optional[str] = None


class ImageDeliverySettings(DeliverImageRequest):
    image_delivery: Optional[str] = None  # auto | ssh


class BulkDeliverRequest(SSHCreds):
    """Креды из запроса идут только серверам без сохранённых — у остальных свои."""
    server_ids: list[int] = Field(min_length=1, max_length=MAX_BULK_SERVERS)
    save_creds: bool = False


class DeliveryTargetError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _has_stored_creds(server: Server) -> bool:
    return bool(server.ssh_password or server.ssh_private_key)


def _resolve_target(server: Server, req: DeliverImageRequest) -> SSHTarget:
    """SSH-цель доставки: поля запроса поверх сохранённых у сервера."""
    host = (req.ssh_host or server.ssh_host or _host_from_url(server.url)).strip()
    port = req.ssh_port or server.ssh_port or 22
    user = (req.ssh_user or server.ssh_user or "root").strip()
    password = req.ssh_password if req.ssh_password is not None else server.ssh_password
    private_key = req.ssh_private_key if req.ssh_private_key is not None else server.ssh_private_key
    passphrase = req.ssh_passphrase if req.ssh_passphrase is not None else server.ssh_passphrase

    if not host:
        raise DeliveryTargetError("no_host", "Не удалось определить SSH-хост ноды")
    if user != "root":
        raise DeliveryTargetError("not_root", "Доставка образа поддерживает только root-доступ по SSH")
    if not password and not private_key:
        raise DeliveryTargetError("no_creds", "Нет SSH-кредов: сохраните их у сервера или укажите в запросе")

    return SSHTarget(
        host=host, port=port, user=user,
        password=password, private_key=private_key, passphrase=passphrase,
    )


def _store_creds(server: Server, req: DeliverImageRequest) -> None:
    """Write-only: обновляем только переданные поля; пустая строка — очистить."""
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

    _store_creds(server, req)

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
    try:
        target = _resolve_target(server, req)
    except DeliveryTargetError as exc:
        raise HTTPException(400, str(exc)) from exc

    job_id = get_image_delivery_manager().start(server.id, server.name, target, _target_tag())
    return {"job_id": job_id}


@router.post("/deliver-image/bulk")
async def deliver_image_bulk(
    req: BulkDeliverRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Запустить доставку на несколько серверов. Серверы без кредов пропускаются с причиной."""
    servers = (await db.execute(
        select(Server).where(Server.id.in_(req.server_ids)).order_by(Server.position)
    )).scalars().all()
    found_ids = {s.id for s in servers}

    fallback = DeliverImageRequest(**req.model_dump(include=set(SSHCreds.model_fields)))
    targets: list[tuple[Server, SSHTarget]] = []
    skipped = [
        {"server_id": sid, "name": None, "reason": "not_found"}
        for sid in req.server_ids if sid not in found_ids
    ]
    for server in servers:
        uses_fallback = not _has_stored_creds(server)
        try:
            target = _resolve_target(server, fallback if uses_fallback else DeliverImageRequest())
        except DeliveryTargetError as exc:
            skipped.append({"server_id": server.id, "name": server.name, "reason": exc.reason})
            continue
        if uses_fallback and req.save_creds:
            _store_creds(server, fallback)
        targets.append((server, target))

    if req.save_creds:
        await db.commit()

    tag = _target_tag()
    manager = get_image_delivery_manager()
    started = [
        {"server_id": server.id, "job_id": manager.start(server.id, server.name, target, tag)}
        for server, target in targets
    ]
    logger.info("Bulk image delivery: started=%d skipped=%d", len(started), len(skipped))
    return {"started": started, "skipped": skipped}


@router.get("/deliver-image/jobs")
async def list_delivery_jobs(_: dict = Depends(verify_auth)):
    """Идущие и недавно завершённые доставки — статусы на странице обновлений."""
    return {"jobs": get_image_delivery_manager().list_jobs()}


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
