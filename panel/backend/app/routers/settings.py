from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models import PanelSettings
from app.auth import verify_auth

router = APIRouter(prefix="/settings", tags=["settings"])

DEFAULT_SETTINGS = {
    "refresh_interval": "5",
    "theme": "dark",
    "compact_view": "false",
    "blocklist_temp_timeout": "600",
    "blocklist_auto_update_enabled": "true",
    "blocklist_auto_update_interval": "86400",
    # Collector intervals (in seconds)
    "metrics_collect_interval": "10",  # Recommended: 10-15s
    "haproxy_collect_interval": "60",  # Recommended: 60s
}


class SettingUpdate(BaseModel):
    value: str


async def get_setting(key: str, db: AsyncSession) -> Optional[str]:
    result = await db.execute(
        select(PanelSettings).where(PanelSettings.key == key)
    )
    setting = result.scalar_one_or_none()
    if setting:
        return setting.value
    return DEFAULT_SETTINGS.get(key)


async def set_setting(key: str, value: str, db: AsyncSession):
    result = await db.execute(
        select(PanelSettings).where(PanelSettings.key == key)
    )
    setting = result.scalar_one_or_none()
    
    if setting:
        setting.value = value
    else:
        setting = PanelSettings(key=key, value=value)
        db.add(setting)
    
    await db.commit()


@router.get("")
async def get_all_settings(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    result = await db.execute(select(PanelSettings))
    db_settings = {s.key: s.value for s in result.scalars().all()}
    
    settings = {**DEFAULT_SETTINGS, **db_settings}
    return {"settings": settings}


@router.get("/{key}")
async def get_single_setting(
    key: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    value = await get_setting(key, db)
    return {"key": key, "value": value}


@router.put("/{key}")
async def update_setting(
    key: str,
    data: SettingUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    await set_setting(key, data.value, db)
    return {"success": True, "key": key, "value": data.value}
