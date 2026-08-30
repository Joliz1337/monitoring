import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import BillingServer, BillingSettings
from app.auth import verify_auth
from app.routers.servers import parse_flexible_date
from app.services.cloud_billing import (
    PROVIDERS,
    CloudAuthError,
    CloudBillingError,
    sync_cloud_balance,
)

router = APIRouter(prefix="/billing", tags=["billing"])


VALID_BILLING_TYPES = ("monthly", "resource", "cloud")

# Вкладки браузера, открытые до обновления панели, ещё шлют старый тип
LEGACY_TYPE_PROVIDERS = {"yandex_cloud": "yandex_cloud"}


class BillingServerCreate(BaseModel):
    name: str
    billing_type: str  # 'monthly' | 'resource' | 'cloud'
    paid_days: Optional[int] = None
    paid_until: Optional[str] = None
    monthly_cost: Optional[float] = None
    account_balance: Optional[float] = None
    currency: Optional[str] = "USD"
    notes: Optional[str] = None
    folder: Optional[str] = None
    cloud_provider: Optional[str] = None
    cloud_credential: Optional[str] = None
    cloud_account_id: Optional[str] = None
    cloud_balance_threshold: Optional[float] = 0


class BillingServerUpdate(BaseModel):
    name: Optional[str] = None
    billing_type: Optional[str] = None
    paid_until: Optional[str] = None
    monthly_cost: Optional[float] = None
    account_balance: Optional[float] = None
    currency: Optional[str] = None
    notes: Optional[str] = None
    folder: Optional[str] = None
    cloud_provider: Optional[str] = None
    cloud_credential: Optional[str] = None
    cloud_account_id: Optional[str] = None
    cloud_balance_threshold: Optional[float] = None


class ExtendRequest(BaseModel):
    days: int


class TopupRequest(BaseModel):
    amount: float


class BillingSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    notify_days: Optional[list[int]] = None
    check_interval_minutes: Optional[int] = None


def _normalize_type(billing_type: str) -> tuple[str, Optional[str]]:
    """Тип биллинга + провайдер, с поддержкой старого типа 'yandex_cloud'."""
    provider = LEGACY_TYPE_PROVIDERS.get(billing_type)
    if provider:
        return "cloud", provider
    if billing_type not in VALID_BILLING_TYPES:
        raise HTTPException(400, f"billing_type must be one of {VALID_BILLING_TYPES}")
    return billing_type, None


def _validate_provider(provider: Optional[str]) -> None:
    if provider not in PROVIDERS:
        raise HTTPException(400, f"cloud_provider must be one of {tuple(PROVIDERS)}")


def _compute_paid_until_resource(monthly_cost: float, balance: float, from_time: datetime) -> Optional[datetime]:
    if monthly_cost <= 0 or balance <= 0:
        return from_time
    days_left = (balance / monthly_cost) * 30
    return from_time + timedelta(days=days_left)


def _compute_live_balance(s: BillingServer, now: datetime) -> tuple[float | None, datetime | None, float | None]:
    # Облачный баланс обновляется только синхронизацией с провайдером,
    # линейно уменьшать его между синками нельзя — расход неравномерный
    if s.billing_type == "cloud":
        days_left = None
        if s.cloud_daily_cost and s.cloud_daily_cost > 0 and s.account_balance is not None:
            threshold = s.cloud_balance_threshold or 0
            usable = s.account_balance - threshold
            days_left = max(0.0, usable / s.cloud_daily_cost) if usable > 0 else 0.0
        return s.account_balance, s.paid_until, days_left

    if (
        s.billing_type != "resource"
        or not s.monthly_cost
        or s.monthly_cost <= 0
        or s.account_balance is None
        or not s.balance_updated_at
    ):
        return s.account_balance, s.paid_until, None

    updated = s.balance_updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)

    elapsed_days = (now - updated).total_seconds() / 86400
    daily_cost = s.monthly_cost / 30
    consumed = elapsed_days * daily_cost
    live_balance = max(0.0, s.account_balance - consumed)

    if live_balance > 0:
        remaining = live_balance / daily_cost
        paid_until = now + timedelta(days=remaining)
    else:
        remaining = 0.0
        paid_until = now

    return live_balance, paid_until, remaining


