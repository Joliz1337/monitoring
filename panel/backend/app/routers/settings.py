import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional


from app.database import get_db
from app.models import PanelSettings, AlertSettings
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
    # Speedtest settings
    "speedtest_enabled": "true",
    "speedtest_mode": "both",
    "speedtest_threshold": "500",
    "speedtest_interval": "60",
    "speedtest_duration": "3",
    "speedtest_streams": "3",
    "speedtest_test_mode": "quick",
    "speedtest_panel_port": "5201",
    "speedtest_panel_address": "",
    # Time synchronization
    "server_timezone": "Europe/Moscow",
    "time_sync_enabled": "true",
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

    if key == "server_timezone":
        from app.services.time_sync import get_time_sync_service
        asyncio.ensure_future(get_time_sync_service().sync_all_servers(data.value))

    return {"success": True, "key": key, "value": data.value}


class SpeedtestTestNotification(BaseModel):
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None


@router.post("/speedtest/test-notification")
async def speedtest_test_notification(
    body: SpeedtestTestNotification,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    bot_token = body.bot_token
    chat_id = body.chat_id

    if not bot_token or not chat_id:
        result = await db.execute(select(AlertSettings).limit(1))
        alert_s = result.scalar_one_or_none()
        if alert_s:
            bot_token = bot_token or alert_s.telegram_bot_token
            chat_id = chat_id or alert_s.telegram_chat_id

    if not bot_token or not chat_id:
        raise HTTPException(400, "No bot token/chat ID configured")

    from app.services.telegram_bot import get_telegram_bot_service
    text = "✅ <b>Speed Test</b>\n\nТестовое уведомление — конфигурация работает!"
    return await get_telegram_bot_service().send_test(bot_token, chat_id, text)


# ==================== Time Synchronization ====================


@router.post("/time-sync/run")
async def time_sync_run(
    _: dict = Depends(verify_auth),
):
    from app.services.time_sync import get_time_sync_service
    service = get_time_sync_service()

    if service.get_status()["sync_in_progress"]:
        raise HTTPException(409, "Sync already in progress")

    asyncio.ensure_future(service.sync_all_servers())
    return {"success": True, "message": "Sync started"}


@router.get("/time-sync/status")
async def time_sync_status(
    _: dict = Depends(verify_auth),
):
    from app.services.time_sync import get_time_sync_service
    return get_time_sync_service().get_status()
