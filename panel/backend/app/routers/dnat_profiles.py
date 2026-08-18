"""DNAT-профили (проброс портов через iptables nat) — CRUD, привязка серверов, синхронизация."""

import ipaddress
import json
import logging
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import async_session_maker, get_db
from app.models import DnatProfile, DnatSyncLog, Server
from app.services.dnat_profile_sync import (
    assigned_targets,
    clear_dnat_on_servers,
    compute_rules_hash,
    load_rules,
    ordered_linked_servers,
    render_rules_for_server,
    split_targets,
    sync_profile_to_servers,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dnat-profiles", tags=["dnat-profiles"])

# Порт mTLS-nginx ноды: правило на него отрезало бы панель от сервера
NODE_API_PORT = 9100
SSH_DEFAULT_PORT = 22
# Больше адресов назначения в одном правиле не бывает нужно — а нода получает всё равно один
MAX_TARGETS_PER_RULE = 32

Protocol = Literal["tcp", "udp", "both"]
# per_server — каждой ноде свой IP из списка по порядку привязки (распределяет панель);
# остальные — нода получает весь список и раскидывает новые соединения сама
Distribution = Literal["per_server", "random", "round_robin", "client_hash"]


# ==================== Schemas ====================

class DnatRuleData(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    protocol: Protocol = "tcp"
    listen_port: int = Field(..., ge=1, le=65535)
    listen_port_end: Optional[int] = Field(None, ge=1, le=65535)
    target_ip: str
    distribution: Distribution = "per_server"
    target_port: int = Field(0, ge=0, le=65535)
    masquerade: bool = True
    # Маскировка транзита на ноде: TTL=64 + MSS clamp на потоках правила
    mask_ttl: bool = False
    enabled: bool = True
    comment: Optional[str] = Field("", max_length=200)

    @field_validator("target_ip")
    @classmethod
    def _ipv4_list(cls, value: str) -> str:
        """Один адрес или несколько через запятую — серверы профиля получают их по кругу."""
        targets: list[str] = []
        for part in split_targets(value):
            try:
                address = ipaddress.IPv4Address(part)
            except ValueError:
                raise ValueError(f"Адрес назначения '{part}' должен быть IPv4")
            if address.is_unspecified or address.is_multicast:
                raise ValueError(f"Адрес назначения '{part}' должен быть unicast IPv4")
            if str(address) not in targets:
                targets.append(str(address))
        if not targets:
            raise ValueError("Укажите хотя бы один адрес назначения")
        if len(targets) > MAX_TARGETS_PER_RULE:
            raise ValueError(f"Не больше {MAX_TARGETS_PER_RULE} адресов назначения в одном правиле")
        return ",".join(targets)

    @field_validator("comment", mode="before")
    @classmethod
    def _none_comment(cls, value):
        return "" if value is None else value

    @model_validator(mode="after")
    def _check_range(self) -> "DnatRuleData":
        if self.listen_port_end is not None:
            if self.listen_port_end == self.listen_port:
                self.listen_port_end = None
            elif self.listen_port_end < self.listen_port:
                raise ValueError("Конец диапазона должен быть больше начала")
        return self


class ProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    rules: Optional[list[DnatRuleData]] = None


class ProfileUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    rules: Optional[list[DnatRuleData]] = None


class CloneRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)


# ==================== Helpers ====================

def _protocols(rule: dict) -> set[str]:
    proto = (rule.get("protocol") or "tcp").lower()
    return {"tcp", "udp"} if proto == "both" else {proto}


def _port_range(rule: dict) -> tuple[int, int]:
    low = int(rule.get("listen_port", 0))
    end = rule.get("listen_port_end")
    return low, int(end) if end else low


def validate_rule_set(rules: list[dict]) -> Optional[str]:
    """Причина отказа или None. Зеркало validate_rules на ноде: правило, которое
    нода отвергнет, не должно попадать в профиль и висеть в статусе failed."""
    seen: set[str] = set()
    for rule in rules:
        if rule["name"] in seen:
            return f"Правило с именем '{rule['name']}' уже есть в профиле"
        seen.add(rule["name"])

    active = [r for r in rules if r.get("enabled", True)]
    for rule in active:
        low, high = _port_range(rule)
        if "tcp" in _protocols(rule) and low <= NODE_API_PORT <= high:
            return f"Правило '{rule['name']}' закрывает порт API ноды {NODE_API_PORT}/tcp — панель потеряет связь с сервером"

    for index, rule in enumerate(active):
        low, high = _port_range(rule)
        for other in active[index + 1:]:
            if not _protocols(rule) & _protocols(other):
                continue
            other_low, other_high = _port_range(other)
            if low <= other_high and other_low <= high:
                return f"Правила '{rule['name']}' и '{other['name']}' пересекаются по портам"
    return None


def _covers_ssh(rules: list[dict]) -> bool:
    return any(
        r.get("enabled", True) and "tcp" in _protocols(r) and _port_range(r)[0] <= SSH_DEFAULT_PORT <= _port_range(r)[1]
        for r in rules
    )


async def _get_profile(profile_id: int, db: AsyncSession) -> DnatProfile:
    profile = (await db.execute(select(DnatProfile).where(DnatProfile.id == profile_id))).scalar_one_or_none()
    if not profile:
        raise HTTPException(404, "DNAT profile not found")
    return profile


def _profile_to_dict(profile: DnatProfile, *, linked: int = 0, synced: int = 0) -> dict:
    rules = load_rules(profile)
    return {
        "id": profile.id,
        "name": profile.name,
        "description": profile.description,
        "rules": rules,
        "position": profile.position,
        "linked_servers_count": linked,
        "synced_servers_count": synced,
        "ssh_port_covered": _covers_ssh(rules),
        "ssh_default_port": SSH_DEFAULT_PORT,
        "node_api_port": NODE_API_PORT,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


async def _store_rules(profile: DnatProfile, rules: list[dict], db: AsyncSession) -> None:
    """Сохранить набор и перевести разъехавшиеся серверы в pending. Ожидаемый хэш у
    каждого сервера свой — правило с несколькими IP рендерится под его позицию."""
    error = validate_rule_set(rules)
    if error:
        raise HTTPException(409, error)
    profile.rules_json = json.dumps(rules)
    for index, server in enumerate(await ordered_linked_servers(profile.id, db)):
        if server.dnat_rules_hash != compute_rules_hash(render_rules_for_server(rules, index)):
            server.dnat_sync_status = "pending"
    await db.commit()


async def _bg_sync_profile(profile_id: int, server_ids: list[int] | None = None):
    async with async_session_maker() as db:
        try:
            profile = (await db.execute(select(DnatProfile).where(DnatProfile.id == profile_id))).scalar_one_or_none()
            if profile:
                await sync_profile_to_servers(profile, db, server_ids=server_ids)
        except Exception as e:
            logger.error("Background DNAT sync failed for profile %s: %s", profile_id, e)


# ==================== Available servers (до /{profile_id}) ====================

@router.get("/available-servers")
async def get_available_servers(db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    result = await db.execute(
        select(Server.id, Server.name, Server.url, Server.active_dnat_profile_id, Server.dnat_sync_status, Server.folder)
        .order_by(Server.name)
    )
    return [
        {"id": row[0], "name": row[1], "url": row[2], "active_profile_id": row[3], "sync_status": row[4], "folder": row[5]}
        for row in result.fetchall()
    ]


# ==================== CRUD ====================

@router.get("/")
async def list_profiles(db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profiles = list((await db.execute(
        select(DnatProfile).order_by(DnatProfile.position, DnatProfile.id)
    )).scalars().all())

    counts: dict[int, dict] = {p.id: {"total": 0, "synced": 0} for p in profiles}
    if counts:
        rows = await db.execute(
            select(Server.active_dnat_profile_id, Server.dnat_sync_status, func.count())
            .where(Server.active_dnat_profile_id.in_(list(counts)))
            .group_by(Server.active_dnat_profile_id, Server.dnat_sync_status)
        )
        for profile_id, sync_status, count in rows.fetchall():
            counts[profile_id]["total"] += count
            if sync_status == "synced":
                counts[profile_id]["synced"] += count

    return [_profile_to_dict(p, linked=counts[p.id]["total"], synced=counts[p.id]["synced"]) for p in profiles]


@router.post("/")
async def create_profile(data: ProfileCreate, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    if (await db.execute(select(DnatProfile).where(DnatProfile.name == data.name))).scalar_one_or_none():
        raise HTTPException(400, f"Profile '{data.name}' already exists")

    rules = [r.model_dump() for r in data.rules] if data.rules else []
    error = validate_rule_set(rules)
    if error:
        raise HTTPException(409, error)

    position = ((await db.execute(select(func.max(DnatProfile.position)))).scalar() or 0) + 1
    profile = DnatProfile(
        name=data.name, description=data.description, rules_json=json.dumps(rules), position=position,
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return _profile_to_dict(profile)


@router.get("/{profile_id}")
async def get_profile(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)
    rules = load_rules(profile)
    servers = await ordered_linked_servers(profile_id, db)

    data = _profile_to_dict(profile)
    data["rules_hash"] = compute_rules_hash(rules)
    data["servers"] = [
        {
            "server_id": s.id,
            "server_name": s.name,
            "server_url": s.url,
            "sync_status": s.dnat_sync_status,
            "rules_hash": s.dnat_rules_hash,
            "is_synced": s.dnat_rules_hash == compute_rules_hash(render_rules_for_server(rules, index)),
            "last_sync_at": s.dnat_last_sync_at.isoformat() if s.dnat_last_sync_at else None,
            "link_position": index + 1,
            "targets": assigned_targets(rules, index),
        }
        for index, s in enumerate(servers)
    ]
    return data


@router.put("/{profile_id}")
async def update_profile(
    profile_id: int, data: ProfileUpdate, bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)

    if data.name is not None and data.name != profile.name:
        dup = (await db.execute(
            select(DnatProfile).where(DnatProfile.name == data.name, DnatProfile.id != profile_id)
        )).scalar_one_or_none()
        if dup:
            raise HTTPException(400, f"Profile '{data.name}' already exists")
        profile.name = data.name
    if data.description is not None:
        profile.description = data.description

    if data.rules is not None:
        await _store_rules(profile, [r.model_dump() for r in data.rules], db)
        bg.add_task(_bg_sync_profile, profile_id)
    else:
        await db.commit()

    await db.refresh(profile)
    return _profile_to_dict(profile)


@router.post("/{profile_id}/clone")
async def clone_profile(
    profile_id: int, data: CloneRequest, db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    source = await _get_profile(profile_id, db)

    base_name = (data.name or "").strip() or f"{source.name} (копия)"
    new_name = base_name
    suffix = 2
    while (await db.execute(select(DnatProfile).where(DnatProfile.name == new_name))).scalar_one_or_none():
        new_name = f"{base_name} ({suffix})"
        suffix += 1
        if suffix > 100:
            raise HTTPException(400, "Could not generate unique name")

    position = ((await db.execute(select(func.max(DnatProfile.position)))).scalar() or 0) + 1
    clone = DnatProfile(
        name=new_name, description=source.description, rules_json=source.rules_json, position=position,
    )
    db.add(clone)
    await db.commit()
    await db.refresh(clone)
    return _profile_to_dict(clone)


@router.delete("/{profile_id}")
async def delete_profile(
    profile_id: int, bg: BackgroundTasks, db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    """Удалить профиль; с привязанных нод правила снимаются — иначе они пробрасывали
    бы трафик дальше без профиля, который бы этим управлял."""
    profile = await _get_profile(profile_id, db)
    linked = [
        row[0] for row in (await db.execute(
            select(Server.id).where(Server.active_dnat_profile_id == profile_id)
        )).fetchall()
    ]
    await db.execute(
        update(Server)
        .where(Server.active_dnat_profile_id == profile_id)
        .values(active_dnat_profile_id=None, dnat_sync_status=None, dnat_link_position=None)
    )
    await db.delete(profile)
    await db.commit()
    if linked:
        bg.add_task(clear_dnat_on_servers, linked)
    return {"success": True}


# ==================== Rules CRUD ====================

@router.post("/{profile_id}/rules")
async def add_rule(
    profile_id: int, rule: DnatRuleData, bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)
    rules = load_rules(profile)
    rules.append(rule.model_dump())
    await _store_rules(profile, rules, db)
    bg.add_task(_bg_sync_profile, profile_id)
    return {"success": True, "rules": rules}


@router.put("/{profile_id}/rules/{rule_index}")
async def update_rule(
    profile_id: int, rule_index: int, rule: DnatRuleData, bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)
    rules = load_rules(profile)
    if not 0 <= rule_index < len(rules):
        raise HTTPException(404, "Rule index out of range")
    rules[rule_index] = rule.model_dump()
    await _store_rules(profile, rules, db)
    bg.add_task(_bg_sync_profile, profile_id)
    return {"success": True, "rules": rules}


@router.delete("/{profile_id}/rules/{rule_index}")
async def delete_rule(
    profile_id: int, rule_index: int, bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)
    rules = load_rules(profile)
    if not 0 <= rule_index < len(rules):
        raise HTTPException(404, "Rule index out of range")
    rules.pop(rule_index)
    await _store_rules(profile, rules, db)
    bg.add_task(_bg_sync_profile, profile_id)
    return {"success": True, "rules": rules}


# ==================== Server bindings ====================

@router.post("/{profile_id}/servers/{server_id}")
async def link_server(
    profile_id: int, server_id: int, bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    await _get_profile(profile_id, db)
    server = (await db.execute(select(Server).where(Server.id == server_id))).scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Server not found")

    # Новый сервер встаёт в конец очереди привязки: у уже привязанных IP назначения
    # не меняются, и их правила не переприменяются
    last_position = (await db.execute(
        select(func.max(Server.dnat_link_position)).where(Server.active_dnat_profile_id == profile_id)
    )).scalar() or 0
    server.active_dnat_profile_id = profile_id
    server.dnat_sync_status = "pending"
    server.dnat_link_position = last_position + 1
    await db.commit()

    bg.add_task(_bg_sync_profile, profile_id, server_ids=[server_id])
    return {"success": True}


@router.delete("/{profile_id}/servers/{server_id}")
async def unlink_server(
    profile_id: int, server_id: int, bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    """Отвязать сервер и снять с ноды правила (офлайн-нода получит это через очередь)."""
    server = (await db.execute(select(Server).where(Server.id == server_id))).scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Server not found")

    server.active_dnat_profile_id = None
    server.dnat_sync_status = None
    server.dnat_rules_hash = None
    server.dnat_last_sync_at = None
    server.dnat_link_position = None
    await db.commit()

    bg.add_task(clear_dnat_on_servers, [server_id], True, profile_id)
    return {"success": True}


# ==================== Sync ====================

def _result_to_dict(r) -> dict:
    return {
        "server_id": r.server_id, "server_name": r.server_name,
        "success": r.success, "message": r.message, "queued": r.queued,
    }


@router.post("/{profile_id}/sync")
async def sync_all(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)
    results = await sync_profile_to_servers(profile, db)
    return {"results": [_result_to_dict(r) for r in results]}


@router.post("/{profile_id}/sync/{server_id}")
async def sync_one(profile_id: int, server_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)
    results = await sync_profile_to_servers(profile, db, server_ids=[server_id])
    if not results:
        raise HTTPException(404, "Server not linked to this profile or inactive")
    return _result_to_dict(results[0])


# ==================== Sync log ====================

@router.get("/{profile_id}/log")
async def get_sync_log(profile_id: int, limit: int = 50, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    await _get_profile(profile_id, db)
    logs = list((await db.execute(
        select(DnatSyncLog).where(DnatSyncLog.profile_id == profile_id)
        .order_by(DnatSyncLog.created_at.desc()).limit(limit)
    )).scalars().all())

    server_ids = list({entry.server_id for entry in logs})
    server_names: dict[int, str] = {}
    if server_ids:
        rows = await db.execute(select(Server.id, Server.name).where(Server.id.in_(server_ids)))
        server_names = {row[0]: row[1] for row in rows.fetchall()}

    return [
        {
            "id": entry.id,
            "server_id": entry.server_id,
            "server_name": server_names.get(entry.server_id, "Unknown"),
            "status": entry.status,
            "message": entry.message,
            "rules_hash": entry.rules_hash,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }
        for entry in logs
    ]
