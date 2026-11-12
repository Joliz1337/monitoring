"""API поиска IP в облаках (VK Cloud, Yandex Cloud, Cloud.ru, Selectel) и ширины канала Cloud.ru."""

import asyncio
import ipaddress
import logging
from typing import Literal, Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, engine
from app.auth import verify_auth
from app.security import is_direct_local_request
from app.services.ip_search import cloudru_adv, cloudru_bandwidth, cloudru_evo, yandex
from app.services.ip_search.cloudru_adv import CloudAdvService
from app.services.ip_search.cloudru_evo import CloudEvoService
from app.services.ip_search.selectel import (
    SelectelService,
    add_whitelist_cidr as selectel_add_whitelist_cidr,
    add_whitelist_cidrs as selectel_add_whitelist_cidrs,
    clear_whitelist as selectel_clear_whitelist,
    enable_multiqueue as selectel_enable_multiqueue,
    get_whitelist as selectel_get_whitelist,
    init_tables as selectel_init_tables,
    remove_whitelist_cidr as selectel_remove_whitelist_cidr,
)
from app.services.ip_search.vk import (
    AVAILABLE_SUBNETS,
    ExtService,
    add_custom_subnet,
    add_precise_cidr,
    enable_multiqueue,
    get_all_subnets,
    get_custom_subnets,
    get_precise_cidrs,
    get_subnet_name,
    init_tables,
    remove_custom_subnet,
    remove_precise_cidr,
)
from app.services.ip_search.yandex import (
    YcService,
    add_custom_subnet as yc_add_custom_subnet,
    get_all_subnets as yc_get_all_subnets,
    init_tables as yc_init_tables,
    remove_custom_subnet as yc_remove_custom_subnet,
)
from app.services.shutdown_hooks import register_shutdown_hook

logger = logging.getLogger(__name__)

# Сколько воркер доделывает раунд при остановке панели, прежде чем его добьют:
# Cloud.ru ждёт provision каждого адреса пачки, потом промахи удаляются с паузами
WORKER_ROUND_FINISH_SECONDS = 150


class AccountCreate(BaseModel):
    email: str
    password: str
    proxy: Optional[str] = None


class AccountUpdate(BaseModel):
    enabled: Optional[bool] = None
    password: Optional[str] = None
    proxy: Optional[str] = None


class ProjectCreate(BaseModel):
    project_id: str
    project_name: str


class ProjectUpdate(BaseModel):
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    disabled: Optional[bool] = None


class SettingsUpdate(BaseModel):
    delay_min: Optional[int] = None
    delay_max: Optional[int] = None
    max_ip_per_project: Optional[int] = None
    stop_on_catch: Optional[bool] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    target_subnets: Optional[List[int]] = None
    proxy_required: Optional[bool] = None
    proxy_check_interval: Optional[int] = None
    log_level: Optional[str] = None  # "info" или "full"
    start_delay_max: Optional[int] = None
    subnet_mode: Optional[Literal["per_account", "rotate"]] = None


class PreciseCidrAdd(BaseModel):
    cidr: str


class ManualObtainRequest(BaseModel):
    account_id: int
    project_db_id: int
    subnet_key: int
    custom_subnet_name: Optional[str] = None


class MultiqueueRequest(BaseModel):
    auth_url: str
    project_id: str
    image_id: str
    auth_type: str = "password"
    username: Optional[str] = ""
    user_domain_name: Optional[str] = "users"
    password: Optional[str] = ""
    api_token: Optional[str] = ""
    proxy: Optional[str] = None


class YcAccountCreate(BaseModel):
    oauth_token: str
    folder_id: str
    proxy: Optional[str] = None
    label: Optional[str] = None


class YcAccountUpdate(BaseModel):
    enabled: Optional[bool] = None
    oauth_token: Optional[str] = None
    folder_id: Optional[str] = None
    proxy: Optional[str] = None
    label: Optional[str] = None


class YcSettingsUpdate(BaseModel):
    delay_min: Optional[int] = None
    delay_max: Optional[int] = None
    start_delay_max: Optional[int] = None
    stop_on_catch: Optional[bool] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    target_subnets: Optional[List[int]] = None
    zones: Optional[List[str]] = None
    proxy_required: Optional[bool] = None
    proxy_check_interval: Optional[int] = None
    log_level: Optional[str] = None


class YcManualObtainRequest(BaseModel):
    account_id: int
    zone: str


class CloudAccountCreate(BaseModel):
    key_id: str
    key_secret: str
    project_id: str
    proxy: Optional[str] = None
    label: Optional[str] = None
    # STS-токен временного ключа (только Cloud.ru Advanced; у постоянного ключа пусто)
    key_token: Optional[str] = None


class CloudAccountUpdate(BaseModel):
    enabled: Optional[bool] = None
    key_id: Optional[str] = None
    key_secret: Optional[str] = None
    project_id: Optional[str] = None
    proxy: Optional[str] = None
    label: Optional[str] = None
    key_token: Optional[str] = None


class CloudSettingsUpdate(BaseModel):
    delay_burst: Optional[float] = None
    delay_round: Optional[float] = None
    delay_delete: Optional[float] = None
    jitter: Optional[float] = None
    provision_timeout: Optional[int] = None
    provision_poll: Optional[float] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    proxy_required: Optional[bool] = None
    log_level: Optional[str] = None


class CloudAdvSettingsUpdate(CloudSettingsUpdate):
    mode: Optional[Literal["random", "targeted"]] = None


class CloudEvoSettingsUpdate(CloudSettingsUpdate):
    zones: Optional[List[str]] = None


class CloudWhitelistAdd(BaseModel):
    cidr: str


class WhitelistBulkAdd(BaseModel):
    cidrs: List[str] = Field(max_length=10000)


class CloudAdvManualObtainRequest(BaseModel):
    account_id: int
    ip_address: Optional[str] = None


class CloudEvoManualObtainRequest(BaseModel):
    account_id: int
    zone: str


class CloudBwAccountCreate(BaseModel):
    label: str = Field("", max_length=255)
    key_id: str = Field(min_length=1, max_length=255)
    key_secret: str = Field(min_length=1, max_length=500)
    project_id: str = Field(min_length=1, max_length=100)


