import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import TorrentBlockerSettings, RemnawaveSettings
from app.services.remnawave_api import RemnawaveAPI, RemnawaveAPIError
from app.services.torrent_blocker import get_torrent_blocker_service

router = APIRouter(prefix="/torrent-blocker", tags=["torrent-blocker"])


class UpdateSettings(BaseModel):
    enabled: Optional[bool] = None
    poll_interval_minutes: Optional[int] = Field(None, ge=1, le=60)
    ban_duration_minutes: Optional[int] = Field(None, ge=1, le=43200)
    excluded_server_ids: Optional[list[int]] = None


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
    }


DEFAULTS = {
    "enabled": False,
    "poll_interval_minutes": 5,
    "ban_duration_minutes": 30,
    "excluded_server_ids": [],
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


async def _get_rw_api(db: AsyncSession) -> RemnawaveAPI:
    result = await db.execute(select(RemnawaveSettings).limit(1))
    rw = result.scalar_one_or_none()
    if not rw or not rw.api_url or not rw.api_token:
        raise HTTPException(status_code=400, detail="Remnawave API not configured")
    return RemnawaveAPI(rw.api_url, rw.api_token, rw.cookie_secret)


@router.get("/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    api = await _get_rw_api(db)
    try:
        data = await api.get_torrent_blocker_stats()
        return data
    except RemnawaveAPIError as e:
        raise HTTPException(status_code=502, detail=e.message)
    finally:
        await api.close()


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
