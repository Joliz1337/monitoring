import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, delete, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models import AlertSettings, AlertHistory
from app.auth import verify_auth
from app.services.server_alerter import get_server_alerter

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    language: Optional[str] = None
    check_interval: Optional[int] = None
    alert_cooldown: Optional[int] = None

    offline_enabled: Optional[bool] = None
    offline_fail_threshold: Optional[int] = None
    offline_recovery_notify: Optional[bool] = None

    cpu_enabled: Optional[bool] = None
    cpu_critical_threshold: Optional[float] = None
    cpu_spike_percent: Optional[float] = None
    cpu_sustained_seconds: Optional[int] = None
    cpu_min_value: Optional[float] = None

    ram_enabled: Optional[bool] = None
    ram_critical_threshold: Optional[float] = None
    ram_spike_percent: Optional[float] = None
    ram_sustained_seconds: Optional[int] = None
    ram_min_value: Optional[float] = None

    network_enabled: Optional[bool] = None
    network_spike_percent: Optional[float] = None
    network_drop_percent: Optional[float] = None
    network_sustained_seconds: Optional[int] = None
    network_min_bytes: Optional[float] = None

    tcp_established_enabled: Optional[bool] = None
    tcp_established_spike_percent: Optional[float] = None
    tcp_established_drop_percent: Optional[float] = None
    tcp_established_sustained_seconds: Optional[int] = None
    tcp_min_connections: Optional[int] = None

    tcp_listen_enabled: Optional[bool] = None
    tcp_listen_spike_percent: Optional[float] = None
    tcp_listen_sustained_seconds: Optional[int] = None

    tcp_timewait_enabled: Optional[bool] = None
    tcp_timewait_spike_percent: Optional[float] = None
    tcp_timewait_sustained_seconds: Optional[int] = None

    tcp_closewait_enabled: Optional[bool] = None
    tcp_closewait_spike_percent: Optional[float] = None
    tcp_closewait_sustained_seconds: Optional[int] = None

    tcp_synsent_enabled: Optional[bool] = None
    tcp_synsent_spike_percent: Optional[float] = None
    tcp_synsent_sustained_seconds: Optional[int] = None

    tcp_synrecv_enabled: Optional[bool] = None
    tcp_synrecv_spike_percent: Optional[float] = None
    tcp_synrecv_sustained_seconds: Optional[int] = None

    tcp_finwait_enabled: Optional[bool] = None
    tcp_finwait_spike_percent: Optional[float] = None
    tcp_finwait_sustained_seconds: Optional[int] = None

    excluded_server_ids: Optional[list[int]] = None


class TelegramTestRequest(BaseModel):
    bot_token: str
    chat_id: str