class CloudBwAccountUpdate(BaseModel):
    label: Optional[str] = Field(None, max_length=255)
    key_id: Optional[str] = Field(None, max_length=255)
    key_secret: Optional[str] = Field(None, max_length=500)
    project_id: Optional[str] = Field(None, max_length=100)


class CloudBwIpCreate(BaseModel):
    account_id: int
    eip_id: str = Field(min_length=1, max_length=64)
    server_id: Optional[int] = None


class CloudBwSharedCreate(BaseModel):
    account_id: int
    bandwidth_id: str = Field(min_length=1, max_length=64)
    server_ids: List[int] = Field(default_factory=list, max_length=100)


class CloudBwScheduleRule(BaseModel):
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=1, le=24)
    mbps: int = Field(ge=1)


class CloudBwIpUpdate(BaseModel):
    # Выделенная полоса привязывается к одной ноде, общая — к нескольким
    server_id: Optional[int] = None
    server_ids: List[int] = Field(default_factory=list, max_length=100)
    mode: Literal["off", "schedule", "dynamic"]
    schedule: List[CloudBwScheduleRule] = Field(default_factory=list, max_length=24)
    # Сырые снапшоты метрик хранятся сутки — окно длиннее не из чего считать
    dyn_window_hours: int = Field(3, ge=1, le=24)
    dyn_reserve_percent: int = Field(20, ge=0, le=300)
    dyn_interval_hours: int = Field(1, ge=1, le=24)
    # Размеры (шаг и потолок 2 Гбит/с) проверяет сервис — с понятным текстом ошибки
    dyn_min_mbps: int = Field(100, ge=1)
    dyn_max_mbps: int = Field(2000, ge=1)


class ReceivedIp(BaseModel):
    ip: ipaddress.IPv4Address
    zone: str = Field(min_length=1, max_length=50)
    caught: bool = False


class ReceivedBatch(BaseModel):
    account_id: int
    items: List[ReceivedIp] = Field(max_length=1000)


class SelectelAccountCreate(BaseModel):
    username: str
    password: str
    domain: str
    project_id: str
    proxy: Optional[str] = None
    label: Optional[str] = None


class SelectelAccountUpdate(BaseModel):
    enabled: Optional[bool] = None
    username: Optional[str] = None
    password: Optional[str] = None
    domain: Optional[str] = None
    project_id: Optional[str] = None
    proxy: Optional[str] = None
    label: Optional[str] = None


class SelectelSettingsUpdate(BaseModel):
    delay_burst: Optional[float] = None
    delay_round: Optional[float] = None
    delay_delete: Optional[float] = None
    jitter: Optional[float] = None
    provision_timeout: Optional[int] = None
    provision_poll: Optional[float] = None
    regions: Optional[List[str]] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    proxy_required: Optional[bool] = None
    log_level: Optional[str] = None


class SelectelWhitelistAdd(BaseModel):
    cidr: str


class SelectelManualObtainRequest(BaseModel):
    account_id: int
    region: str


class SelectelMultiqueueRequest(BaseModel):
    auth_url: str
    project_id: str
    image_id: str
    auth_type: str = "password"
    username: Optional[str] = ""
    user_domain_name: Optional[str] = ""
    password: Optional[str] = ""
    api_token: Optional[str] = ""
    region: Optional[str] = ""
    proxy: Optional[str] = None


async def _finish_worker_rounds():
    """Перед остановкой панели на обновление воркеры доделывают начатый раунд
    (удаляют созданные промахи) и выходят сами, а не умирают с контейнером."""
    services = (ExtService, YcService, CloudEvoService, CloudAdvService, SelectelService)
    results = await asyncio.gather(
        *(svc.stop_worker(grace_seconds=WORKER_ROUND_FINISH_SECONDS) for svc in services),
        return_exceptions=True,
    )
    for svc, result in zip(services, results):
        if isinstance(result, Exception):
            logger.error(f"{svc.__name__}: workers did not stop cleanly before shutdown: {result}")


def _is_internal_request(request: Request) -> bool:
    """Служебные эндпоинты зовут только воркеры из самого контейнера бэкенда"""
    return is_direct_local_request(request)


