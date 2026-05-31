"""Firewall (UFW) profiles — CRUD, привязка серверов, массовая синхронизация."""

import json
import logging
from typing import Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import async_session_maker, get_db
from app.models import FirewallProfile, FirewallSyncLog, Server
from app.services.firewall_profile_sync import compute_rules_hash, sync_profile_to_servers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/firewall-profiles", tags=["firewall-profiles"])


# ==================== Schemas ====================

class FirewallRuleData(BaseModel):
    port: int = Field(..., ge=1, le=65535)
    protocol: Literal["tcp", "udp", "any"] = "tcp"
    action: Literal["allow", "deny"] = "allow"
    from_ip: Optional[str] = None
    direction: Literal["in", "out"] = "in"
    comment: Optional[str] = ""

    @field_validator("from_ip", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        if v in ("", "any", "anywhere", "Anywhere"):
            return None
        return v


DefaultPolicy = Literal["allow", "deny", "reject"]


class ProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    rules: Optional[list[FirewallRuleData]] = None  # None => дефолт SSH 22/tcp
    default_incoming: DefaultPolicy = "deny"
    default_outgoing: DefaultPolicy = "allow"


class ProfileUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    rules: Optional[list[FirewallRuleData]] = None
    default_incoming: Optional[DefaultPolicy] = None
    default_outgoing: Optional[DefaultPolicy] = None


class ReorderRequest(BaseModel):
    profile_ids: list[int]


class CloneRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)


# ==================== Helpers ====================

NODE_API_PORT = 9100

DEFAULT_RULES: list[dict] = [
    {"port": NODE_API_PORT, "protocol": "tcp", "action": "allow",
     "from_ip": None, "direction": "in", "comment": "Monitoring node API"},
]