def _server_to_dict(s: BillingServer) -> dict:
    now = datetime.now(timezone.utc)

    live_balance, paid_until, resource_days = _compute_live_balance(s, now)

    days_left = None
    if resource_days is not None:
        days_left = resource_days
    elif paid_until:
        if paid_until.tzinfo is None:
            paid_until = paid_until.replace(tzinfo=timezone.utc)
        days_left = max(0, (paid_until - now).total_seconds() / 86400)

    return {
        "id": s.id,
        "name": s.name,
        "billing_type": s.billing_type,
        "paid_until": paid_until.isoformat() if paid_until else None,
        "days_left": round(days_left, 2) if days_left is not None else None,
        "monthly_cost": s.monthly_cost,
        "account_balance": round(live_balance, 2) if live_balance is not None else s.account_balance,
        "balance_updated_at": s.balance_updated_at.isoformat() if s.balance_updated_at else None,
        "currency": s.currency or "USD",
        "notes": s.notes,
        "folder": s.folder,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "cloud_provider": s.cloud_provider,
        "cloud_account_id": s.cloud_account_id,
        "cloud_balance_threshold": s.cloud_balance_threshold,
        "cloud_daily_cost": s.cloud_daily_cost,
        "cloud_last_sync_at": s.cloud_last_sync_at.isoformat() if s.cloud_last_sync_at else None,
        "cloud_last_error": s.cloud_last_error,
        "has_cloud_credential": bool(s.cloud_credential),
    }


def _settings_to_dict(s: BillingSettings) -> dict:
    try:
        notify_days = json.loads(s.notify_days) if s.notify_days else [1, 3, 7]
    except (json.JSONDecodeError, TypeError):
        notify_days = [1, 3, 7]

    return {
        "enabled": s.enabled,
        "notify_days": notify_days,
        "check_interval_minutes": s.check_interval_minutes,
    }


