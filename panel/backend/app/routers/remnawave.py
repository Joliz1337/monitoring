"""Remnawave integration router.

IP collection via Remnawave Panel API, HWID device tracking.
"""

import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func as sql_func, and_, or_, delete, cast, String
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db, async_session
from app.models import RemnawaveSettings, XrayStats, RemnawaveUserCache, RemnawaveHwidDevice
from app.services.remnawave_api import get_remnawave_api
from app.services.xray_stats_collector import get_xray_stats_collector

router = APIRouter(prefix="/remnawave", tags=["remnawave"])
logger = logging.getLogger(__name__)


# === Request Models ===

class UpdateSettingsRequest(BaseModel):
    api_url: Optional[str] = Field(None, max_length=500)
    api_token: Optional[str] = Field(None, max_length=500)
    cookie_secret: Optional[str] = Field(None, max_length=500)
    enabled: Optional[bool] = None
    collection_interval: Optional[int] = Field(None, ge=60, le=900)
    anomaly_enabled: Optional[bool] = None
    anomaly_use_custom_bot: Optional[bool] = None
    anomaly_tg_bot_token: Optional[str] = Field(None, max_length=200)
    anomaly_tg_chat_id: Optional[str] = Field(None, max_length=100)
    traffic_anomaly_enabled: Optional[bool] = None
    traffic_threshold_gb: Optional[float] = Field(None, ge=1.0, le=500.0)
    traffic_confirm_count: Optional[int] = Field(None, ge=1, le=10)


class AddIgnoredUserRequest(BaseModel):
    user_id: int


# === Helpers ===

def _parse_ignored_user_ids(json_str: Optional[str]) -> list[int]:
    if not json_str:
        return []
    try:
        data = json.loads(json_str)
        if isinstance(data, list):
            return [int(x) for x in data if isinstance(x, (int, str)) and str(x).isdigit()]
        return []
    except (json.JSONDecodeError, ValueError):
        return []


# === Settings ===

@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    result = await db.execute(select(RemnawaveSettings).limit(1))
    s = result.scalar_one_or_none()

    if not s:
        return {
            "api_url": None, "api_token": None, "cookie_secret": None,
            "enabled": False, "collection_interval": 300,
            "ignored_user_ids": [],
        }

    return {
        "api_url": s.api_url,
        "api_token": "***" if s.api_token else None,
        "cookie_secret": "***" if s.cookie_secret else None,
        "enabled": s.enabled,
        "collection_interval": s.collection_interval,
        "ignored_user_ids": _parse_ignored_user_ids(s.ignored_user_ids),
        "anomaly_enabled": s.anomaly_enabled or False,
        "anomaly_use_custom_bot": s.anomaly_use_custom_bot or False,
        "anomaly_tg_bot_token": "***" if s.anomaly_tg_bot_token else None,
        "anomaly_tg_chat_id": s.anomaly_tg_chat_id,
        "anomaly_ignore_ip": _parse_ignored_user_ids(s.anomaly_ignore_ip),
        "anomaly_ignore_hwid": _parse_ignored_user_ids(s.anomaly_ignore_hwid),
        "traffic_anomaly_enabled": s.traffic_anomaly_enabled or False,
        "traffic_threshold_gb": s.traffic_threshold_gb if s.traffic_threshold_gb is not None else 30.0,
        "traffic_confirm_count": s.traffic_confirm_count if s.traffic_confirm_count is not None else 2,
    }


