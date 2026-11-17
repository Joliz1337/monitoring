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

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import Server
from app.services import ssh_access, update_channel
from app.services.node_image_delivery import get_image_delivery_manager
from app.services.ssh_access import AccessChange, store_server_access
from app.services.ssh_target import (
    AccessLevel,
    SSHTarget,
    SSHTargetError,
    has_stored_creds,
    host_from_url,
    login_level,
    resolve_ssh_target,
    saved_access,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/servers", tags=["node-image"])

MAX_BULK_SERVERS = 1000


def _ndjson(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


class SSHCreds(BaseModel):
    ssh_port: Optional[int] = None
    ssh_user: Optional[str] = None
    ssh_password: Optional[str] = None
    ssh_private_key: Optional[str] = None
    ssh_passphrase: Optional[str] = None

    def to_change(self) -> AccessChange:
        return AccessChange(
            user=self.ssh_user,
            port=self.ssh_port,
            password=self.ssh_password,
            private_key=self.ssh_private_key,
            passphrase=self.ssh_passphrase,
        )


class SSHAccessRequest(SSHCreds):
    """Разовые SSH-креды поверх доступа сервера (свой, папки или общий). Ими же
    ставится нода Remnawave, когда сервер качает всё через панель."""
    ssh_host: Optional[str] = None


class ImageDeliverySettings(SSHAccessRequest):
    image_delivery: Optional[str] = None  # auto | ssh


class BulkDeliverRequest(SSHCreds):
    """Креды из запроса идут только серверам без входа на любом уровне — у остальных свой доступ."""
    server_ids: list[int] = Field(min_length=1, max_length=MAX_BULK_SERVERS)
    save_creds: bool = False


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
    saved = saved_access(server, await ssh_access.load_rules(db))
    return {
        "image_delivery": server.image_delivery or "auto",
        "ssh_host": server.ssh_host or host_from_url(server.url),
        "ssh_port": saved.port,
        "ssh_user": saved.user,
        # откуда берётся вход: server | folder | global; None — входа нет нигде
        "ssh_access_level": saved.login_level.value if saved.login_level else None,
        "ssh_access_folder": server.folder if saved.login_level is AccessLevel.FOLDER else None,
        # секреты не отдаём — только факт наличия своего входа у сервера
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

    store_server_access(server, req.to_change(), ssh_host=req.ssh_host)

    await db.commit()
    return {"success": True}


@router.post("/{server_id}/deliver-image")
async def deliver_image_to_server(
    server_id: int,
    req: SSHAccessRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Доставить образ ноды по SSH и обновить её. Возвращает job_id — лог в стриме."""
    server = await _get_server(server_id, db)
    try:
        target = resolve_ssh_target(server, await ssh_access.load_rules(db), **req.model_dump())
    except SSHTargetError as exc:
        raise HTTPException(400, str(exc)) from exc

    job_id = get_image_delivery_manager().start(server.id, server.name, target, update_channel.current_image_tag())
    return {"job_id": job_id}


@router.post("/deliver-image/bulk")
async def deliver_image_bulk(
    req: BulkDeliverRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Запустить доставку на несколько серверов. Серверы без доступа пропускаются с причиной."""
    servers = (await db.execute(
        select(Server).where(Server.id.in_(req.server_ids)).order_by(Server.position)
    )).scalars().all()
    found_ids = {s.id for s in servers}
    rules = await ssh_access.load_rules(db)

    fallback = SSHAccessRequest(**req.model_dump(include=set(SSHCreds.model_fields)))
    targets: list[tuple[Server, SSHTarget]] = []
    skipped = [
        {"server_id": sid, "name": None, "reason": "not_found"}
        for sid in req.server_ids if sid not in found_ids
    ]
    for server in servers:
        uses_fallback = login_level(rules, server.folder, has_stored_creds(server)) is None
        try:
            target = resolve_ssh_target(server, rules, **(fallback.model_dump() if uses_fallback else {}))
        except SSHTargetError as exc:
            skipped.append({"server_id": server.id, "name": server.name, "reason": exc.reason})
            continue
        if uses_fallback and req.save_creds:
            store_server_access(server, fallback.to_change())
            # Сохранение доступа сбросило закреплённый ключ хоста — цель без него
            target = resolve_ssh_target(server, rules)
        targets.append((server, target))

    if req.save_creds:
        await db.commit()

    tag = update_channel.current_image_tag()
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
