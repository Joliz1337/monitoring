"""Страница «Потери»: сводка по адресам назначения релеев и ручная проверка адреса."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import Server
from app.services.loss_overview import TargetParseError, build_overview, check_from_servers, parse_target
from app.services.loss_registry import get_loss_registry
from app.services.server_alerter import get_server_alerter

router = APIRouter(prefix="/loss", tags=["loss"], dependencies=[Depends(verify_auth)])

MAX_CHECK_SERVERS = 500


class LossCheckRequest(BaseModel):
    target: str = Field(..., min_length=1, max_length=64)
    server_ids: list[int] = Field(..., min_length=1, max_length=MAX_CHECK_SERVERS)


@router.get("/overview")
async def get_overview():
    registry = get_loss_registry()
    return {
        "targets": build_overview(registry.fresh(), registry.owners(), get_server_alerter().loss_episodes()),
    }


@router.post("/check")
async def check_target(data: LossCheckRequest, db: AsyncSession = Depends(get_db)):
    try:
        ip, port = parse_target(data.target)
    except TargetParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result = await db.execute(
        select(Server).where(Server.id.in_(data.server_ids), Server.is_active == True)  # noqa: E712
    )
    servers = list(result.scalars().all())
    # Сессию отпускаем до похода по нодам: медленная нода не держит коннект пула
    await db.commit()
    return {"ip": ip, "port": port, "results": await check_from_servers(servers, ip, port)}