async def _get_server(server_id: int, db: AsyncSession) -> BillingServer:
    result = await db.execute(select(BillingServer).where(BillingServer.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Server not found")
    return server


@router.get("/providers", dependencies=[Depends(verify_auth)])
async def list_cloud_providers():
    return {
        "providers": [
            {
                "id": p.id,
                "requires_account_id": p.requires_account_id,
                "default_currency": p.default_currency,
            }
            for p in PROVIDERS.values()
        ]
    }


@router.get("/servers", dependencies=[Depends(verify_auth)])
async def list_billing_servers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(BillingServer).order_by(BillingServer.paid_until.asc().nullsfirst())
    )
    servers = result.scalars().all()
    return {"servers": [_server_to_dict(s) for s in servers], "count": len(servers)}


@router.post("/servers", dependencies=[Depends(verify_auth)])
async def create_billing_server(data: BillingServerCreate, db: AsyncSession = Depends(get_db)):
    billing_type, legacy_provider = _normalize_type(data.billing_type)

    now = datetime.now(timezone.utc)
    server = BillingServer(
        name=data.name,
        billing_type=billing_type,
        currency=data.currency or "USD",
        notes=data.notes,
        folder=data.folder,
    )

    if billing_type == "monthly":
        if data.paid_until:
            try:
                server.paid_until = parse_flexible_date(data.paid_until)
            except ValueError:
                raise HTTPException(400, f"Invalid date format: {data.paid_until}")
        else:
            days = data.paid_days or 30
            server.paid_until = now + timedelta(days=days)
    elif billing_type == "resource":
        server.monthly_cost = data.monthly_cost or 0
        server.account_balance = data.account_balance or 0
        server.balance_updated_at = now
        server.paid_until = _compute_paid_until_resource(
            server.monthly_cost, server.account_balance, now
        )
    else:
        provider = data.cloud_provider or legacy_provider
        _validate_provider(provider)
        server.cloud_provider = provider
        server.cloud_credential = data.cloud_credential
        server.cloud_account_id = data.cloud_account_id
        server.cloud_balance_threshold = data.cloud_balance_threshold or 0
        server.currency = data.currency or PROVIDERS[provider].default_currency

    db.add(server)
    await db.commit()
    await db.refresh(server)
    return {"success": True, "server": _server_to_dict(server)}


@router.put("/servers/{server_id}", dependencies=[Depends(verify_auth)])
async def update_billing_server(
    server_id: int, data: BillingServerUpdate, db: AsyncSession = Depends(get_db)
):
    server = await _get_server(server_id, db)

    update = data.model_dump(exclude_unset=True)

    if "billing_type" in update:
        billing_type, legacy_provider = _normalize_type(update["billing_type"])
        update["billing_type"] = billing_type
        if legacy_provider:
            update.setdefault("cloud_provider", legacy_provider)
    if update.get("cloud_provider"):
        _validate_provider(update["cloud_provider"])

    for key, value in update.items():
        if key == "paid_until":
            if value:
                try:
                    server.paid_until = parse_flexible_date(value)
                except ValueError:
                    raise HTTPException(400, f"Invalid date format: {value}")
            else:
                server.paid_until = None
        elif key == "account_balance" and value is not None:
            server.account_balance = value
            server.balance_updated_at = datetime.now(timezone.utc)
        elif key == "cloud_credential":
            # Пустая строка = «оставить как было»: форма не присылает сохранённый токен
            if value:
                server.cloud_credential = value
        else:
            setattr(server, key, value)

    if server.billing_type == "resource" and server.monthly_cost and server.account_balance is not None:
        base = server.balance_updated_at or datetime.now(timezone.utc)
        server.paid_until = _compute_paid_until_resource(
            server.monthly_cost, server.account_balance, base
        )

    server.last_notified_days = None
    await db.commit()
    await db.refresh(server)
    return _server_to_dict(server)


@router.delete("/servers/{server_id}", dependencies=[Depends(verify_auth)])
async def delete_billing_server(server_id: int, db: AsyncSession = Depends(get_db)):
    server = await _get_server(server_id, db)
    await db.delete(server)
    await db.commit()
    return {"success": True}


@router.post("/servers/{server_id}/extend", dependencies=[Depends(verify_auth)])
async def extend_billing_server(
    server_id: int, data: ExtendRequest, db: AsyncSession = Depends(get_db)
):
    server = await _get_server(server_id, db)

    now = datetime.now(timezone.utc)
    base = server.paid_until or now
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    if base < now:
        base = now

    server.paid_until = base + timedelta(days=data.days)
    server.last_notified_days = None
    await db.commit()
    await db.refresh(server)
    return _server_to_dict(server)


@router.post("/servers/{server_id}/topup", dependencies=[Depends(verify_auth)])
async def topup_billing_server(
    server_id: int, data: TopupRequest, db: AsyncSession = Depends(get_db)
):
    server = await _get_server(server_id, db)

    if server.billing_type != "resource":
        raise HTTPException(400, "Topup is only for resource billing type")

    now = datetime.now(timezone.utc)

    if server.balance_updated_at and server.monthly_cost and server.monthly_cost > 0:
        updated = server.balance_updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        elapsed_days = (now - updated).total_seconds() / 86400
        daily_cost = server.monthly_cost / 30
        consumed = elapsed_days * daily_cost
        server.account_balance = max(0, (server.account_balance or 0) - consumed)

    server.account_balance = (server.account_balance or 0) + data.amount
    server.balance_updated_at = now
    server.paid_until = _compute_paid_until_resource(
        server.monthly_cost or 0, server.account_balance, now
    )
    server.last_notified_days = None
    await db.commit()
    await db.refresh(server)
    return _server_to_dict(server)


@router.post("/servers/{server_id}/sync", dependencies=[Depends(verify_auth)])
async def sync_cloud_server(server_id: int, db: AsyncSession = Depends(get_db)):
    server = await _get_server(server_id, db)
    if server.billing_type != "cloud":
        raise HTTPException(400, "Not a cloud billing server")

    try:
        await sync_cloud_balance(server, datetime.now(timezone.utc))
    except CloudAuthError as e:
        await db.commit()
        raise HTTPException(502, f"Cloud auth error: {e}")
    except CloudBillingError as e:
        await db.commit()
        raise HTTPException(502, f"Cloud API error: {e}")

    server.last_notified_days = None
    await db.commit()
    await db.refresh(server)
    return _server_to_dict(server)


class MoveToFolderRequest(BaseModel):
    server_ids: list[int]
    folder: Optional[str] = None


class RenameFolderRequest(BaseModel):
    old_name: str
    new_name: str


@router.post("/servers/move-to-folder", dependencies=[Depends(verify_auth)])
async def move_servers_to_folder(data: MoveToFolderRequest, db: AsyncSession = Depends(get_db)):
    folder_value = data.folder.strip() if data.folder and data.folder.strip() else None
    result = await db.execute(
        select(BillingServer).where(BillingServer.id.in_(data.server_ids))
    )
    servers = result.scalars().all()
    for s in servers:
        s.folder = folder_value
    await db.commit()
    return {"success": True, "moved": len(servers)}


@router.post("/folders/rename", dependencies=[Depends(verify_auth)])
async def rename_billing_folder(data: RenameFolderRequest, db: AsyncSession = Depends(get_db)):
    new_name = data.new_name.strip() if data.new_name else None
    if not new_name:
        raise HTTPException(400, "new_name is required")
    result = await db.execute(
        select(BillingServer).where(BillingServer.folder == data.old_name)
    )
    servers = result.scalars().all()
    for s in servers:
        s.folder = new_name
    await db.commit()
    return {"success": True, "renamed": len(servers)}


@router.delete("/folders/{folder_name}", dependencies=[Depends(verify_auth)])
async def delete_billing_folder(folder_name: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(BillingServer).where(BillingServer.folder == folder_name)
    )
    servers = result.scalars().all()
    for s in servers:
        s.folder = None
    await db.commit()
    return {"success": True, "unfoldered": len(servers)}


@router.get("/settings", dependencies=[Depends(verify_auth)])
async def get_billing_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BillingSettings).limit(1))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = BillingSettings()
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return _settings_to_dict(settings)


@router.put("/settings", dependencies=[Depends(verify_auth)])
async def update_billing_settings(
    data: BillingSettingsUpdate, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(BillingSettings).limit(1))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = BillingSettings()
        db.add(settings)
        await db.flush()

    update = data.model_dump(exclude_unset=True)
    for key, value in update.items():
        if key == "notify_days":
            settings.notify_days = json.dumps(value)
        else:
            setattr(settings, key, value)

    await db.commit()
    await db.refresh(settings)
    return _settings_to_dict(settings)
