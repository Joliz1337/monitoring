from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

import aiohttp

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

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        text = "✅ <b>Speed Test</b>\n\nТестовое уведомление — конфигурация работает!"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            }) as resp:
                if resp.status != 200:
                    body_text = await resp.text()
                    return {"success": False, "error": body_text[:200]}
                return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
