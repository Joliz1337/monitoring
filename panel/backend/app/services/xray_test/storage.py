"""Хранение профилей и истории прогонов.

История ограничена сверху: результаты объёмные, а ценность их падает за часы —
без обрезки таблица растёт бесконечно ради данных, на которые никто не смотрит.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    PanelSettings,
    XrayTestResult,
    XrayTestRun,
    XrayTestSniSet,
    XrayTestSubscription,
)
from app.services.xray_test.errors import XrayTestError

MAX_RUNS_KEPT = 50
MAX_SNI_PER_SET = 50


class ProfileNameTakenError(XrayTestError):
    code = "PROFILE_NAME_TAKEN"


class ProfileNotFoundError(XrayTestError):
    code = "PROFILE_NOT_FOUND"


async def load_core_versions(db: AsyncSession, keys: list[str]) -> dict[str, str]:
    """Выбранные версии ядер из настроек панели — читается один раз при старте."""
    rows = await db.execute(
        select(PanelSettings.key, PanelSettings.value).where(PanelSettings.key.in_(keys))
    )
    return {key: value for key, value in rows.all() if value}


async def save_core_version(db: AsyncSession, key: str, version: str) -> None:
    setting = await db.scalar(select(PanelSettings).where(PanelSettings.key == key))
    if setting is None:
        db.add(PanelSettings(key=key, value=version))
    else:
        setting.value = version
    await db.commit()


async def list_subscriptions(db: AsyncSession) -> list[dict]:
    rows = await db.execute(
        select(XrayTestSubscription).order_by(XrayTestSubscription.name)
    )
    return [_subscription_view(row) for row in rows.scalars().all()]


async def get_subscription(db: AsyncSession, profile_id: int) -> XrayTestSubscription:
    profile = await db.get(XrayTestSubscription, profile_id)
    if profile is None:
        raise ProfileNotFoundError(f"Профиль {profile_id} не найден")
    return profile


async def create_subscription(
    db: AsyncSession, *, name: str, kind: str, payload: str, client: Optional[str]
) -> dict:
    await _assert_name_free(db, XrayTestSubscription, name)
    profile = XrayTestSubscription(
        name=name.strip(), kind=kind, payload=payload, user_agent=client
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return _subscription_view(profile)


async def update_subscription(
    db: AsyncSession,
    profile_id: int,
    *,
    name: Optional[str] = None,
    payload: Optional[str] = None,
    client: Optional[str] = None,
) -> dict:
    profile = await get_subscription(db, profile_id)
    if name and name.strip() != profile.name:
        await _assert_name_free(db, XrayTestSubscription, name)
        profile.name = name.strip()
    if payload is not None:
        profile.payload = payload
    if client is not None:
        profile.user_agent = client or None

    await db.commit()
    await db.refresh(profile)
    return _subscription_view(profile)


async def delete_subscription(db: AsyncSession, profile_id: int) -> None:
    profile = await get_subscription(db, profile_id)
    await db.delete(profile)
    await db.commit()


async def mark_subscription_fetched(db: AsyncSession, profile_id: int, count: int) -> None:
    profile = await db.get(XrayTestSubscription, profile_id)
    if profile is None:
        return
    profile.last_fetched_at = datetime.now(timezone.utc)
    profile.last_count = count
    await db.commit()


async def list_sni_sets(db: AsyncSession) -> list[dict]:
    rows = await db.execute(select(XrayTestSniSet).order_by(XrayTestSniSet.name))
    return [_sni_view(row) for row in rows.scalars().all()]


async def create_sni_set(db: AsyncSession, *, name: str, sni_list: list[str]) -> dict:
    await _assert_name_free(db, XrayTestSniSet, name)
    profile = XrayTestSniSet(name=name.strip(), sni_list=_dump_sni(sni_list))
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return _sni_view(profile)


async def update_sni_set(
    db: AsyncSession,
    profile_id: int,
    *,
    name: Optional[str] = None,
    sni_list: Optional[list[str]] = None,
) -> dict:
    profile = await db.get(XrayTestSniSet, profile_id)
    if profile is None:
        raise ProfileNotFoundError(f"Набор SNI {profile_id} не найден")
    if name and name.strip() != profile.name:
        await _assert_name_free(db, XrayTestSniSet, name)
        profile.name = name.strip()
    if sni_list is not None:
        profile.sni_list = _dump_sni(sni_list)

    await db.commit()
    await db.refresh(profile)
    return _sni_view(profile)


async def delete_sni_set(db: AsyncSession, profile_id: int) -> None:
    profile = await db.get(XrayTestSniSet, profile_id)
    if profile is None:
        raise ProfileNotFoundError(f"Набор SNI {profile_id} не найден")
    await db.delete(profile)
    await db.commit()


async def save_run(
    db: AsyncSession,
    *,
    source: str,
    source_name: Optional[str],
    location: str,
    location_name: Optional[str],
    status: str,
    results: list[dict],
    started_at: Optional[datetime] = None,
) -> int:
    counts = {"ok": 0, "degraded": 0, "fail": 0}
    for item in results:
        counts[item.get("verdict", "fail")] = counts.get(item.get("verdict", "fail"), 0) + 1

    run = XrayTestRun(
        started_at=started_at or datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        source=source,
        source_name=source_name,
        location=location,
        # Подпись — склейка имён всех выбранных нод, колонка её длину не вместит
        location_name=(location_name or "")[:200] or None,
        status=status,
        total=len(results),
        ok_count=counts["ok"],
        degraded_count=counts["degraded"],
        fail_count=counts["fail"],
    )
    db.add(run)
    await db.flush()

    db.add_all([
        XrayTestResult(
            run_id=run.id,
            remark=(item.get("remark") or "")[:200],
            protocol=item.get("protocol"),
            address=(item.get("address") or "")[:255],
            port=item.get("port"),
            sni=(item.get("sni") or None),
            transport=item.get("transport"),
            security=item.get("security"),
            core=item.get("core"),
            location=item.get("location"),
            location_name=(item.get("location_name") or "")[:200] or None,
            verdict=item.get("verdict", "fail"),
            reason=item.get("reason"),
            rtt_ms=item.get("rtt_ms"),
            handshake_ms=item.get("handshake_ms"),
            tcp_min_ms=item.get("tcp_min_ms"),
            speed_mbps=item.get("speed_mbps"),
            exit_ip=item.get("exit_ip"),
            exit_country=item.get("exit_country"),
            sni_from_config=bool(item.get("sni_from_config")),
        )
        for item in results
    ])
    await _trim_history(db)
    await db.commit()
    return run.id


async def list_runs(db: AsyncSession, limit: int = 30) -> list[dict]:
    rows = await db.execute(
        select(XrayTestRun).order_by(XrayTestRun.started_at.desc()).limit(limit)
    )
    return [
        {
            "id": run.id,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "source": run.source,
            "source_name": run.source_name,
            "location": run.location,
            "location_name": run.location_name,
            "status": run.status,
            "total": run.total,
            "ok": run.ok_count,
            "degraded": run.degraded_count,
            "fail": run.fail_count,
        }
        for run in rows.scalars().all()
    ]


async def get_run_results(db: AsyncSession, run_id: int) -> list[dict]:
    rows = await db.execute(
        select(XrayTestResult).where(XrayTestResult.run_id == run_id).order_by(XrayTestResult.id)
    )
    # Порядковый номер нужен таблице на фронте: по нему ключи строк и раскрытие
    return [
        {
            "index": index,
            "remark": row.remark,
            "protocol": row.protocol,
            "address": row.address,
            "port": row.port,
            "sni": row.sni,
            "transport": row.transport,
            "security": row.security,
            "core": row.core,
            "location": row.location,
            "location_name": row.location_name,
            "verdict": row.verdict,
            "reason": row.reason,
            "rtt_ms": row.rtt_ms,
            "handshake_ms": row.handshake_ms,
            "tcp_min_ms": row.tcp_min_ms,
            "speed_mbps": row.speed_mbps,
            "exit_ip": row.exit_ip,
            "exit_country": row.exit_country,
            "sni_from_config": bool(row.sni_from_config),
        }
        for index, row in enumerate(rows.scalars().all())
    ]


async def delete_run(db: AsyncSession, run_id: int) -> None:
    await db.execute(delete(XrayTestRun).where(XrayTestRun.id == run_id))
    await db.commit()


async def _trim_history(db: AsyncSession) -> None:
    total = await db.scalar(select(func.count()).select_from(XrayTestRun))
    if not total or total <= MAX_RUNS_KEPT:
        return

    stale = await db.execute(
        select(XrayTestRun.id)
        .order_by(XrayTestRun.started_at.desc())
        .offset(MAX_RUNS_KEPT)
    )
    ids = [row for row in stale.scalars().all()]
    if ids:
        await db.execute(delete(XrayTestRun).where(XrayTestRun.id.in_(ids)))


async def _assert_name_free(db: AsyncSession, model, name: str) -> None:
    exists = await db.scalar(select(model.id).where(model.name == name.strip()))
    if exists:
        raise ProfileNameTakenError(f"Профиль с именем «{name.strip()}» уже есть")


def _dump_sni(sni_list: list[str]) -> str:
    cleaned = [item.strip().lower() for item in sni_list if item and item.strip()]
    return json.dumps(cleaned[:MAX_SNI_PER_SET], ensure_ascii=False)


def _subscription_view(profile: XrayTestSubscription) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "kind": profile.kind,
        "payload": profile.payload,
        "client": profile.user_agent,
        "last_fetched_at": profile.last_fetched_at.isoformat() if profile.last_fetched_at else None,
        "last_count": profile.last_count,
    }


def _sni_view(profile: XrayTestSniSet) -> dict:
    try:
        names = json.loads(profile.sni_list or "[]")
    except json.JSONDecodeError:
        names = []
    return {"id": profile.id, "name": profile.name, "sni_list": names}