def _settings_to_dict(s: AlertSettings) -> dict:
    return {
        "enabled": s.enabled,
        "telegram_bot_token": s.telegram_bot_token or "",
        "telegram_chat_id": s.telegram_chat_id or "",
        "language": s.language or "en",
        "check_interval": s.check_interval,
        "alert_cooldown": s.alert_cooldown,
        "offline_enabled": s.offline_enabled,
        "offline_fail_threshold": s.offline_fail_threshold,
        "offline_recovery_notify": s.offline_recovery_notify,
        "cpu_enabled": s.cpu_enabled,
        "cpu_critical_threshold": s.cpu_critical_threshold,
        "cpu_spike_percent": s.cpu_spike_percent,
        "cpu_sustained_seconds": s.cpu_sustained_seconds,
        "cpu_min_value": s.cpu_min_value,
        "ram_enabled": s.ram_enabled,
        "ram_critical_threshold": s.ram_critical_threshold,
        "ram_spike_percent": s.ram_spike_percent,
        "ram_sustained_seconds": s.ram_sustained_seconds,
        "ram_min_value": s.ram_min_value,
        "network_enabled": s.network_enabled,
        "network_spike_percent": s.network_spike_percent,
        "network_drop_percent": s.network_drop_percent,
        "network_sustained_seconds": s.network_sustained_seconds,
        "network_min_bytes": s.network_min_bytes,
        "tcp_established_enabled": s.tcp_established_enabled,
        "tcp_established_spike_percent": s.tcp_established_spike_percent,
        "tcp_established_drop_percent": s.tcp_established_drop_percent,
        "tcp_established_sustained_seconds": s.tcp_established_sustained_seconds,
        "tcp_min_connections": s.tcp_min_connections,
        "tcp_listen_enabled": s.tcp_listen_enabled,
        "tcp_listen_spike_percent": s.tcp_listen_spike_percent,
        "tcp_listen_sustained_seconds": s.tcp_listen_sustained_seconds,
        "tcp_timewait_enabled": s.tcp_timewait_enabled,
        "tcp_timewait_spike_percent": s.tcp_timewait_spike_percent,
        "tcp_timewait_sustained_seconds": s.tcp_timewait_sustained_seconds,
        "tcp_closewait_enabled": s.tcp_closewait_enabled,
        "tcp_closewait_spike_percent": s.tcp_closewait_spike_percent,
        "tcp_closewait_sustained_seconds": s.tcp_closewait_sustained_seconds,
        "tcp_synsent_enabled": s.tcp_synsent_enabled,
        "tcp_synsent_spike_percent": s.tcp_synsent_spike_percent,
        "tcp_synsent_sustained_seconds": s.tcp_synsent_sustained_seconds,
        "tcp_synrecv_enabled": s.tcp_synrecv_enabled,
        "tcp_synrecv_spike_percent": s.tcp_synrecv_spike_percent,
        "tcp_synrecv_sustained_seconds": s.tcp_synrecv_sustained_seconds,
        "tcp_finwait_enabled": s.tcp_finwait_enabled,
        "tcp_finwait_spike_percent": s.tcp_finwait_spike_percent,
        "tcp_finwait_sustained_seconds": s.tcp_finwait_sustained_seconds,
        "excluded_server_ids": json.loads(s.excluded_server_ids) if s.excluded_server_ids else [],
    }


@router.get("/settings", dependencies=[Depends(verify_auth)])
async def get_alert_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AlertSettings).limit(1))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = AlertSettings()
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return _settings_to_dict(settings)


@router.put("/settings", dependencies=[Depends(verify_auth)])
async def update_alert_settings(
    data: AlertSettingsUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AlertSettings).limit(1))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = AlertSettings()
        db.add(settings)
        await db.flush()

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if key == "excluded_server_ids":
            setattr(settings, key, json.dumps(value) if value is not None else None)
        else:
            setattr(settings, key, value)

    await db.commit()
    await db.refresh(settings)
    return _settings_to_dict(settings)


@router.post("/test-telegram", dependencies=[Depends(verify_auth)])
async def test_telegram(data: TelegramTestRequest):
    alerter = get_server_alerter()
    return await alerter.test_telegram(data.bot_token, data.chat_id)


@router.get("/status", dependencies=[Depends(verify_auth)])
async def get_alerter_status():
    alerter = get_server_alerter()
    return alerter.get_status()


@router.get("/history", dependencies=[Depends(verify_auth)])
async def get_alert_history(
    server_id: Optional[int] = Query(None),
    alert_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    query = select(AlertHistory).order_by(desc(AlertHistory.created_at))

    if server_id is not None:
        query = query.where(AlertHistory.server_id == server_id)
    if alert_type:
        query = query.where(AlertHistory.alert_type == alert_type)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    rows = (await db.execute(query.offset(offset).limit(limit))).scalars().all()

    items = []
    for r in rows:
        items.append({
            "id": r.id,
            "server_id": r.server_id,
            "server_name": r.server_name,
            "alert_type": r.alert_type,
            "severity": r.severity,
            "message": r.message,
            "details": r.details,
            "notified": r.notified,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })

    return {"items": items, "total": total}


@router.delete("/history", dependencies=[Depends(verify_auth)])
async def clear_alert_history(db: AsyncSession = Depends(get_db)):
    result = await db.execute(delete(AlertHistory))
    await db.commit()
    return {"deleted": result.rowcount}