def _register_received_stats_routes(router: APIRouter, prefix: str, service) -> None:
    """Статистика выданных облаком адресов: одни и те же маршруты у каждого провайдера"""

    @router.get(f"{prefix}/received-stats")
    async def get_received_stats(
        account_id: Optional[int] = Query(None),
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await service.get_received_stats(db, account_id=account_id)

    @router.get(f"{prefix}/received-ips")
    async def get_received_subnet_ips(
        subnet: str = Query(...),
        account_id: Optional[int] = Query(None),
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        try:
            network = ipaddress.IPv4Network(subnet, strict=False)
        except ValueError:
            raise HTTPException(400, "Некорректная подсеть")
        return await service.get_received_subnet_ips(db, str(network), account_id=account_id)

    @router.delete(f"{prefix}/received-ips")
    async def clear_received_ips(
        account_id: Optional[int] = Query(None),
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        count = await service.clear_received_ips(db, account_id=account_id)
        return {"success": True, "deleted": count}

    @router.post(f"{prefix}/internal/received-ips")
    async def save_received_ips_internal(
        request: Request,
        data: ReceivedBatch,
        db: AsyncSession = Depends(get_db),
    ):
        if not _is_internal_request(request):
            raise HTTPException(403, "Internal endpoint")
        items = [
            {"ip_address": str(item.ip), "zone": item.zone, "caught": item.caught}
            for item in data.items
        ]
        saved = await service.save_received_ips(db, data.account_id, items)
        return {"success": True, "saved": saved}


def _register_cloud_routes(
    router: APIRouter, prefix: str, name: str, module, service,
    settings_model: type[BaseModel], manual_model: type[BaseModel],
) -> None:
    """Cloud.ru Evolution и Advanced: один набор маршрутов. Редакции отличаются
    сервисом, настройками (зоны есть только у Evolution) и запросом ручного IP"""

    @router.on_event("startup")
    async def init_cloud_tables():
        await module.init_tables(engine)
        logger.info(f"{name} tables initialized")

    # ── ACCOUNTS ──

    @router.get(f"{prefix}/accounts")
    async def get_accounts(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await service.get_accounts(db)

    @router.post(f"{prefix}/accounts")
    async def create_account(
        data: CloudAccountCreate,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        try:
            kwargs = dict(
                key_id=data.key_id,
                key_secret=data.key_secret,
                project_id=data.project_id,
                label=data.label or "",
                proxy=data.proxy or "",
            )
            # key_token принимает только Advanced; передаём лишь когда задан
            token = getattr(data, "key_token", None)
            if token is not None:
                kwargs["key_token"] = token
            return await service.create_account(db, **kwargs)
        except Exception as e:
            raise HTTPException(500, str(e))

    @router.put(f"{prefix}/accounts/{{account_id}}")
    async def update_account(
        account_id: int, data: CloudAccountUpdate,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        result = await service.update_account(db, account_id, data.model_dump(exclude_unset=True))
        if not result:
            raise HTTPException(404, "Not found")
        return result

    @router.delete(f"{prefix}/accounts/{{account_id}}")
    async def delete_account(
        account_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        if not await service.delete_account(db, account_id):
            raise HTTPException(404, "Not found")
        return {"success": True}

    # ── SETTINGS ──

    @router.get(f"{prefix}/settings")
    async def get_settings(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await service.get_settings(db)

    @router.put(f"{prefix}/settings")
    async def update_settings(
        data: settings_model,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await service.update_settings(db, data.model_dump(exclude_unset=True))

    # ── WHITELIST ──

    @router.get(f"{prefix}/whitelist")
    async def get_whitelist(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await module.get_whitelist(db)

    @router.post(f"{prefix}/whitelist")
    async def add_whitelist(
        data: CloudWhitelistAdd,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        result = await module.add_whitelist_cidr(db, data.cidr)
        if not result.get("success"):
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.post(f"{prefix}/whitelist/bulk")
    async def add_whitelist_bulk(
        data: WhitelistBulkAdd,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await module.add_whitelist_cidrs(db, data.cidrs)

    @router.delete(f"{prefix}/whitelist")
    async def clear_whitelist(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await module.clear_whitelist(db)

    @router.delete(f"{prefix}/whitelist/{{subnet_id}}")
    async def delete_whitelist(
        subnet_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        result = await module.remove_whitelist_cidr(db, subnet_id)
        if not result.get("success"):
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    # ── WORKER CONTROL ──

    @router.post(f"{prefix}/start")
    async def start_worker(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        result = await service.start_worker(db)
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.post(f"{prefix}/stop")
    async def stop_worker(_user=Depends(verify_auth)):
        result = await service.stop_worker()
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.get(f"{prefix}/status")
    async def get_status(_user=Depends(verify_auth)):
        return service.get_status()

    @router.post(f"{prefix}/accounts/{{account_id}}/start")
    async def start_account(
        account_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        result = await service.start_account(db, account_id)
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.post(f"{prefix}/accounts/{{account_id}}/stop")
    async def stop_account(account_id: int, _user=Depends(verify_auth)):
        result = await service.stop_account(account_id)
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.get(f"{prefix}/worker/statuses")
    async def get_worker_statuses(_user=Depends(verify_auth)):
        return service.get_worker_statuses()

    @router.get(f"{prefix}/accounts/{{account_id}}/errors")
    async def get_account_errors(account_id: int, _user=Depends(verify_auth)):
        return service.get_account_errors(account_id)

    @router.delete(f"{prefix}/accounts/{{account_id}}/errors")
    async def clear_account_errors(account_id: int, _user=Depends(verify_auth)):
        service.clear_account_errors(account_id)
        return {"success": True}

    # ── CAUGHT IPS ──

    @router.get(f"{prefix}/caught-ips")
    async def get_caught_ips(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await service.get_caught_ips(db)

    @router.get(f"{prefix}/accounts/{{account_id}}/caught-ips")
    async def get_account_caught_ips(
        account_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await service.get_caught_ips(db, account_id=account_id)

    @router.delete(f"{prefix}/caught-ips")
    async def clear_caught_ips(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        count = await service.clear_caught_ips(db)
        return {"success": True, "deleted": count}

    @router.delete(f"{prefix}/caught-ips/{{ip_id}}")
    async def delete_caught_ip(
        ip_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        if not await service.delete_caught_ip(db, ip_id):
            raise HTTPException(404, "Not found")
        return {"success": True}

    @router.delete(f"{prefix}/accounts/{{account_id}}/caught-ips")
    async def clear_account_caught_ips(
        account_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        count = await service.clear_caught_ips(db, account_id=account_id)
        return {"success": True, "deleted": count}

    _register_received_stats_routes(router, prefix, service)

    # ── LOGS ──

    @router.get(f"{prefix}/logs")
    async def get_logs(lines: int = Query(100, ge=1, le=1000), _user=Depends(verify_auth)):
        return {"logs": service.get_logs(lines)}

    @router.delete(f"{prefix}/logs")
    async def clear_logs(_user=Depends(verify_auth)):
        service.clear_logs()
        return {"success": True}

    @router.get(f"{prefix}/logs/stream")
    async def stream_logs(_user=Depends(verify_auth)):
        async def event_generator():
            existing = service.get_logs(1000)
            for line in existing:
                yield f"data: {line}\n\n"
            yield "event: sync\ndata: ok\n\n"
            last_seq = service.get_log_seq()
            while True:
                new_logs, last_seq = service.get_logs_after(last_seq)
                for line in new_logs:
                    yield f"data: {line}\n\n"
                status = service.get_status()
                yield f"event: status\ndata: {status['status']}\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(
            event_generator(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # ── MANUAL OBTAIN ──

    @router.post(f"{prefix}/manual-obtain")
    async def manual_obtain(
        data: manual_model,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await service.manual_obtain_ip(db, **data.model_dump())

    # ── INTERNAL ──

    @router.post(f"{prefix}/internal/caught-ip")
    async def save_caught_ip_internal(
        request: Request,
        account_id: int = Query(...),
        ip_address: str = Query(...),
        eip_id: str = Query(""),
        zone: str = Query(""),
        db: AsyncSession = Depends(get_db),
    ):
        if not _is_internal_request(request):
            raise HTTPException(403, "Internal endpoint")
        return await service.save_caught_ip(db, account_id, ip_address, eip_id, zone)

    logger.info(f"{name} routes registered")


CLOUD_BW_HISTORY_LIMIT = 100


def _register_cloud_bw_routes(router: APIRouter) -> None:
    """Ширина канала публичных IP и общих полос Cloud.ru Advanced: расписание и динамика по нагрузке нод"""
    service = cloudru_bandwidth.CloudBwService

    async def guarded(awaitable):
        try:
            return await awaitable
        except cloudru_bandwidth.CloudBwNotFound as e:
            raise HTTPException(404, str(e))
        except cloudru_bandwidth.CloudBwError as e:
            raise HTTPException(400, str(e))

    @router.on_event("startup")
    async def start_cloud_bw():
        try:
            await cloudru_bandwidth.init_tables(engine)
        except Exception as e:
            logger.error(f"Cloud.ru bandwidth tables init failed: {e}")
        await cloudru_bandwidth.start_scheduler()

    @router.on_event("shutdown")
    async def stop_cloud_bw():
        await cloudru_bandwidth.stop_scheduler()

    # ── ACCOUNTS ──

    @router.get("/cloud/bw/accounts")
    async def cloud_bw_accounts(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await service.list_accounts(db)

    @router.post("/cloud/bw/accounts")
    async def cloud_bw_create_account(
        data: CloudBwAccountCreate,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await guarded(service.create_account(
            db, data.label.strip(), data.key_id.strip(), data.key_secret.strip(), data.project_id.strip(),
        ))

    @router.put("/cloud/bw/accounts/{account_id}")
    async def cloud_bw_update_account(
        account_id: int, data: CloudBwAccountUpdate,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        changes = {key: value.strip() for key, value in data.model_dump(exclude_unset=True).items() if value is not None}
        return await guarded(service.update_account(db, account_id, changes))

    @router.delete("/cloud/bw/accounts/{account_id}")
    async def cloud_bw_delete_account(
        account_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        await guarded(service.delete_account(db, account_id))
        return {"success": True}

    @router.get("/cloud/bw/accounts/{account_id}/eips")
    async def cloud_bw_cloud_eips(
        account_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await guarded(service.list_cloud_eips(db, account_id))

    # ── IPS ──

    @router.get("/cloud/bw/ips")
    async def cloud_bw_ips(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await service.list_ips(db)

    @router.post("/cloud/bw/ips")
    async def cloud_bw_add_ip(
        data: CloudBwIpCreate,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await guarded(service.add_ip(db, data.account_id, data.eip_id.strip(), data.server_id))

    @router.post("/cloud/bw/shared")
    async def cloud_bw_add_shared(
        data: CloudBwSharedCreate,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await guarded(service.add_shared(db, data.account_id, data.bandwidth_id.strip(), data.server_ids))

    @router.put("/cloud/bw/ips/{ip_id}")
    async def cloud_bw_update_ip(
        ip_id: int, data: CloudBwIpUpdate,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        await guarded(service.update_ip(db, ip_id, data.model_dump()))
        # Новое правило применяется сразу, а не в начале следующего часа
        await cloudru_bandwidth.evaluate_ip(ip_id, force_regular=True)
        return await guarded(service.get_ip(db, ip_id))

    @router.post("/cloud/bw/ips/{ip_id}/apply")
    async def cloud_bw_apply_ip(
        ip_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        await guarded(service.get_ip(db, ip_id))
        await db.commit()
        await cloudru_bandwidth.evaluate_ip(ip_id, force_regular=True)
        return await guarded(service.get_ip(db, ip_id))

    @router.delete("/cloud/bw/ips/{ip_id}")
    async def cloud_bw_delete_ip(
        ip_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        await guarded(service.delete_ip(db, ip_id))
        return {"success": True}

    @router.get("/cloud/bw/ips/{ip_id}/history")
    async def cloud_bw_history(
        ip_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await service.get_history(db, ip_id, CLOUD_BW_HISTORY_LIMIT)

    @router.get("/cloud/bw/usage")
    async def cloud_bw_usage(
        period: Literal["day", "week", "30d", "month", "prev_month"] = Query("month"),
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await service.get_usage(db, cloudru_bandwidth.UsagePeriod(period))

    logger.info("Cloud.ru bandwidth routes registered")


def register_routes(router: APIRouter):
    """Register all routes to the router"""

    register_shutdown_hook(_finish_worker_rounds)

    @router.on_event("startup")
    async def init_ext():
        await init_tables(engine)
        logger.info("Extension tables initialized")
    
    # ─────────────────────────────────────────────────────────────────
    # ACCOUNTS
    # ─────────────────────────────────────────────────────────────────
    
    @router.get("/ext/accounts")
    async def get_accounts(
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        return await ExtService.get_accounts(db)
    
    @router.post("/ext/accounts")
    async def create_account(
        data: AccountCreate,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        try:
            return await ExtService.create_account(db, data.email, data.password, data.proxy)
        except Exception as e:
            if "UNIQUE constraint" in str(e):
                raise HTTPException(400, "Account exists")
            raise HTTPException(500, str(e))
    
    @router.put("/ext/accounts/{account_id}")
    async def update_account(
        account_id: int,
        data: AccountUpdate,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        update_data = data.model_dump(exclude_unset=True)
        result = await ExtService.update_account(db, account_id, update_data)
        if not result:
            raise HTTPException(404, "Not found")
        return result
    
    @router.delete("/ext/accounts/{account_id}")
    async def delete_account(
        account_id: int,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        success = await ExtService.delete_account(db, account_id)
        if not success:
            raise HTTPException(404, "Not found")
        return {"success": True}
    
    # ─────────────────────────────────────────────────────────────────
    # PROJECTS
    # ─────────────────────────────────────────────────────────────────
    
    @router.get("/ext/accounts/{account_id}/projects")
    async def get_projects(
        account_id: int,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        return await ExtService.get_projects(db, account_id)
    
    @router.get("/ext/projects")
    async def get_all_projects(
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        return await ExtService.get_all_projects(db)
    
    @router.post("/ext/accounts/{account_id}/projects")
    async def create_project(
        account_id: int,
        data: ProjectCreate,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        try:
            return await ExtService.create_project(
                db, account_id,
                data.project_id, data.project_name
            )
        except Exception as e:
            raise HTTPException(500, str(e))
    
    @router.put("/ext/projects/{project_id}")
    async def update_project(
        project_id: int,
        data: ProjectUpdate,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        update_data = data.model_dump(exclude_unset=True)
        result = await ExtService.update_project(db, project_id, update_data)
        if not result:
            raise HTTPException(404, "Not found")
        return result
    
    @router.delete("/ext/projects/{project_id}")
    async def delete_project(
        project_id: int,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        success = await ExtService.delete_project(db, project_id)
        if not success:
            raise HTTPException(404, "Not found")
        return {"success": True}
    
    # ─────────────────────────────────────────────────────────────────
    # SETTINGS
    # ─────────────────────────────────────────────────────────────────
    
    @router.get("/ext/settings")
    async def get_settings(
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        return await ExtService.get_settings(db)
    
    @router.put("/ext/settings")
    async def update_settings(
        data: SettingsUpdate,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        update_data = data.model_dump(exclude_unset=True)
        return await ExtService.update_settings(db, update_data)
    
    @router.get("/ext/subnets")
    async def get_subnets(
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        all_subs = await get_all_subnets(db)
        custom = await get_custom_subnets(db)
        subnet_names = {k: get_subnet_name(k) for k in all_subs}
        return {"subnets": all_subs, "custom_subnets": custom, "builtin_ids": [k for k in AVAILABLE_SUBNETS], "subnet_names": subnet_names}
    
    @router.post("/ext/subnets")
    async def create_subnet(
        data: dict,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        subnet_id = data.get("id")
        cidr = data.get("cidr", "")
        if subnet_id is None or not cidr:
            raise HTTPException(400, "id and cidr are required")
        result = await add_custom_subnet(db, int(subnet_id), cidr)
        if not result["success"]:
            raise HTTPException(400, result["error"])
        return result
    
    @router.delete("/ext/subnets/{subnet_id}")
    async def delete_subnet(
        subnet_id: int,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        result = await remove_custom_subnet(db, subnet_id)
        if not result["success"]:
            raise HTTPException(400, result["error"])
        return result

    # ─────────────────────────────────────────────────────────────────
    # PRECISE CIDR FILTER (узкий целевой диапазон)
    # ─────────────────────────────────────────────────────────────────

    @router.get("/ext/precise-cidrs")
    async def get_precise_cidr_list(
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        return await get_precise_cidrs(db)

    @router.post("/ext/precise-cidrs")
    async def add_precise_cidr_route(
        data: PreciseCidrAdd,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        result = await add_precise_cidr(db, data.cidr)
        if not result.get("success"):
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.delete("/ext/precise-cidrs/{cidr_id}")
    async def delete_precise_cidr_route(
        cidr_id: int,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        result = await remove_precise_cidr(db, cidr_id)
        if not result.get("success"):
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    # ─────────────────────────────────────────────────────────────────
    # WORKER CONTROL
    # ─────────────────────────────────────────────────────────────────
    
    @router.post("/ext/start")
    async def start_worker(
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        result = await ExtService.start_worker(db)
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result
    
    @router.post("/ext/stop")
    async def stop_worker(_user = Depends(verify_auth)):
        result = await ExtService.stop_worker()
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result
    
    @router.get("/ext/status")
    async def get_status(_user = Depends(verify_auth)):
        return ExtService.get_status()
    
    @router.get("/ext/logs")
    async def get_logs(
        lines: int = Query(100, ge=1, le=1000),
        _user = Depends(verify_auth)
    ):
        return {"logs": ExtService.get_logs(lines)}
    
    @router.delete("/ext/logs")
    async def clear_logs(_user = Depends(verify_auth)):
        ExtService.clear_logs()
        return {"success": True}
    
    @router.get("/ext/logs/stream")
    async def stream_logs(_user = Depends(verify_auth)):
        async def event_generator():
            # Send all existing logs
            existing = ExtService.get_logs(1000)
            for log in existing:
                yield f"data: {log}\n\n"
            
            # Signal initial batch complete
            yield "event: sync\ndata: ok\n\n"
            
            # Track position using monotonic sequence number
            last_seq = ExtService.get_log_seq()
            
            while True:
                new_logs, last_seq = ExtService.get_logs_after(last_seq)
                for log in new_logs:
                    yield f"data: {log}\n\n"
                
                status = ExtService.get_status()
                yield f"event: status\ndata: {status['status']}\n\n"
                
                await asyncio.sleep(0.5)
        
        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
        )
    
    # ─────────────────────────────────────────────────────────────────
    # PER-ACCOUNT WORKER CONTROL
    # ─────────────────────────────────────────────────────────────────
    
    @router.post("/ext/accounts/{account_id}/start")
    async def start_account_worker(
        account_id: int,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        result = await ExtService.start_account(db, account_id)
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result
    
    @router.post("/ext/accounts/{account_id}/stop")
    async def stop_account_worker(
        account_id: int,
        _user = Depends(verify_auth)
    ):
        result = await ExtService.stop_account(account_id)
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result
    
    @router.get("/ext/worker/statuses")
    async def get_worker_statuses(_user = Depends(verify_auth)):
        return ExtService.get_worker_statuses()
    
    @router.get("/ext/accounts/{account_id}/errors")
    async def get_account_errors(
        account_id: int,
        _user = Depends(verify_auth)
    ):
        return ExtService.get_account_errors(account_id)
    
    @router.delete("/ext/accounts/{account_id}/errors")
    async def clear_account_errors(
        account_id: int,
        _user = Depends(verify_auth)
    ):
        ExtService.clear_account_errors(account_id)
        return {"success": True}
    
    # ─────────────────────────────────────────────────────────────────
    # CAUGHT IPS
    # ─────────────────────────────────────────────────────────────────
    
    @router.get("/ext/caught-ips")
    async def get_all_caught_ips(
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        return await ExtService.get_caught_ips(db)
    
    @router.get("/ext/accounts/{account_id}/caught-ips")
    async def get_account_caught_ips(
        account_id: int,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        return await ExtService.get_caught_ips(db, account_id=account_id)
    
    @router.get("/ext/projects/{project_id}/caught-ips")
    async def get_project_caught_ips(
        project_id: int,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        return await ExtService.get_caught_ips(db, project_id=project_id)
    
    @router.delete("/ext/caught-ips")
    async def clear_all_caught_ips(
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        count = await ExtService.clear_caught_ips(db)
        return {"success": True, "deleted": count}
    
    @router.delete("/ext/accounts/{account_id}/caught-ips")
    async def clear_account_caught_ips(
        account_id: int,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        count = await ExtService.clear_caught_ips(db, account_id=account_id)
        return {"success": True, "deleted": count}
    
    @router.delete("/ext/projects/{project_id}/caught-ips")
    async def clear_project_caught_ips(
        project_id: int,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        count = await ExtService.clear_caught_ips(db, project_id=project_id)
        return {"success": True, "deleted": count}
    
    @router.delete("/ext/caught-ips/{ip_id}")
    async def delete_caught_ip(
        ip_id: int,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        success = await ExtService.delete_caught_ip(db, ip_id)
        if not success:
            raise HTTPException(404, "Not found")
        return {"success": True}

    @router.get("/ext/accounts/{account_id}/cloud-ips")
    async def get_account_cloud_ips(
        account_id: int,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        result = await ExtService.get_account_cloud_ips(db, account_id)
        if result is None:
            raise HTTPException(404, "Not found")
        return result

    # ─────────────────────────────────────────────────────────────────
    # MANUAL IP OBTAINMENT
    # ─────────────────────────────────────────────────────────────────

    @router.post("/ext/manual-obtain")
    async def manual_obtain_ip(
        data: ManualObtainRequest,
        db: AsyncSession = Depends(get_db),
        _user = Depends(verify_auth)
    ):
        result = await ExtService.manual_obtain_ip(
            db, data.account_id, data.project_db_id,
            data.subnet_key, data.custom_subnet_name
        )
        return result

    # ─────────────────────────────────────────────────────────────────
    # MULTIQUEUE
    # ─────────────────────────────────────────────────────────────────

    @router.post("/ext/multiqueue-enable")
    async def multiqueue_enable(
        data: MultiqueueRequest,
        _user = Depends(verify_auth)
    ):
        return await enable_multiqueue(
            auth_url=data.auth_url,
            project_id=data.project_id,
            image_id=data.image_id,
            auth_type=data.auth_type,
            username=data.username or "",
            user_domain_name=data.user_domain_name or "users",
            password=data.password or "",
            api_token=data.api_token or "",
            proxy=(data.proxy or "").strip(),
        )

    # ─────────────────────────────────────────────────────────────────
    # INTERNAL (for worker callback, no auth required)
    # ─────────────────────────────────────────────────────────────────

    @router.post("/ext/internal/caught-ip")
    async def save_caught_ip_internal(
        request: Request,
        account_id: int = Query(...),
        project_id: int = Query(...),
        ip_address: str = Query(...),
        subnet_name: str = Query(...),
        db: AsyncSession = Depends(get_db)
    ):
        """Save caught IP from worker (internal only, localhost/Docker network)"""
        if not _is_internal_request(request):
            raise HTTPException(403, "Internal endpoint")
        return await ExtService.save_caught_ip(db, account_id, project_id, ip_address, subnet_name)

    # ─────────────────────────────────────────────────────────────────
    # RECEIVED IPS STATS
    # ─────────────────────────────────────────────────────────────────

    _register_received_stats_routes(router, "/ext", ExtService)

    # ═════════════════════════════════════════════════════════════════
    # YANDEX CLOUD ROUTES
    # ═════════════════════════════════════════════════════════════════

    @router.on_event("startup")
    async def init_yc():
        await yc_init_tables(engine)
        logger.info("YC tables initialized")

    # ── YC ACCOUNTS ──

    @router.get("/yc/accounts")
    async def yc_get_accounts(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await YcService.get_accounts(db)

    @router.post("/yc/accounts")
    async def yc_create_account(data: YcAccountCreate, db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        try:
            return await YcService.create_account(db, data.oauth_token, data.folder_id, data.proxy, data.label)
        except Exception as e:
            raise HTTPException(500, str(e))

    @router.put("/yc/accounts/{account_id}")
    async def yc_update_account(account_id: int, data: YcAccountUpdate, db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        update_data = data.model_dump(exclude_unset=True)
        result = await YcService.update_account(db, account_id, update_data)
        if not result:
            raise HTTPException(404, "Not found")
        return result

    @router.delete("/yc/accounts/{account_id}")
    async def yc_delete_account(account_id: int, db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        success = await YcService.delete_account(db, account_id)
        if not success:
            raise HTTPException(404, "Not found")
        return {"success": True}

    # ── YC SETTINGS ──

    @router.get("/yc/settings")
    async def yc_get_settings(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await YcService.get_settings(db)

    @router.put("/yc/settings")
    async def yc_update_settings(data: YcSettingsUpdate, db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        update_data = data.model_dump(exclude_unset=True)
        return await YcService.update_settings(db, update_data)

    @router.get("/yc/subnets")
    async def yc_get_subnets(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        all_subs = await yc_get_all_subnets(db)
        custom = await yandex.get_custom_subnets(db)
        builtin = list(yandex.YC_BUILTIN_SUBNETS)
        return {"subnets": all_subs, "custom_subnets": custom, "builtin": builtin}

    @router.post("/yc/subnets")
    async def yc_create_subnet(data: dict, db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        cidr = data.get("cidr", "")
        if not cidr:
            raise HTTPException(400, "cidr is required")
        result = await yc_add_custom_subnet(db, cidr)
        if not result["success"]:
            raise HTTPException(400, result["error"])
        return result

    @router.delete("/yc/subnets/{subnet_id}")
    async def yc_delete_subnet(subnet_id: int, db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        result = await yc_remove_custom_subnet(db, subnet_id)
        if not result["success"]:
            raise HTTPException(400, result["error"])
        return result

    # ── YC WORKER CONTROL ──

    @router.post("/yc/start")
    async def yc_start_worker(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        result = await YcService.start_worker(db)
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.post("/yc/stop")
    async def yc_stop_worker(_user=Depends(verify_auth)):
        result = await YcService.stop_worker()
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.get("/yc/status")
    async def yc_get_status(_user=Depends(verify_auth)):
        return YcService.get_status()

    @router.post("/yc/accounts/{account_id}/start")
    async def yc_start_account(account_id: int, db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        result = await YcService.start_account(db, account_id)
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.post("/yc/accounts/{account_id}/stop")
    async def yc_stop_account(account_id: int, _user=Depends(verify_auth)):
        result = await YcService.stop_account(account_id)
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.get("/yc/worker/statuses")
    async def yc_get_worker_statuses(_user=Depends(verify_auth)):
        return YcService.get_worker_statuses()

    @router.get("/yc/accounts/{account_id}/errors")
    async def yc_get_account_errors(account_id: int, _user=Depends(verify_auth)):
        return YcService.get_account_errors(account_id)

    @router.delete("/yc/accounts/{account_id}/errors")
    async def yc_clear_account_errors(account_id: int, _user=Depends(verify_auth)):
        YcService.clear_account_errors(account_id)
        return {"success": True}

    # ── YC CAUGHT IPS ──

    @router.get("/yc/caught-ips")
    async def yc_get_caught_ips(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await YcService.get_caught_ips(db)

    @router.get("/yc/accounts/{account_id}/caught-ips")
    async def yc_get_account_caught_ips(account_id: int, db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await YcService.get_caught_ips(db, account_id=account_id)

    @router.delete("/yc/caught-ips")
    async def yc_clear_caught_ips(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        count = await YcService.clear_caught_ips(db)
        return {"success": True, "deleted": count}

    @router.delete("/yc/caught-ips/{ip_id}")
    async def yc_delete_caught_ip(ip_id: int, db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        success = await YcService.delete_caught_ip(db, ip_id)
        if not success:
            raise HTTPException(404, "Not found")
        return {"success": True}

    @router.delete("/yc/accounts/{account_id}/caught-ips")
    async def yc_clear_account_caught_ips(account_id: int, db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        count = await YcService.clear_caught_ips(db, account_id=account_id)
        return {"success": True, "deleted": count}

    # ── YC LOGS ──

    @router.get("/yc/logs")
    async def yc_get_logs(lines: int = Query(100, ge=1, le=1000), _user=Depends(verify_auth)):
        return {"logs": YcService.get_logs(lines)}

    @router.delete("/yc/logs")
    async def yc_clear_logs(_user=Depends(verify_auth)):
        YcService.clear_logs()
        return {"success": True}

    @router.get("/yc/logs/stream")
    async def yc_stream_logs(_user=Depends(verify_auth)):
        async def event_generator():
            existing = YcService.get_logs(1000)
            for log in existing:
                yield f"data: {log}\n\n"
            yield "event: sync\ndata: ok\n\n"
            last_seq = YcService.get_log_seq()
            while True:
                new_logs, last_seq = YcService.get_logs_after(last_seq)
                for log in new_logs:
                    yield f"data: {log}\n\n"
                status = YcService.get_status()
                yield f"event: status\ndata: {status['status']}\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(event_generator(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    # ── YC MANUAL OBTAIN ──

    @router.post("/yc/manual-obtain")
    async def yc_manual_obtain(data: YcManualObtainRequest, db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await YcService.manual_obtain_ip(db, data.account_id, data.zone)

    # ── YC INTERNAL ──

    @router.post("/yc/internal/caught-ip")
    async def yc_save_caught_ip_internal(
        request: Request,
        account_id: int = Query(...),
        ip_address: str = Query(...),
        address_id: str = Query(""),
        zone: str = Query(""),
        db: AsyncSession = Depends(get_db)
    ):
        if not _is_internal_request(request):
            raise HTTPException(403, "Internal endpoint")
        return await YcService.save_caught_ip(db, account_id, ip_address, address_id, zone)

    # ── YC RECEIVED IPS STATS ──

    _register_received_stats_routes(router, "/yc", YcService)

    logger.info("YC routes registered")

    # ═════════════════════════════════════════════════════════════════
    # CLOUD.RU ROUTES (EVOLUTION / ADVANCED)
    # ═════════════════════════════════════════════════════════════════

    _register_cloud_routes(
        router, "/cloud/evo", "Cloud.ru EVO", cloudru_evo, CloudEvoService,
        CloudEvoSettingsUpdate, CloudEvoManualObtainRequest,
    )
    _register_cloud_routes(
        router, "/cloud/adv", "Cloud.ru ADV", cloudru_adv, CloudAdvService,
        CloudAdvSettingsUpdate, CloudAdvManualObtainRequest,
    )
    _register_cloud_bw_routes(router)

    # ═════════════════════════════════════════════════════════════════
    # SELECTEL ROUTES
    # ═════════════════════════════════════════════════════════════════

    @router.on_event("startup")
    async def init_selectel():
        await selectel_init_tables(engine)
        logger.info("Selectel tables initialized")

    # ── SELECTEL ACCOUNTS ──

    @router.get("/selectel/accounts")
    async def selectel_get_accounts(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await SelectelService.get_accounts(db)

    @router.post("/selectel/accounts")
    async def selectel_create_account(
        data: SelectelAccountCreate,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        try:
            return await SelectelService.create_account(
                db,
                username=data.username,
                password=data.password,
                domain=data.domain,
                project_id=data.project_id,
                label=data.label or "",
                proxy=data.proxy or "",
            )
        except Exception as e:
            raise HTTPException(500, str(e))

    @router.put("/selectel/accounts/{account_id}")
    async def selectel_update_account(
        account_id: int, data: SelectelAccountUpdate,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        update_data = data.model_dump(exclude_unset=True)
        result = await SelectelService.update_account(db, account_id, update_data)
        if not result:
            raise HTTPException(404, "Not found")
        return result

    @router.delete("/selectel/accounts/{account_id}")
    async def selectel_delete_account(
        account_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        success = await SelectelService.delete_account(db, account_id)
        if not success:
            raise HTTPException(404, "Not found")
        return {"success": True}

    # ── SELECTEL SETTINGS ──

    @router.get("/selectel/settings")
    async def selectel_get_settings(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await SelectelService.get_settings(db)

    @router.put("/selectel/settings")
    async def selectel_update_settings(
        data: SelectelSettingsUpdate,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        update_data = data.model_dump(exclude_unset=True)
        return await SelectelService.update_settings(db, update_data)

    # ── SELECTEL WHITELIST ──

    @router.get("/selectel/whitelist")
    async def selectel_get_whitelist_list(
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await selectel_get_whitelist(db)

    @router.post("/selectel/whitelist")
    async def selectel_add_whitelist(
        data: SelectelWhitelistAdd,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        result = await selectel_add_whitelist_cidr(db, data.cidr)
        if not result.get("success"):
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.post("/selectel/whitelist/bulk")
    async def selectel_add_whitelist_bulk(
        data: WhitelistBulkAdd,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await selectel_add_whitelist_cidrs(db, data.cidrs)

    @router.delete("/selectel/whitelist")
    async def selectel_clear_whitelist_all(
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await selectel_clear_whitelist(db)

    @router.delete("/selectel/whitelist/{subnet_id}")
    async def selectel_delete_whitelist(
        subnet_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        result = await selectel_remove_whitelist_cidr(db, subnet_id)
        if not result.get("success"):
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    # ── SELECTEL WORKER CONTROL ──

    @router.post("/selectel/start")
    async def selectel_start_worker(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        result = await SelectelService.start_worker(db)
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.post("/selectel/stop")
    async def selectel_stop_worker(_user=Depends(verify_auth)):
        result = await SelectelService.stop_worker()
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.get("/selectel/status")
    async def selectel_get_status(_user=Depends(verify_auth)):
        return SelectelService.get_status()

    @router.post("/selectel/accounts/{account_id}/start")
    async def selectel_start_account(
        account_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        result = await SelectelService.start_account(db, account_id)
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.post("/selectel/accounts/{account_id}/stop")
    async def selectel_stop_account(account_id: int, _user=Depends(verify_auth)):
        result = await SelectelService.stop_account(account_id)
        if not result["success"]:
            raise HTTPException(400, result.get("error", "Failed"))
        return result

    @router.get("/selectel/worker/statuses")
    async def selectel_get_worker_statuses(_user=Depends(verify_auth)):
        return SelectelService.get_worker_statuses()

    @router.get("/selectel/accounts/{account_id}/errors")
    async def selectel_get_account_errors(account_id: int, _user=Depends(verify_auth)):
        return SelectelService.get_account_errors(account_id)

    @router.delete("/selectel/accounts/{account_id}/errors")
    async def selectel_clear_account_errors(account_id: int, _user=Depends(verify_auth)):
        SelectelService.clear_account_errors(account_id)
        return {"success": True}

    # ── SELECTEL CAUGHT IPS ──

    @router.get("/selectel/caught-ips")
    async def selectel_get_caught_ips(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        return await SelectelService.get_caught_ips(db)

    @router.get("/selectel/accounts/{account_id}/caught-ips")
    async def selectel_get_account_caught_ips(
        account_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await SelectelService.get_caught_ips(db, account_id=account_id)

    @router.delete("/selectel/caught-ips")
    async def selectel_clear_caught_ips(db: AsyncSession = Depends(get_db), _user=Depends(verify_auth)):
        count = await SelectelService.clear_caught_ips(db)
        return {"success": True, "deleted": count}

    @router.delete("/selectel/caught-ips/{ip_id}")
    async def selectel_delete_caught_ip(
        ip_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        success = await SelectelService.delete_caught_ip(db, ip_id)
        if not success:
            raise HTTPException(404, "Not found")
        return {"success": True}

    @router.delete("/selectel/accounts/{account_id}/caught-ips")
    async def selectel_clear_account_caught_ips(
        account_id: int,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        count = await SelectelService.clear_caught_ips(db, account_id=account_id)
        return {"success": True, "deleted": count}

    # ── SELECTEL LOGS ──

    @router.get("/selectel/logs")
    async def selectel_get_logs(lines: int = Query(100, ge=1, le=1000), _user=Depends(verify_auth)):
        return {"logs": SelectelService.get_logs(lines)}

    @router.delete("/selectel/logs")
    async def selectel_clear_logs(_user=Depends(verify_auth)):
        SelectelService.clear_logs()
        return {"success": True}

    @router.get("/selectel/logs/stream")
    async def selectel_stream_logs(_user=Depends(verify_auth)):
        async def event_generator():
            existing = SelectelService.get_logs(1000)
            for line in existing:
                yield f"data: {line}\n\n"
            yield "event: sync\ndata: ok\n\n"
            last_seq = SelectelService.get_log_seq()
            while True:
                new_logs, last_seq = SelectelService.get_logs_after(last_seq)
                for line in new_logs:
                    yield f"data: {line}\n\n"
                status = SelectelService.get_status()
                yield f"event: status\ndata: {status['status']}\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(
            event_generator(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # ── SELECTEL MANUAL OBTAIN ──

    @router.post("/selectel/manual-obtain")
    async def selectel_manual_obtain(
        data: SelectelManualObtainRequest,
        db: AsyncSession = Depends(get_db), _user=Depends(verify_auth),
    ):
        return await SelectelService.manual_obtain_ip(db, data.account_id, data.region)

    # ── SELECTEL MULTIQUEUE ──

    @router.post("/selectel/multiqueue-enable")
    async def selectel_multiqueue_enable(
        data: SelectelMultiqueueRequest,
        _user=Depends(verify_auth),
    ):
        return await selectel_enable_multiqueue(
            auth_url=data.auth_url,
            project_id=data.project_id,
            image_id=data.image_id,
            auth_type=data.auth_type,
            username=data.username or "",
            user_domain_name=data.user_domain_name or "",
            password=data.password or "",
            api_token=data.api_token or "",
            region=data.region or "",
            proxy=(data.proxy or "").strip(),
        )

    # ── SELECTEL INTERNAL ──

    @router.post("/selectel/internal/caught-ip")
    async def selectel_save_caught_ip_internal(
        request: Request,
        account_id: int = Query(...),
        ip_address: str = Query(...),
        fip_id: str = Query(""),
        region: str = Query(""),
        db: AsyncSession = Depends(get_db),
    ):
        if not _is_internal_request(request):
            raise HTTPException(403, "Internal endpoint")
        return await SelectelService.save_caught_ip(db, account_id, ip_address, fip_id, region)

    # ── SELECTEL RECEIVED IPS STATS ──

    _register_received_stats_routes(router, "/selectel", SelectelService)

    logger.info("Selectel routes registered")

    logger.info(f"Extension routes registered: {len([r for r in router.routes])}")


router = APIRouter()
register_routes(router)