@router.put("/settings")
async def update_settings(request: UpdateSettingsRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    result = await db.execute(select(RemnawaveSettings).limit(1))
    s = result.scalar_one_or_none()
    if not s:
        s = RemnawaveSettings()
        db.add(s)

    for field_name in ("api_url", "api_token", "cookie_secret", "enabled", "collection_interval",
                       "anomaly_enabled", "anomaly_use_custom_bot",
                       "anomaly_tg_bot_token", "anomaly_tg_chat_id",
                       "traffic_anomaly_enabled", "traffic_threshold_gb", "traffic_confirm_count"):
        val = getattr(request, field_name)
        if val is not None:
            setattr(s, field_name, val)

    await db.commit()
    return {"success": True, "message": "Settings updated"}


@router.post("/settings/test")
async def test_connection(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    result = await db.execute(select(RemnawaveSettings).limit(1))
    s = result.scalar_one_or_none()
    if not s or not s.api_url or not s.api_token:
        return {"success": False, "error": "API URL and token not configured"}

    api = get_remnawave_api(s.api_url, s.api_token, s.cookie_secret)
    try:
        r = await api.check_connection()
        return {"success": r.get("auth_valid", False), "api_reachable": r.get("api_reachable", False), "error": r.get("error")}
    finally:
        await api.close()


# === Ignored Users ===

@router.get("/ignored-users")
async def get_ignored_users(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    result = await db.execute(select(RemnawaveSettings).limit(1))
    s = result.scalar_one_or_none()
    ignored_ids = _parse_ignored_user_ids(s.ignored_user_ids if s else None)

    user_info = []
    if ignored_ids:
        cache_result = await db.execute(select(RemnawaveUserCache).where(RemnawaveUserCache.email.in_(ignored_ids)))
        user_cache = {u.email: u for u in cache_result.scalars().all()}
        for uid in ignored_ids:
            cached = user_cache.get(uid)
            user_info.append({
                "user_id": uid,
                "username": cached.username if cached else None,
                "status": cached.status if cached else None,
                "telegram_id": cached.telegram_id if cached else None,
            })

    return {"ignored_users": user_info, "count": len(ignored_ids)}


@router.post("/ignored-users")
async def add_ignored_user(request: AddIgnoredUserRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    result = await db.execute(select(RemnawaveSettings).limit(1))
    s = result.scalar_one_or_none()
    if not s:
        s = RemnawaveSettings()
        db.add(s)

    current = _parse_ignored_user_ids(s.ignored_user_ids)
    if request.user_id in current:
        return {"success": False, "error": "User already ignored"}

    current.append(request.user_id)
    s.ignored_user_ids = json.dumps(current)
    await db.commit()
    return {"success": True, "message": "User added to ignore list"}


@router.delete("/ignored-users/{user_id}")
async def remove_ignored_user(user_id: int, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    result = await db.execute(select(RemnawaveSettings).limit(1))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(404)

    current = _parse_ignored_user_ids(s.ignored_user_ids)
    if user_id not in current:
        return {"success": False, "error": "User not in ignore list"}

    current.remove(user_id)
    s.ignored_user_ids = json.dumps(current)
    await db.commit()
    return {"success": True, "message": "User removed from ignore list"}


# === Collector Status & Control ===

@router.get("/status")
async def get_status(_: dict = Depends(verify_auth)):
    collector = get_xray_stats_collector()
    return collector.get_status()


@router.post("/devices/sync")
async def sync_hwid_devices(_: dict = Depends(verify_auth)):
    collector = get_xray_stats_collector()
    return await collector.sync_hwid_now()


@router.post("/collect")
async def collect_now(_: dict = Depends(verify_auth)):
    collector = get_xray_stats_collector()
    return await collector.collect_now()


# === Remnawave Nodes (read-only from API) ===

@router.get("/nodes")
async def get_nodes(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    result = await db.execute(select(RemnawaveSettings).limit(1))
    s = result.scalar_one_or_none()
    if not s or not s.api_url or not s.api_token:
        return {"nodes": []}

    api = get_remnawave_api(s.api_url, s.api_token, s.cookie_secret)
    try:
        nodes = await api.get_all_nodes()
        return {
            "nodes": [
                {
                    "uuid": n.get("uuid"),
                    "name": n.get("name"),
                    "address": n.get("address"),
                    "is_connected": n.get("isConnected", False),
                    "is_disabled": n.get("isDisabled", False),
                    "country_code": n.get("countryCode", "XX"),
                    "users_online": n.get("usersOnline", 0),
                }
                for n in nodes
            ]
        }
    except Exception as e:
        logger.debug(f"Failed to fetch Remnawave nodes: {e}")
        return {"nodes": [], "error": str(e)}
    finally:
        await api.close()


# === HWID Devices ===

@router.get("/devices")
async def get_devices(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    search: Optional[str] = None,
    platform: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    conditions = []
    if search:
        like_pat = f"%{search.lower()}%"
        user_uuids = select(RemnawaveUserCache.uuid).where(
            or_(
                sql_func.lower(RemnawaveUserCache.username).ilike(like_pat),
                cast(RemnawaveUserCache.email, String).ilike(like_pat),
            )
        )
        conditions.append(RemnawaveHwidDevice.user_uuid.in_(user_uuids))
    if platform:
        conditions.append(sql_func.lower(RemnawaveHwidDevice.platform).ilike(f"%{platform.lower()}%"))

    base_q = select(RemnawaveHwidDevice)
    if conditions:
        base_q = base_q.where(and_(*conditions))

    count_q = select(sql_func.count()).select_from(base_q.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    data_q = base_q.order_by(RemnawaveHwidDevice.synced_at.desc()).offset(offset).limit(limit)
    rows = (await db.execute(data_q)).scalars().all()

    user_uuids_set = {d.user_uuid for d in rows}
    cache_result = await db.execute(
        select(RemnawaveUserCache).where(RemnawaveUserCache.uuid.in_(list(user_uuids_set)))
    ) if user_uuids_set else None
    cache_map = {u.uuid: u for u in cache_result.scalars().all()} if cache_result else {}

    devices = []
    for d in rows:
        cached = cache_map.get(d.user_uuid)
        devices.append({
            "hwid": d.hwid,
            "user_uuid": d.user_uuid,
            "username": cached.username if cached else None,
            "platform": d.platform,
            "os_version": d.os_version,
            "device_model": d.device_model,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        })

    return {"devices": devices, "total": total, "offset": offset, "limit": limit}


@router.get("/devices/user/{user_uuid}")
async def get_user_devices(user_uuid: str, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    result = await db.execute(
        select(RemnawaveHwidDevice)
        .where(RemnawaveHwidDevice.user_uuid == user_uuid)
        .order_by(RemnawaveHwidDevice.created_at.desc())
    )
    devices = [
        {
            "hwid": d.hwid,
            "platform": d.platform,
            "os_version": d.os_version,
            "device_model": d.device_model,
            "user_agent": d.user_agent,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in result.scalars().all()
    ]
    return {"devices": devices, "count": len(devices)}


# === Stats ===

@router.get("/stats/summary")
async def get_summary(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    """Текущий снимок: все IP эфемерные (заменяются каждый цикл сбора)."""
    row = (await db.execute(
        select(
            sql_func.count(sql_func.distinct(XrayStats.email)),
            sql_func.count(sql_func.distinct(XrayStats.source_ip)),
        )
    )).one()

    device_count = (await db.execute(
        select(sql_func.count()).select_from(RemnawaveHwidDevice)
    )).scalar() or 0

    return {
        "unique_users": row[0],
        "unique_ips": row[1],
        "total_devices": device_count,
    }


@router.get("/stats/top-users")
async def get_top_users(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    search: Optional[str] = None,
    status: Optional[str] = Query("ACTIVE"),
    source_ip: Optional[str] = None,
    sort_by: str = Query("unique_ips", pattern="^(unique_ips|username|status|device_count)$"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Текущие IP на пользователя (эфемерные данные). По умолчанию только ACTIVE."""
    conditions = []

    if source_ip:
        ip_emails = select(XrayStats.email).where(
            XrayStats.source_ip.ilike(f"%{source_ip}%")
        ).distinct()
        conditions.append(XrayStats.email.in_(ip_emails))

    cache_conds = []
    if status:
        cache_conds.append(RemnawaveUserCache.status == status)
    if search:
        like_pat = f"%{search.lower()}%"
        cache_conds.append(or_(
            cast(RemnawaveUserCache.email, String).ilike(like_pat),
            sql_func.lower(RemnawaveUserCache.username).ilike(like_pat),
        ))

    if cache_conds:
        cache_emails = select(RemnawaveUserCache.email).where(and_(*cache_conds))
        if search and not status:
            try:
                email_int = int(search)
                conditions.append(or_(
                    XrayStats.email.in_(cache_emails),
                    XrayStats.email == email_int,
                ))
            except ValueError:
                conditions.append(XrayStats.email.in_(cache_emails))
        else:
            conditions.append(XrayStats.email.in_(cache_emails))

    base_q = select(
        XrayStats.email,
        sql_func.count(sql_func.distinct(XrayStats.source_ip)).label("unique_ips"),
    )
    if conditions:
        base_q = base_q.where(and_(*conditions))
    base_q = base_q.group_by(XrayStats.email)

    count_q = select(sql_func.count()).select_from(base_q.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    ips_col = sql_func.count(sql_func.distinct(XrayStats.source_ip))
    if sort_by == "unique_ips":
        order_col = ips_col.desc() if sort_dir == "desc" else ips_col.asc()
    else:
        order_col = ips_col.desc()

    data_q = base_q.order_by(order_col).offset(offset).limit(limit)
    rows = (await db.execute(data_q)).all()

    emails = [r[0] for r in rows]
    cache_result = await db.execute(select(RemnawaveUserCache).where(RemnawaveUserCache.email.in_(emails))) if emails else None
    cache_map = {u.email: u for u in cache_result.scalars().all()} if cache_result else {}

    device_counts: dict[str, int] = {}
    if cache_map:
        user_uuids = [u.uuid for u in cache_map.values() if u.uuid]
        if user_uuids:
            dev_result = await db.execute(
                select(
                    RemnawaveHwidDevice.user_uuid,
                    sql_func.count().label("cnt"),
                ).where(RemnawaveHwidDevice.user_uuid.in_(user_uuids))
                .group_by(RemnawaveHwidDevice.user_uuid)
            )
            device_counts = {row[0]: row[1] for row in dev_result.all()}

    users = []
    for email, unique_ips in rows:
        cached = cache_map.get(email)
        dev_count = device_counts.get(cached.uuid, 0) if cached and cached.uuid else 0
        users.append({
            "email": email,
            "username": cached.username if cached else None,
            "status": cached.status if cached else None,
            "unique_ips": unique_ips,
            "device_count": dev_count,
        })

    if sort_by in ("username", "status", "device_count"):
        reverse = sort_dir == "desc"
        users.sort(key=lambda u: (u.get(sort_by) or "") if sort_by != "device_count" else (u.get("device_count") or 0), reverse=reverse)

    return {"users": users, "total": total, "offset": offset, "limit": limit}


@router.get("/stats/user/{email}")
async def get_user_stats(
    email: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Текущие IP и устройства пользователя."""
    result = await db.execute(
        select(XrayStats.source_ip, XrayStats.last_seen)
        .where(XrayStats.email == email)
        .order_by(XrayStats.last_seen.desc())
    )
    rows = result.all()

    cache_result = await db.execute(select(RemnawaveUserCache).where(RemnawaveUserCache.email == email))
    cached = cache_result.scalar_one_or_none()

    ips = [
        {
            "source_ip": source_ip,
            "last_seen": last_seen.isoformat() if last_seen else None,
        }
        for source_ip, last_seen in rows
    ]

    devices = []
    if cached and cached.uuid:
        dev_result = await db.execute(
            select(RemnawaveHwidDevice)
            .where(RemnawaveHwidDevice.user_uuid == cached.uuid)
            .order_by(RemnawaveHwidDevice.created_at.desc())
        )
        devices = [
            {
                "hwid": d.hwid,
                "platform": d.platform,
                "os_version": d.os_version,
                "device_model": d.device_model,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in dev_result.scalars().all()
        ]

    return {
        "email": email,
        "username": cached.username if cached else None,
        "status": cached.status if cached else None,
        "unique_ips": len(ips),
        "ips": ips,
        "devices": devices,
    }


@router.delete("/stats/clear")
async def clear_stats(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    stats_result = await db.execute(delete(XrayStats))
    hwid_result = await db.execute(delete(RemnawaveHwidDevice))
    await db.commit()

    # Сбросить in-memory кулдауны аномалий
    collector = get_xray_stats_collector()
    collector._anomaly_last_notified.clear()

    return {
        "success": True,
        "deleted": {"xray_stats": stats_result.rowcount, "hwid_devices": hwid_result.rowcount},
    }


@router.delete("/stats/user/{email}/ips/{source_ip}")
async def clear_user_ip(email: int, source_ip: str, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    from urllib.parse import unquote
    source_ip = unquote(source_ip)
    result = await db.execute(
        delete(XrayStats).where(and_(XrayStats.email == email, XrayStats.source_ip == source_ip))
    )
    await db.commit()
    return {"success": True, "email": email, "source_ip": source_ip, "deleted_records": result.rowcount}


@router.delete("/stats/user/{email}/ips")
async def clear_user_all_ips(email: int, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    result = await db.execute(delete(XrayStats).where(XrayStats.email == email))
    await db.commit()
    return {"success": True, "email": email, "deleted_records": result.rowcount}


@router.delete("/stats/client-ips/clear")
async def clear_all_client_ips(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    result = await db.execute(delete(XrayStats))
    await db.commit()
    return {"success": True, "deleted_records": result.rowcount}


# === Anomalies ===

KNOWN_UA_PATTERN = re.compile(
    r'^(v2raytun/(ios|android|windows)'
    r'|Clash-Meta/Prizrak-Box'
    r'|Happ/'
    r'|FlClash ?X/'
    r'|INCY/'
    r'|HiddifyNext/'
    r'|Hiddify/'
    r'|Flowvy/'
    r'|prizrak-box/'
    r'|koala-clash/'
    r')',
    re.IGNORECASE,
)

VERSION_PATTERN = re.compile(r'^[\d._]+$')


@router.get("/anomalies")
async def get_anomalies(
    minutes: int = Query(10, ge=1, le=60),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """
    Детектор аномалий:
    - ip_exceeds_limit: кол-во текущих IP > hwid_device_limit
    - hwid_exceeds_limit: кол-во HWID устройств > hwid_device_limit
    - unknown_user_agent: неизвестный user-agent на HWID устройстве
    """
    # Игнор-листы
    settings_result = await db.execute(select(RemnawaveSettings).limit(1))
    s = settings_result.scalar_one_or_none()
    ignore_ip = set(_parse_ignored_user_ids(s.anomaly_ignore_ip if s else None))
    ignore_hwid = set(_parse_ignored_user_ids(s.anomaly_ignore_hwid if s else None))
    ignore_all = set(_parse_ignored_user_ids(s.ignored_user_ids if s else None))

    # HWID устройств на пользователя (по uuid)
    hwid_counts_q = (
        select(
            RemnawaveHwidDevice.user_uuid,
            sql_func.count().label("device_count"),
        )
        .group_by(RemnawaveHwidDevice.user_uuid)
    )
    hwid_rows = (await db.execute(hwid_counts_q)).all()
    hwid_by_uuid: dict[str, int] = {row[0]: row[1] for row in hwid_rows}

    # Все устройства для проверки UA и данных
    all_devices_q = select(
        RemnawaveHwidDevice.user_uuid,
        RemnawaveHwidDevice.hwid,
        RemnawaveHwidDevice.user_agent,
        RemnawaveHwidDevice.platform,
        RemnawaveHwidDevice.device_model,
        RemnawaveHwidDevice.os_version,
    )
    all_devices = (await db.execute(all_devices_q)).all()

    # Кэш пользователей
    cache_result = await db.execute(select(RemnawaveUserCache))
    cache_list = cache_result.scalars().all()
    cache_by_email: dict[int, any] = {u.email: u for u in cache_list}
    cache_by_uuid: dict[str, any] = {u.uuid: u for u in cache_list if u.uuid}

    anomalies = []

    # 1) IP > лимит — только подтверждённые (streak >= 5 из коллектора)
    collector = get_xray_stats_collector()
    ip_streaks = collector.get_ip_anomaly_streaks()
    IP_CONFIRM_THRESHOLD = 5

    for email, (streak, ip_count) in ip_streaks.items():
        if streak < IP_CONFIRM_THRESHOLD:
            continue
        if email in ignore_all or email in ignore_ip:
            continue
        cached = cache_by_email.get(email)
        if not cached or cached.status != 'ACTIVE':
            continue
        limit_val = cached.hwid_device_limit
        if limit_val is None or limit_val <= 0:
            continue
        anomalies.append({
            "type": "ip_exceeds_limit",
            "severity": "high" if ip_count > limit_val * 2 else "medium",
            "email": email,
            "username": cached.username,
            "status": cached.status,
            "current": ip_count,
            "limit": limit_val,
            "detail": f"{ip_count} IP / {limit_val} allowed (streak: {streak})",
        })

    # 2) HWID devices > лимит (только ACTIVE)
    for uuid, device_count in hwid_by_uuid.items():
        cached = cache_by_uuid.get(uuid)
        if not cached or cached.status != 'ACTIVE':
            continue
        if cached.email in ignore_all or cached.email in ignore_hwid:
            continue
        limit_val = cached.hwid_device_limit
        if limit_val is None or limit_val <= 0:
            continue
        if device_count > limit_val:
            anomalies.append({
                "type": "hwid_exceeds_limit",
                "severity": "high" if device_count > limit_val * 2 else "medium",
                "email": cached.email,
                "username": cached.username,
                "status": cached.status,
                "current": device_count,
                "limit": limit_val,
                "detail": f"{device_count} devices / {limit_val} allowed",
            })

    # 3) Неизвестные user-agent
    # 5) Невалидные данные устройства (платформа/версия/модель)
    for user_uuid, hwid, user_agent, platform, device_model, os_version in all_devices:
        cached = cache_by_uuid.get(user_uuid)
        if not cached or cached.status != 'ACTIVE':
            continue
        if cached.email in ignore_all or cached.email in ignore_hwid:
            continue

        if user_agent and not KNOWN_UA_PATTERN.search(user_agent):
            anomalies.append({
                "type": "unknown_user_agent",
                "severity": "low",
                "email": cached.email,
                "username": cached.username,
                "status": cached.status,
                "current": user_agent,
                "limit": None,
                "detail": f"{platform or '?'} / {device_model or '?'}: {user_agent[:80]}",
            })

        problems = []
        if not platform or not platform.strip():
            problems.append("платформа: пусто")
        if not os_version or not VERSION_PATTERN.match(os_version.strip()):
            problems.append(f"версия: «{os_version or 'пусто'}»")
        if not device_model or not device_model.strip():
            problems.append("модель: пусто")

        if problems:
            anomalies.append({
                "type": "invalid_device_data",
                "severity": "medium",
                "email": cached.email,
                "username": cached.username,
                "status": cached.status,
                "current": hwid[:40],
                "limit": None,
                "detail": ", ".join(problems),
            })

    # 4) Трафик превышает порог (только подтверждённые streak >= confirm)
    threshold_gb = s.traffic_threshold_gb if s and s.traffic_threshold_gb else 30.0
    confirm = s.traffic_confirm_count if s and s.traffic_confirm_count else 2
    for item in collector.get_traffic_anomalies():
        email = item["email"]
        streak = item["streak"]
        if streak < confirm:
            continue
        if email in ignore_all:
            continue
        cached = cache_by_email.get(email)
        if not cached or cached.status != 'ACTIVE':
            continue
        anomalies.append({
            "type": "traffic_exceeds_limit",
            "severity": "high",
            "email": email,
            "username": cached.username,
            "status": cached.status,
            "current": streak,
            "limit": confirm,
            "detail": f"Streak: {streak}/{confirm} (>{threshold_gb} GB/30min)",
        })

    # Сортировка: high → medium → low
    severity_order = {"high": 0, "medium": 1, "low": 2}
    anomalies.sort(key=lambda a: (severity_order.get(a["severity"], 9), a.get("type", "")))

    summary = {
        "ip_exceeds": len([a for a in anomalies if a["type"] == "ip_exceeds_limit"]),
        "hwid_exceeds": len([a for a in anomalies if a["type"] == "hwid_exceeds_limit"]),
        "unknown_ua": len([a for a in anomalies if a["type"] == "unknown_user_agent"]),
        "traffic_exceeds": len([a for a in anomalies if a["type"] == "traffic_exceeds_limit"]),
        "invalid_device": len([a for a in anomalies if a["type"] == "invalid_device_data"]),
        "total": len(anomalies),
    }

    return {"anomalies": anomalies, "summary": summary, "minutes": minutes}


# === Anomaly Ignore Lists ===

class AnomalyIgnoreRequest(BaseModel):
    user_id: int
    list_type: str = Field(..., pattern="^(ip|hwid|all)$")


def _modify_id_list(json_str: str | None, user_id: int, action: str) -> str:
    """Add or remove user_id from JSON list. Returns updated JSON string."""
    current = _parse_ignored_user_ids(json_str)
    if action == "add" and user_id not in current:
        current.append(user_id)
    elif action == "remove" and user_id in current:
        current.remove(user_id)
    return json.dumps(current)


@router.post("/anomalies/ignore")
async def add_anomaly_ignore(request: AnomalyIgnoreRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    result = await db.execute(select(RemnawaveSettings).limit(1))
    s = result.scalar_one_or_none()
    if not s:
        s = RemnawaveSettings()
        db.add(s)

    if request.list_type in ("ip", "all"):
        s.anomaly_ignore_ip = _modify_id_list(s.anomaly_ignore_ip, request.user_id, "add")
    if request.list_type in ("hwid", "all"):
        s.anomaly_ignore_hwid = _modify_id_list(s.anomaly_ignore_hwid, request.user_id, "add")
    if request.list_type == "all":
        s.ignored_user_ids = _modify_id_list(s.ignored_user_ids, request.user_id, "add")

    await db.commit()
    return {"success": True}


@router.delete("/anomalies/ignore")
async def remove_anomaly_ignore(request: AnomalyIgnoreRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    result = await db.execute(select(RemnawaveSettings).limit(1))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(404)

    if request.list_type in ("ip", "all"):
        s.anomaly_ignore_ip = _modify_id_list(s.anomaly_ignore_ip, request.user_id, "remove")
    if request.list_type in ("hwid", "all"):
        s.anomaly_ignore_hwid = _modify_id_list(s.anomaly_ignore_hwid, request.user_id, "remove")
    if request.list_type == "all":
        s.ignored_user_ids = _modify_id_list(s.ignored_user_ids, request.user_id, "remove")

    await db.commit()
    return {"success": True}


# === Ignore Lists ===

@router.get("/ignore-lists")
async def get_ignore_lists(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    """Все игнор-листы: общий, IP, HWID."""
    result = await db.execute(select(RemnawaveSettings).limit(1))
    s = result.scalar_one_or_none()

    all_ids = _parse_ignored_user_ids(s.ignored_user_ids if s else None)
    ip_ids = _parse_ignored_user_ids(s.anomaly_ignore_ip if s else None)
    hwid_ids = _parse_ignored_user_ids(s.anomaly_ignore_hwid if s else None)

    unique_ids = set(all_ids + ip_ids + hwid_ids)
    cache_map = {}
    if unique_ids:
        cache_result = await db.execute(select(RemnawaveUserCache).where(RemnawaveUserCache.email.in_(list(unique_ids))))
        cache_map = {u.email: u for u in cache_result.scalars().all()}

    def _enrich(ids: list[int]) -> list[dict]:
        return [{"user_id": uid, "username": cache_map[uid].username if uid in cache_map else None} for uid in ids]

    return {
        "all": _enrich(all_ids),
        "ip": _enrich(ip_ids),
        "hwid": _enrich(hwid_ids),
    }


@router.delete("/ignore-lists/{list_type}/{user_id}")
async def remove_from_ignore_list(list_type: str, user_id: int, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    if list_type not in ("all", "ip", "hwid"):
        raise HTTPException(400, "Invalid list_type")

    result = await db.execute(select(RemnawaveSettings).limit(1))
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(404)

    if list_type == "all":
        s.ignored_user_ids = _modify_id_list(s.ignored_user_ids, user_id, "remove")
    elif list_type == "ip":
        s.anomaly_ignore_ip = _modify_id_list(s.anomaly_ignore_ip, user_id, "remove")
    elif list_type == "hwid":
        s.anomaly_ignore_hwid = _modify_id_list(s.anomaly_ignore_hwid, user_id, "remove")

    await db.commit()
    return {"success": True}


# === User Cache ===

@router.get("/users")
async def get_users(search: Optional[str] = None, limit: int = Query(100, ge=1, le=1000), db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    q = select(RemnawaveUserCache)
    if search:
        q = q.where(
            (RemnawaveUserCache.username.ilike(f"%{search}%")) |
            (RemnawaveUserCache.email == int(search) if search.isdigit() else False)
        )
    q = q.order_by(RemnawaveUserCache.username).limit(limit)
    result = await db.execute(q)
    users = [
        {"email": u.email, "uuid": u.uuid, "username": u.username, "telegram_id": u.telegram_id, "status": u.status}
        for u in result.scalars().all()
    ]
    count_result = await db.execute(select(sql_func.count()).select_from(RemnawaveUserCache))
    return {"count": count_result.scalar() or 0, "users": users}


@router.post("/users/refresh")
async def refresh_users(_: dict = Depends(verify_auth)):
    collector = get_xray_stats_collector()
    return await collector.refresh_user_cache_now()


@router.get("/users/cache-status")
async def get_cache_status(_: dict = Depends(verify_auth)):
    collector = get_xray_stats_collector()
    return collector.get_user_cache_status()
