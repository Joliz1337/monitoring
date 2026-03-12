import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    XrayMonitorSettings, XrayMonitorSubscription,
    XrayMonitorServer, XrayMonitorCheck,
)
from app.auth import verify_auth
from app.services.xray_monitor import get_xray_monitor_service
from app.services.xray_key_parser import fetch_subscription, parse_keys, is_valid_server, is_ignored_address

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xray-monitor", tags=["xray-monitor"])


# ========================= Pydantic models =========================

class SettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    check_interval: Optional[int] = None
    latency_threshold_ms: Optional[int] = None
    fail_threshold: Optional[int] = None
    use_custom_bot: Optional[bool] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    notify_down: Optional[bool] = None
    notify_recovery: Optional[bool] = None
    notify_latency: Optional[bool] = None
    ignore_list: Optional[list[str]] = None


class SubscriptionCreate(BaseModel):
    name: str
    url: str


class SubscriptionUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    enabled: Optional[bool] = None
    auto_refresh: Optional[bool] = None


class AddKeysRequest(BaseModel):
    keys: str


class TestNotificationRequest(BaseModel):
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None


# ========================= Settings =========================

@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    result = await db.execute(select(XrayMonitorSettings).limit(1))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = XrayMonitorSettings()
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    try:
        ignore = json.loads(settings.ignore_list or "[]")
    except (json.JSONDecodeError, TypeError):
        ignore = []

    return {
        "enabled": settings.enabled,
        "check_interval": settings.check_interval,
        "latency_threshold_ms": settings.latency_threshold_ms,
        "fail_threshold": settings.fail_threshold,
        "use_custom_bot": settings.use_custom_bot,
        "telegram_bot_token": settings.telegram_bot_token or "",
        "telegram_chat_id": settings.telegram_chat_id or "",
        "notify_down": settings.notify_down,
        "notify_recovery": settings.notify_recovery,
        "notify_latency": settings.notify_latency,
        "ignore_list": ignore,
    }


@router.put("/settings")
async def update_settings(
    body: SettingsUpdate,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    result = await db.execute(select(XrayMonitorSettings).limit(1))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = XrayMonitorSettings()
        db.add(settings)
        await db.flush()

    data = body.model_dump(exclude_unset=True)
    if "ignore_list" in data:
        raw = data.pop("ignore_list")
        cleaned = sorted({v.strip().lower() for v in (raw or []) if v.strip()})
        settings.ignore_list = json.dumps(cleaned, ensure_ascii=False)
    for field, value in data.items():
        setattr(settings, field, value)
    await db.commit()
    return {"success": True}


# ========================= Subscriptions =========================

def _serialize_server(s: XrayMonitorServer) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "protocol": s.protocol,
        "address": s.address,
        "port": s.port,
        "enabled": s.enabled,
        "status": s.status,
        "last_ping_ms": s.last_ping_ms,
        "last_check": s.last_check.isoformat() if s.last_check else None,
        "fail_count": s.fail_count,
        "position": s.position,
    }


