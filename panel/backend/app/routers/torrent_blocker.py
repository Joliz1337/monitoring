import json
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import TorrentBlockerBan, TorrentBlockerSettings, RemnawaveSettings
from app.services.remnawave_api import RemnawaveAPI, RemnawaveAPIError
from app.services.torrent_blocker import get_torrent_blocker_service

router = APIRouter(prefix="/torrent-blocker", tags=["torrent-blocker"])


class UpdateSettings(BaseModel):
    enabled: Optional[bool] = None
    poll_interval_minutes: Optional[int] = Field(None, ge=1, le=60)
    ban_duration_minutes: Optional[int] = Field(None, ge=1, le=43200)
    excluded_server_ids: Optional[list[int]] = None
    webhook_enabled: Optional[bool] = None
    webhook_url: Optional[str] = Field(None, max_length=2000)
    webhook_secret: Optional[str] = Field(None, max_length=500)
    webhook_delay_seconds: Optional[int] = Field(None, ge=0, le=1800)

    @field_validator("webhook_url")
    @classmethod
    def _require_https(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if value and not value.startswith("https://"):
            raise ValueError("webhook_url must use https://")
        return value


def _settings_to_dict(s: TorrentBlockerSettings) -> dict:
    excluded = []
    if s.excluded_server_ids:
        try:
            excluded = json.loads(s.excluded_server_ids)
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "enabled": s.enabled,
        "poll_interval_minutes": s.poll_interval_minutes,
        "ban_duration_minutes": s.ban_duration_minutes,
        "excluded_server_ids": excluded,
        "webhook_enabled": s.webhook_enabled,
        "webhook_url": s.webhook_url or "",
        "webhook_secret": s.webhook_secret or "",
        "webhook_delay_seconds": s.webhook_delay_seconds if s.webhook_delay_seconds is not None else 60,
    }


DEFAULTS = {
    "enabled": False,
    "poll_interval_minutes": 5,
    "ban_duration_minutes": 30,
    "excluded_server_ids": [],
    "webhook_enabled": False,
    "webhook_url": "",
    "webhook_secret": "",
    "webhook_delay_seconds": 60,
}


@router.get("/settings")
async def get_settings(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    result = await db.execute(select(TorrentBlockerSettings).limit(1))
    settings = result.scalar_one_or_none()
    if not settings:
        return DEFAULTS
    return _settings_to_dict(settings)


@router.put("/settings")
async def update_settings(
    body: UpdateSettings,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    result = await db.execute(select(TorrentBlockerSettings).limit(1))
    settings = result.scalar_one_or_none()

    if not settings:
        settings = TorrentBlockerSettings()
        db.add(settings)

    if body.enabled is not None:
        settings.enabled = body.enabled
    if body.poll_interval_minutes is not None:
        settings.poll_interval_minutes = body.poll_interval_minutes
    if body.ban_duration_minutes is not None:
        settings.ban_duration_minutes = body.ban_duration_minutes
    if body.excluded_server_ids is not None:
        settings.excluded_server_ids = json.dumps(body.excluded_server_ids)
    if body.webhook_enabled is not None:
        settings.webhook_enabled = body.webhook_enabled
    if body.webhook_url is not None:
        settings.webhook_url = body.webhook_url or None
    if body.webhook_secret is not None:
        settings.webhook_secret = body.webhook_secret or None
    if body.webhook_delay_seconds is not None:
        settings.webhook_delay_seconds = body.webhook_delay_seconds

    await db.commit()
    await db.refresh(settings)
    return _settings_to_dict(settings)


@router.get("/status")
async def get_status(_: dict = Depends(verify_auth)):
    service = get_torrent_blocker_service()
    return await service.get_status()


@router.post("/poll-now")
async def poll_now(_: dict = Depends(verify_auth)):
    service = get_torrent_blocker_service()
    await service.run_now()
    return {"message": "Poll cycle triggered"}


class TestWebhook(BaseModel):
    webhook_url: str = Field(..., max_length=2000)
    webhook_secret: Optional[str] = Field(None, max_length=500)


@router.post("/test-webhook")
async def test_webhook(body: TestWebhook, _: dict = Depends(verify_auth)):
    service = get_torrent_blocker_service()
    success, message = await service.send_test_webhook(body.webhook_url, body.webhook_secret)
    return {"success": success, "message": message}


async def _get_rw_api(db: AsyncSession) -> RemnawaveAPI:
    result = await db.execute(select(RemnawaveSettings).limit(1))
    rw = result.scalar_one_or_none()
    if not rw or not rw.api_url or not rw.api_token:
        raise HTTPException(status_code=400, detail="Remnawave API not configured")
    return RemnawaveAPI(rw.api_url, rw.api_token, rw.cookie_secret)


RANGE_CONFIG: dict[str, tuple[timedelta, timedelta, str]] = {
    "24h": (timedelta(hours=24), timedelta(hours=1), "hour"),
    "7d":  (timedelta(days=7),   timedelta(days=1),  "day"),
    "30d": (timedelta(days=30),  timedelta(days=1),  "day"),
}


@router.get("/stats-internal")
async def get_internal_stats(
    range_: Literal["24h", "7d", "30d"] = Query("24h", alias="range"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    period, step, trunc_unit = RANGE_CONFIG[range_]
    now = datetime.now(timezone.utc)
    period_start = now - period

    active_q = select(func.count(distinct(TorrentBlockerBan.ip))).where(
        TorrentBlockerBan.expires_at > now
    )
    currently_banned = (await db.execute(active_q)).scalar_one() or 0

    bucket_col = func.date_trunc(trunc_unit, TorrentBlockerBan.banned_at, "UTC").label("bucket")
    rows = (await db.execute(
        select(bucket_col, func.count())
        .where(TorrentBlockerBan.banned_at >= period_start)
        .group_by(bucket_col)
        .order_by(bucket_col)
    )).all()

    counts: dict[datetime, int] = {}
    for bucket, cnt in rows:
        if bucket.tzinfo is None:
            bucket = bucket.replace(tzinfo=timezone.utc)
        counts[bucket] = int(cnt)

    if trunc_unit == "hour":
        anchor = now.replace(minute=0, second=0, microsecond=0)
    else:
        anchor = now.replace(hour=0, minute=0, second=0, microsecond=0)

    bucket_count = int(period / step)
    series: list[dict] = []
    total = 0
    for i in range(bucket_count - 1, -1, -1):
        ts = anchor - step * i
        cnt = counts.get(ts, 0)
        total += cnt
        series.append({"time": ts.isoformat(), "count": cnt})

    return {
        "range": range_,
        "currently_banned": int(currently_banned),
        "total_in_range": total,
        "buckets": series,
    }


@router.get("/reports")
async def get_reports(
    start: int = 0,
    size: int = 50,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    api = await _get_rw_api(db)
    try:
        data = await api.get_torrent_blocker_reports(start=start, size=size)
        return data
    except RemnawaveAPIError as e:
        raise HTTPException(status_code=502, detail=e.message)
    finally:
        await api.close()


@router.delete("/truncate")
async def truncate_reports(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    api = await _get_rw_api(db)
    try:
        data = await api.truncate_torrent_blocker_reports()
        return {"message": "Reports truncated", "data": data}
    except RemnawaveAPIError as e:
        raise HTTPException(status_code=502, detail=e.message)
    finally:
        await api.close()
