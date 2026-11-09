"""Порты HAProxy и DNAT по серверам — интерфейс предупреждает, когда DNAT перехватывает порт HAProxy."""

from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.services.port_usage import load_port_usage

router = APIRouter(prefix="/port-usage", tags=["port-usage"], dependencies=[Depends(verify_auth)])


@router.get("")
async def get_port_usage(server_id: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    usage = await load_port_usage(db, server_id)
    return {sid: asdict(item) for sid, item in usage.items()}