@router.get("/subscriptions")
async def list_subscriptions(db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    result = await db.execute(select(XrayMonitorSubscription).order_by(XrayMonitorSubscription.id))
    subs = result.scalars().all()

    sub_ids = [s.id for s in subs]
    servers_by_sub: dict[int, list] = {sid: [] for sid in sub_ids}

    if sub_ids:
        srv_result = await db.execute(
            select(XrayMonitorServer)
            .where(XrayMonitorServer.subscription_id.in_(sub_ids))
            .order_by(XrayMonitorServer.position, XrayMonitorServer.id)
        )
        for srv in srv_result.scalars().all():
            if srv.subscription_id in servers_by_sub:
                servers_by_sub[srv.subscription_id].append(_serialize_server(srv))

    return [
        {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "enabled": s.enabled,
            "auto_refresh": s.auto_refresh,
            "last_refreshed": s.last_refreshed.isoformat() if s.last_refreshed else None,
            "last_error": s.last_error,
            "server_count": s.server_count,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "servers": servers_by_sub.get(s.id, []),
        }
        for s in subs
    ]


@router.post("/subscriptions")
async def add_subscription(
    body: SubscriptionCreate,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    sub = XrayMonitorSubscription(name=body.name, url=body.url)
    db.add(sub)
    await db.commit()
    await db.refresh(sub)

    count = 0
    error = None
    try:
        keys = await fetch_subscription(body.url)
        count = await _save_parsed_keys(db, keys, sub.id)
        sub.server_count = count
        sub.last_refreshed = datetime.now(timezone.utc).replace(tzinfo=None)
    except Exception as e:
        error = str(e)[:500]
        sub.last_error = error
    await db.commit()

    svc = get_xray_monitor_service()
    svc.mark_config_dirty()

    return {"id": sub.id, "server_count": count, "error": error}


@router.put("/subscriptions/{sub_id}")
async def update_subscription(
    sub_id: int,
    body: SubscriptionUpdate,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    result = await db.execute(select(XrayMonitorSubscription).where(XrayMonitorSubscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "Subscription not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(sub, field, value)
    await db.commit()
    return {"success": True}


@router.delete("/subscriptions/{sub_id}")
async def delete_subscription(
    sub_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    await db.execute(delete(XrayMonitorServer).where(XrayMonitorServer.subscription_id == sub_id))
    await db.execute(delete(XrayMonitorSubscription).where(XrayMonitorSubscription.id == sub_id))
    await db.commit()

    svc = get_xray_monitor_service()
    svc.mark_config_dirty()
    return {"success": True}


@router.post("/subscriptions/{sub_id}/refresh")
async def refresh_subscription(
    sub_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    result = await db.execute(select(XrayMonitorSubscription).where(XrayMonitorSubscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "Subscription not found")

    await db.execute(delete(XrayMonitorServer).where(XrayMonitorServer.subscription_id == sub_id))
    await db.commit()

    count = 0
    error = None
    try:
        keys = await fetch_subscription(sub.url)
        count = await _save_parsed_keys(db, keys, sub.id)
        sub.server_count = count
        sub.last_refreshed = datetime.now(timezone.utc).replace(tzinfo=None)
        sub.last_error = None
    except Exception as e:
        error = str(e)[:500]
        sub.last_error = error
    await db.commit()

    svc = get_xray_monitor_service()
    svc.mark_config_dirty()

    return {"server_count": count, "error": error}


# ========================= Servers (manual only) =========================

@router.get("/servers")
async def list_servers(db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    """Only manual servers (subscription_id IS NULL)."""
    result = await db.execute(
        select(XrayMonitorServer)
        .where(XrayMonitorServer.subscription_id.is_(None))
        .order_by(XrayMonitorServer.position, XrayMonitorServer.id)
    )
    servers = result.scalars().all()
    return [_serialize_server(s) for s in servers]


@router.post("/servers")
async def add_keys(
    body: AddKeysRequest,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    parsed = parse_keys(body.keys)
    if not parsed:
        raise HTTPException(400, "No valid keys found")

    count = await _save_parsed_keys(db, parsed, subscription_id=None)

    svc = get_xray_monitor_service()
    svc.mark_config_dirty()

    return {"added": count}


@router.delete("/servers/{server_id}")
async def delete_server(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    await db.execute(delete(XrayMonitorCheck).where(XrayMonitorCheck.server_id == server_id))
    await db.execute(delete(XrayMonitorServer).where(XrayMonitorServer.id == server_id))
    await db.commit()

    svc = get_xray_monitor_service()
    svc.mark_config_dirty()
    return {"success": True}


@router.get("/servers/{server_id}/history")
async def server_history(
    server_id: int,
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    result = await db.execute(
        select(XrayMonitorCheck)
        .where(XrayMonitorCheck.server_id == server_id)
        .order_by(desc(XrayMonitorCheck.timestamp))
        .limit(limit)
    )
    checks = result.scalars().all()
    return [
        {
            "id": c.id,
            "timestamp": c.timestamp.isoformat() if c.timestamp else None,
            "status": c.status,
            "ping_ms": c.ping_ms,
            "error": c.error,
        }
        for c in checks
    ]


# ========================= Status / Notifications =========================

@router.get("/status")
async def service_status(_=Depends(verify_auth)):
    svc = get_xray_monitor_service()
    return svc.get_status()


@router.post("/test-notification")
async def test_notification(
    body: TestNotificationRequest,
    _=Depends(verify_auth),
):
    import aiohttp

    bot_token = body.bot_token
    chat_id = body.chat_id

    if not bot_token or not chat_id:
        from app.database import async_session
        from app.models import AlertSettings
        async with async_session() as db:
            result = await db.execute(select(AlertSettings).limit(1))
            alert_s = result.scalar_one_or_none()
        if alert_s:
            bot_token = bot_token or alert_s.telegram_bot_token
            chat_id = chat_id or alert_s.telegram_chat_id

    if not bot_token or not chat_id:
        raise HTTPException(400, "No bot token/chat ID configured")

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        text = "\u2705 <b>Xray Monitor</b>\n\nTest notification \u2014 configuration is working!"
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


# ========================= Helpers =========================

async def _load_ignore_set(db: AsyncSession) -> set[str]:
    result = await db.execute(select(XrayMonitorSettings).limit(1))
    s = result.scalar_one_or_none()
    if not s or not s.ignore_list:
        return set()
    try:
        return set(json.loads(s.ignore_list))
    except (json.JSONDecodeError, TypeError):
        return set()


async def _save_parsed_keys(db: AsyncSession, keys: list[dict], subscription_id: int | None) -> int:
    ignore_set = await _load_ignore_set(db)
    count = 0
    for idx, k in enumerate(keys):
        address = k.get("address", "").strip()
        port = int(k.get("port", 0))
        if not is_valid_server(address, port):
            continue
        if is_ignored_address(address, ignore_set):
            continue
        srv = XrayMonitorServer(
            subscription_id=subscription_id,
            position=idx,
            name=k.get("name", "Unknown"),
            protocol=k.get("protocol", "unknown"),
            address=address,
            port=port,
            raw_key=k.get("raw_key", ""),
            config_json=json.dumps(k.get("config", {}), ensure_ascii=False),
        )
        db.add(srv)
        count += 1
    await db.commit()
    return count
