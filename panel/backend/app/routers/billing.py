import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import BillingServer, BillingSettings
from app.auth import verify_auth

router = APIRouter(prefix="/billing", tags=["billing"])


class BillingServerCreate(BaseModel):
    name: str
    billing_type: str  # 'monthly' | 'resource'
    paid_days: Optional[int] = None
    monthly_cost: Optional[float] = None
    account_balance: Optional[float] = None
    currency: Optional[str] = "USD"
    notes: Optional[str] = None
    folder: Optional[str] = None


class BillingServerUpdate(BaseModel):
    name: Optional[str] = None
    billing_type: Optional[str] = None
    paid_until: Optional[str] = None
    monthly_cost: Optional[float] = None
    account_balance: Optional[float] = None
    currency: Optional[str] = None
    notes: Optional[str] = None
    folder: Optional[str] = None


class ExtendRequest(BaseModel):
    days: int


class TopupRequest(BaseModel):
    amount: float


class BillingSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    notify_days: Optional[list[int]] = None
    check_interval_minutes: Optional[int] = None


def _compute_paid_until_resource(monthly_cost: float, balance: float, from_time: datetime) -> Optional[datetime]:
    if monthly_cost <= 0 or balance <= 0:
        return from_time
    days_left = (balance / monthly_cost) * 30
    return from_time + timedelta(days=days_left)


def _server_to_dict(s: BillingServer) -> dict:
    now = datetime.now(timezone.utc)
    paid_until = s.paid_until
    days_left = None
    if paid_until:
        if paid_until.tzinfo is None:
            paid_until = paid_until.replace(tzinfo=timezone.utc)
        days_left = max(0, (paid_until - now).total_seconds() / 86400)

    return {
        "id": s.id,
        "name": s.name,
        "billing_type": s.billing_type,
        "paid_until": paid_until.isoformat() if paid_until else None,
        "days_left": round(days_left, 1) if days_left is not None else None,
        "monthly_cost": s.monthly_cost,
        "account_balance": s.account_balance,
        "balance_updated_at": s.balance_updated_at.isoformat() if s.balance_updated_at else None,
        "currency": s.currency or "USD",
        "notes": s.notes,
        "folder": s.folder,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
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


@router.get("/servers", dependencies=[Depends(verify_auth)])
async def list_billing_servers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(BillingServer).order_by(BillingServer.paid_until.asc().nullsfirst())
    )
    servers = result.scalars().all()
    return {"servers": [_server_to_dict(s) for s in servers], "count": len(servers)}


@router.post("/servers", dependencies=[Depends(verify_auth)])
async def create_billing_server(data: BillingServerCreate, db: AsyncSession = Depends(get_db)):
    if data.billing_type not in ("monthly", "resource"):
        raise HTTPException(400, "billing_type must be 'monthly' or 'resource'")

    now = datetime.now(timezone.utc)
    server = BillingServer(
        name=data.name,
        billing_type=data.billing_type,
        currency=data.currency or "USD",
        notes=data.notes,
        folder=data.folder,
    )

    if data.billing_type == "monthly":
        days = data.paid_days or 30
        server.paid_until = now + timedelta(days=days)
    else:
        server.monthly_cost = data.monthly_cost or 0
        server.account_balance = data.account_balance or 0
        server.balance_updated_at = now
        server.paid_until = _compute_paid_until_resource(
            server.monthly_cost, server.account_balance, now
        )

    db.add(server)
    await db.commit()
    await db.refresh(server)
    return {"success": True, "server": _server_to_dict(server)}


@router.put("/servers/{server_id}", dependencies=[Depends(verify_auth)])
async def update_billing_server(
    server_id: int, data: BillingServerUpdate, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(BillingServer).where(BillingServer.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Server not found")

    update = data.model_dump(exclude_unset=True)

    if "billing_type" in update and update["billing_type"] not in ("monthly", "resource"):
        raise HTTPException(400, "billing_type must be 'monthly' or 'resource'")

    for key, value in update.items():
        if key == "paid_until" and value:
            server.paid_until = datetime.fromisoformat(value)
        elif key == "account_balance" and value is not None:
            server.account_balance = value
            server.balance_updated_at = datetime.now(timezone.utc)
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
    result = await db.execute(select(BillingServer).where(BillingServer.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Server not found")

    await db.delete(server)
    await db.commit()
    return {"success": True}


@router.post("/servers/{server_id}/extend", dependencies=[Depends(verify_auth)])
async def extend_billing_server(
    server_id: int, data: ExtendRequest, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(BillingServer).where(BillingServer.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Server not found")

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
    result = await db.execute(select(BillingServer).where(BillingServer.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Server not found")

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
