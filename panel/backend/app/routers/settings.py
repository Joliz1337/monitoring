import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional


from app.database import get_db
from app.models import PanelSettings
from app.auth import verify_auth
from app.services import update_channel

router = APIRouter(prefix="/settings", tags=["settings"])

CPU_AFFINITY_KEY = "cpu_affinity_enabled"
# Общий на весь парк список доп. портов, исключаемых из эфемерной выдачи ядра
# ("5201,8443-8450"). Меняется через PUT /reserved-ports/global — тот же ключ
# через generic PUT /settings/{key} рассылку на ноды не запускает.
RESERVED_PORTS_KEY = "reserved_ports_global"
# Графики истории: режим по умолчанию (smooth — сглаженные, raw — как есть),
# полоса пиков и переопределения по метрикам ("cpu:raw,network:smooth")
CHART_MODE_KEY = "chart_mode"
CHART_MODES = {"smooth", "raw"}

DEFAULT_SETTINGS = {
    "refresh_interval": "5",
    "compact_view": "false",
    "blocklist_auto_update_enabled": "true",
    "blocklist_auto_update_interval": "86400",
    # Collector intervals (in seconds)
    "metrics_collect_interval": "10",  # Recommended: 10-15s
    # Значение обязано совпадать с DEFAULT_HAPROXY_INTERVAL коллектора: пока
    # строки в panel_settings нет, коллектор работает со своим значением, а
    # панель показывала бы отсюда другое.
    "haproxy_collect_interval": "300",
    # Time synchronization
    "server_timezone": "Europe/Moscow",
    "time_sync_enabled": "true",
    # Путь поиска установок Remnawave на нодах
    "remnawave_nginx_path": "/opt/remnawave",
    # Канал обновлений панели и нод: main (стабильный) или dev
    "update_branch": update_channel.STABLE_BRANCH,
    # Развод рабочих нагрузок и сетевых прерываний по разным ядрам. Выключено по
    # умолчанию: выигрыш зависит от того, во что упирается конкретная нода, а
    # ядро под сеть забирается у приложения целиком.
    CPU_AFFINITY_KEY: "false",
    RESERVED_PORTS_KEY: "",
    # Разделы, убранные из бокового меню ("billing,updates"). Хранится список
    # выключенных, а не включённых: раздел из следующего релиза появляется сам.
    "hidden_modules": "",
    CHART_MODE_KEY: "smooth",
    "chart_peaks": "true",
    "chart_mode_overrides": "",
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


async def cpu_affinity_enabled(db: AsyncSession) -> bool:
    return (await get_setting(CPU_AFFINITY_KEY, db) or "").lower() == "true"


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


@router.put("/{key}")
async def update_setting(
    key: str,
    data: SettingUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    if key == "update_branch" and data.value not in update_channel.ALLOWED_BRANCHES:
        raise HTTPException(status_code=400, detail="Invalid update branch")

    if key == CHART_MODE_KEY and data.value not in CHART_MODES:
        raise HTTPException(status_code=400, detail="Invalid chart mode")

    await set_setting(key, data.value, db)

    if key == "update_branch":
        update_channel.set_current_branch(data.value)

    if key == "server_timezone":
        from app.services.time_sync import get_time_sync_service
        asyncio.ensure_future(get_time_sync_service().sync_all_servers(data.value))

    if key == CPU_AFFINITY_KEY:
        from app.services.cpu_affinity_sync import push_to_all_nodes
        asyncio.ensure_future(push_to_all_nodes(data.value.lower() == "true"))

    return {"success": True, "key": key, "value": data.value}


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