def _serialize_rules(profile: FirewallProfile) -> list[dict]:
    try:
        rules = json.loads(profile.rules_json) if profile.rules_json else []
        return rules if isinstance(rules, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _rule_identity(rule: dict) -> tuple:
    """Каноничный ключ правила для сравнения дубликатов (как минимум по порту,
    плюс протокол, действие, источник, направление). Комментарий не учитывается."""
    from_ip = (rule.get("from_ip") or "").strip().lower() or None
    return (
        int(rule.get("port", 0)),
        (rule.get("protocol") or "tcp").lower(),
        (rule.get("action") or "allow").lower(),
        from_ip,
        (rule.get("direction") or "in").lower(),
    )


def _has_node_port_allow(rules: list[dict], default_in: str) -> bool:
    """Гарантирует, что панель не потеряет связь с нодой после применения."""
    if (default_in or "deny").lower() == "allow":
        return True
    for r in rules:
        if (
            int(r.get("port", 0)) == NODE_API_PORT
            and (r.get("protocol") or "tcp").lower() in ("tcp", "any")
            and (r.get("action") or "").lower() == "allow"
            and (r.get("direction") or "in").lower() == "in"
        ):
            return True
    return False


async def _get_profile(profile_id: int, db: AsyncSession) -> FirewallProfile:
    result = await db.execute(
        select(FirewallProfile).where(FirewallProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(404, "Firewall profile not found")
    return profile


def _profile_to_dict(profile: FirewallProfile, *, linked: int = 0, synced: int = 0) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "description": profile.description,
        "rules": _serialize_rules(profile),
        "default_incoming": profile.default_incoming,
        "default_outgoing": profile.default_outgoing,
        "position": profile.position,
        "linked_servers_count": linked,
        "synced_servers_count": synced,
        "node_port_allowed": _has_node_port_allow(_serialize_rules(profile), profile.default_incoming),
        "node_api_port": NODE_API_PORT,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


async def _bg_sync_profile(profile_id: int, force: bool = False):
    """Фоновая синхронизация профиля на все привязанные серверы."""
    async with async_session_maker() as db:
        try:
            result = await db.execute(
                select(FirewallProfile).where(FirewallProfile.id == profile_id)
            )
            profile = result.scalar_one_or_none()
            if profile:
                await sync_profile_to_servers(profile, db, force=force)
        except Exception as e:
            logger.error("Background firewall sync failed for profile %s: %s", profile_id, e)


# ==================== Available servers (до /{profile_id}) ====================

@router.get("/available-servers")
async def get_available_servers(db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    result = await db.execute(
        select(Server.id, Server.name, Server.url,
               Server.active_firewall_profile_id, Server.firewall_sync_status)
        .order_by(Server.name)
    )
    return [
        {
            "id": row[0],
            "name": row[1],
            "url": row[2],
            "active_profile_id": row[3],
            "sync_status": row[4],
        }
        for row in result.fetchall()
    ]


@router.post("/reorder")
async def reorder_profiles(data: ReorderRequest, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    for i, pid in enumerate(data.profile_ids):
        await db.execute(
            update(FirewallProfile)
            .where(FirewallProfile.id == pid)
            .values(position=i)
        )
    await db.commit()
    return {"success": True}


# ==================== CRUD ====================

@router.get("/")
async def list_profiles(db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    result = await db.execute(
        select(FirewallProfile).order_by(FirewallProfile.position, FirewallProfile.id)
    )
    profiles = list(result.scalars().all())

    profile_ids = [p.id for p in profiles]
    counts: dict[int, dict] = {pid: {"total": 0, "synced": 0} for pid in profile_ids}

    if profile_ids:
        srv_result = await db.execute(
            select(Server.active_firewall_profile_id, Server.firewall_sync_status, func.count())
            .where(Server.active_firewall_profile_id.in_(profile_ids))
            .group_by(Server.active_firewall_profile_id, Server.firewall_sync_status)
        )
        for prof_id, sync_st, cnt in srv_result.fetchall():
            counts[prof_id]["total"] += cnt
            if sync_st == "synced":
                counts[prof_id]["synced"] += cnt

    return [
        _profile_to_dict(p, linked=counts[p.id]["total"], synced=counts[p.id]["synced"])
        for p in profiles
    ]


@router.post("/")
async def create_profile(data: ProfileCreate, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    existing = await db.execute(
        select(FirewallProfile).where(FirewallProfile.name == data.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"Profile '{data.name}' already exists")

    max_pos = await db.execute(select(func.max(FirewallProfile.position)))
    position = (max_pos.scalar() or 0) + 1

    if data.rules is None:
        rules = list(DEFAULT_RULES)
    else:
        rules = [r.model_dump() for r in data.rules]

    profile = FirewallProfile(
        name=data.name,
        description=data.description,
        rules_json=json.dumps(rules),
        default_incoming=data.default_incoming,
        default_outgoing=data.default_outgoing,
        position=position,
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)

    return _profile_to_dict(profile)


@router.get("/{profile_id}")
async def get_profile(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)

    result = await db.execute(
        select(Server).where(Server.active_firewall_profile_id == profile_id).order_by(Server.name)
    )
    servers = list(result.scalars().all())

    rules_hash = compute_rules_hash(
        profile.rules_json, profile.default_incoming, profile.default_outgoing
    )

    data = _profile_to_dict(profile)
    data["rules_hash"] = rules_hash
    data["servers"] = [
        {
            "server_id": s.id,
            "server_name": s.name,
            "server_url": s.url,
            "sync_status": s.firewall_sync_status,
            "rules_hash": s.firewall_rules_hash,
            "is_synced": s.firewall_rules_hash == rules_hash,
            "last_sync_at": s.firewall_last_sync_at.isoformat() if s.firewall_last_sync_at else None,
        }
        for s in servers
    ]
    return data


@router.put("/{profile_id}")
async def update_profile(
    profile_id: int,
    data: ProfileUpdate,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)
    rules_changed = False

    if data.name is not None and data.name != profile.name:
        dup = await db.execute(
            select(FirewallProfile).where(
                FirewallProfile.name == data.name,
                FirewallProfile.id != profile_id,
            )
        )
        if dup.scalar_one_or_none():
            raise HTTPException(400, f"Profile '{data.name}' already exists")
        profile.name = data.name

    if data.description is not None:
        profile.description = data.description
    if data.default_incoming is not None:
        profile.default_incoming = data.default_incoming
        rules_changed = True
    if data.default_outgoing is not None:
        profile.default_outgoing = data.default_outgoing
        rules_changed = True
    if data.rules is not None:
        profile.rules_json = json.dumps([r.model_dump() for r in data.rules])
        rules_changed = True

    if rules_changed:
        new_hash = compute_rules_hash(
            profile.rules_json, profile.default_incoming, profile.default_outgoing
        )
        await db.execute(
            update(Server)
            .where(
                Server.active_firewall_profile_id == profile_id,
                Server.firewall_rules_hash != new_hash,
            )
            .values(firewall_sync_status="pending")
        )

    await db.commit()
    await db.refresh(profile)

    if rules_changed:
        bg.add_task(_bg_sync_profile, profile_id)

    return _profile_to_dict(profile)


@router.post("/{profile_id}/clone")
async def clone_profile(
    profile_id: int,
    data: CloneRequest,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    """Скопировать профиль: правила и политики идентичны, имя новое."""
    source = await _get_profile(profile_id, db)

    base_name = (data.name or "").strip() or f"{source.name} (копия)"
    new_name = base_name
    suffix = 2
    while True:
        existing = await db.execute(
            select(FirewallProfile).where(FirewallProfile.name == new_name)
        )
        if existing.scalar_one_or_none() is None:
            break
        new_name = f"{base_name} ({suffix})"
        suffix += 1
        if suffix > 100:
            raise HTTPException(400, "Could not generate unique name")

    max_pos = await db.execute(select(func.max(FirewallProfile.position)))
    position = (max_pos.scalar() or 0) + 1

    clone = FirewallProfile(
        name=new_name,
        description=source.description,
        rules_json=source.rules_json,
        default_incoming=source.default_incoming,
        default_outgoing=source.default_outgoing,
        position=position,
    )
    db.add(clone)
    await db.commit()
    await db.refresh(clone)

    return _profile_to_dict(clone)


@router.delete("/{profile_id}")
async def delete_profile(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)

    await db.execute(
        update(Server)
        .where(Server.active_firewall_profile_id == profile_id)
        .values(
            active_firewall_profile_id=None,
            firewall_sync_status=None,
        )
    )
    await db.delete(profile)
    await db.commit()
    return {"success": True}


# ==================== Rules CRUD ====================

@router.get("/{profile_id}/rules")
async def get_rules(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)
    return _serialize_rules(profile)


@router.post("/{profile_id}/rules")
async def add_rule(
    profile_id: int,
    rule: FirewallRuleData,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)
    rules = _serialize_rules(profile)

    new_rule = rule.model_dump()
    if any(_rule_identity(r) == _rule_identity(new_rule) for r in rules):
        raise HTTPException(409, "Такое правило уже есть в профиле")

    rules.append(new_rule)
    profile.rules_json = json.dumps(rules)

    new_hash = compute_rules_hash(profile.rules_json, profile.default_incoming, profile.default_outgoing)
    await db.execute(
        update(Server)
        .where(
            Server.active_firewall_profile_id == profile_id,
            Server.firewall_rules_hash != new_hash,
        )
        .values(firewall_sync_status="pending")
    )
    await db.commit()

    bg.add_task(_bg_sync_profile, profile_id)
    return {"success": True, "rules": rules}


@router.put("/{profile_id}/rules/{rule_index}")
async def update_rule(
    profile_id: int,
    rule_index: int,
    rule: FirewallRuleData,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)
    rules = _serialize_rules(profile)
    if not 0 <= rule_index < len(rules):
        raise HTTPException(404, "Rule index out of range")

    new_rule = rule.model_dump()
    new_key = _rule_identity(new_rule)
    if any(i != rule_index and _rule_identity(r) == new_key for i, r in enumerate(rules)):
        raise HTTPException(409, "Такое правило уже есть в профиле")

    rules[rule_index] = new_rule
    profile.rules_json = json.dumps(rules)

    new_hash = compute_rules_hash(profile.rules_json, profile.default_incoming, profile.default_outgoing)
    await db.execute(
        update(Server)
        .where(
            Server.active_firewall_profile_id == profile_id,
            Server.firewall_rules_hash != new_hash,
        )
        .values(firewall_sync_status="pending")
    )
    await db.commit()

    bg.add_task(_bg_sync_profile, profile_id)
    return {"success": True, "rules": rules}


@router.delete("/{profile_id}/rules/{rule_index}")
async def delete_rule(
    profile_id: int,
    rule_index: int,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)
    rules = _serialize_rules(profile)
    if not 0 <= rule_index < len(rules):
        raise HTTPException(404, "Rule index out of range")

    rules.pop(rule_index)
    profile.rules_json = json.dumps(rules)

    new_hash = compute_rules_hash(profile.rules_json, profile.default_incoming, profile.default_outgoing)
    await db.execute(
        update(Server)
        .where(
            Server.active_firewall_profile_id == profile_id,
            Server.firewall_rules_hash != new_hash,
        )
        .values(firewall_sync_status="pending")
    )
    await db.commit()

    bg.add_task(_bg_sync_profile, profile_id)
    return {"success": True, "rules": rules}


# ==================== Server bindings ====================

@router.post("/{profile_id}/servers/{server_id}")
async def link_server(
    profile_id: int,
    server_id: int,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    await _get_profile(profile_id, db)

    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Server not found")

    server.active_firewall_profile_id = profile_id
    server.firewall_sync_status = "pending"
    await db.commit()

    bg.add_task(_bg_sync_profile, profile_id)
    return {"success": True}


@router.delete("/{profile_id}/servers/{server_id}")
async def unlink_server(profile_id: int, server_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Server not found")

    server.active_firewall_profile_id = None
    server.firewall_sync_status = None
    server.firewall_rules_hash = None
    server.firewall_last_sync_at = None
    await db.commit()
    return {"success": True}


# ==================== Sync ====================

@router.post("/{profile_id}/sync")
async def sync_all(
    profile_id: int,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)
    results = await sync_profile_to_servers(profile, db, force=force)
    return {
        "results": [
            {
                "server_id": r.server_id,
                "server_name": r.server_name,
                "success": r.success,
                "message": r.message,
                "rolled_back": r.rolled_back,
            }
            for r in results
        ]
    }


@router.post("/{profile_id}/sync/{server_id}")
async def sync_one(
    profile_id: int,
    server_id: int,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)
    results = await sync_profile_to_servers(profile, db, server_ids=[server_id], force=force)
    if not results:
        raise HTTPException(404, "Server not linked to this profile or inactive")
    r = results[0]
    return {
        "server_id": r.server_id,
        "server_name": r.server_name,
        "success": r.success,
        "message": r.message,
        "rolled_back": r.rolled_back,
    }


# ==================== Sync log ====================

@router.get("/{profile_id}/log")
async def get_sync_log(profile_id: int, limit: int = 50, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    await _get_profile(profile_id, db)

    result = await db.execute(
        select(FirewallSyncLog)
        .where(FirewallSyncLog.profile_id == profile_id)
        .order_by(FirewallSyncLog.created_at.desc())
        .limit(limit)
    )
    logs = list(result.scalars().all())

    server_ids = list({l.server_id for l in logs})
    server_names: dict[int, str] = {}
    if server_ids:
        srv_result = await db.execute(select(Server.id, Server.name).where(Server.id.in_(server_ids)))
        server_names = {row[0]: row[1] for row in srv_result.fetchall()}

    return [
        {
            "id": l.id,
            "server_id": l.server_id,
            "server_name": server_names.get(l.server_id, "Unknown"),
            "status": l.status,
            "message": l.message,
            "rules_hash": l.rules_hash,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        }
        for l in logs
    ]
