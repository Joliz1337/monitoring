"""Профили nginx-конфигов Remnawave-нод (зеркало haproxy_profiles).

Профиль — шаблон полного nginx.conf с плейсхолдером {{DOMAIN}} + JSON-опции
схемы реального IP. Правила (gRPC/XHTTP/proxy-локации) живут в конфиге между
маркерами и мутируются через parse → splice; опции меняют структуру
server-блока и пересобирают конфиг целиком.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import async_session_maker, get_db
from app.models import RemnawaveNginxProfile, RemnawaveNginxSyncLog, Server
from app.services.haproxy_profile_sync import compute_config_hash, is_server_online
from app.services.remnawave_nginx_config import (
    CLOUDFLARE_RANGES,
    DOMAIN_RE,
    GrpcRule,
    MissingMarkersError,
    OptionsValidationError,
    ProfileOptions,
    ProxyRule,
    Rule,
    RuleValidationError,
    XhttpRule,
    detect_domain,
    generate_full_config,
    has_markers,
    parse_rules_from_config,
    replace_domain_with_placeholder,
    splice_rules,
)
from app.services.remnawave_nginx_sync import (
    NginxLinkError,
    apply_server_link,
    get_remnawave_nginx_path,
    render_profile_for_server,
    sync_profile_to_servers,
)
from app.services.remnawave_nginx_validator import validate_config
from app.routers.proxy import get_server_by_id, proxy_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/remnawave-nginx-profiles", tags=["remnawave-nginx-profiles"])


# ==================== Schemas ====================

class ProfileCreate(BaseModel):
    name: str
    description: Optional[str] = None
    config_content: Optional[str] = None  # None = шаблон по умолчанию
    options: Optional[dict] = None


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    config_content: Optional[str] = None


class OptionsUpdate(BaseModel):
    options: dict


class ReorderRequest(BaseModel):
    profile_ids: list[int]


class ValidateRequest(BaseModel):
    config_content: str


class ImportFromNodeRequest(BaseModel):
    server_id: int


def _normalize_domain(value: str) -> str:
    value = value.strip().lower()
    if not DOMAIN_RE.match(value):
        raise ValueError(f"Некорректный домен: {value!r}")
    return value


class LinkServerRequest(BaseModel):
    """Домен нужен, только если шаблон профиля содержит {{DOMAIN}} —
    при wildcard-домене профиля ноде свой домен не требуется."""
    domain: Optional[str] = None

    @field_validator("domain")
    @classmethod
    def _check_domain(cls, v: Optional[str]) -> Optional[str]:
        if not v or not v.strip():
            return None
        return _normalize_domain(v)


class ServerDomainRequest(BaseModel):
    domain: str

    @field_validator("domain")
    @classmethod
    def _check_domain(cls, v: str) -> str:
        return _normalize_domain(v)


class RuleData(BaseModel):
    name: str
    rule_type: str  # grpc | xhttp | proxy
    service_path: Optional[str] = None
    port: Optional[int] = None
    path: Optional[str] = None
    target_url: Optional[str] = None

    @field_validator("rule_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        if v not in ("grpc", "xhttp", "proxy"):
            raise ValueError(f"Неизвестный тип правила: {v!r}")
        return v


# ==================== Helpers ====================

async def _get_profile(profile_id: int, db: AsyncSession) -> RemnawaveNginxProfile:
    profile = await db.get(RemnawaveNginxProfile, profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile


def _profile_options(profile: RemnawaveNginxProfile) -> ProfileOptions:
    try:
        data = json.loads(profile.options) if profile.options else None
    except (json.JSONDecodeError, TypeError):
        data = None
    return ProfileOptions.from_dict(data)


def _rule_from_data(data: RuleData) -> Rule:
    if data.rule_type == "grpc":
        if not data.service_path or data.port is None:
            raise HTTPException(400, "Для gRPC-правила нужны service_path и port")
        return GrpcRule(name=data.name, service_path=data.service_path, port=data.port)
    if data.rule_type == "xhttp":
        if not data.path or data.port is None:
            raise HTTPException(400, "Для XHTTP-правила нужны path и port")
        return XhttpRule(name=data.name, path=data.path, port=data.port)
    if not data.path or not data.target_url:
        raise HTTPException(400, "Для proxy-правила нужны path и target_url")
    return ProxyRule(name=data.name, path=data.path, target_url=data.target_url)


def _serialize_rule(rule: Rule) -> dict:
    if isinstance(rule, GrpcRule):
        return {"name": rule.name, "rule_type": "grpc",
                "service_path": rule.service_path, "port": rule.port}
    if isinstance(rule, XhttpRule):
        return {"name": rule.name, "rule_type": "xhttp",
                "path": rule.path, "port": rule.port}
    return {"name": rule.name, "rule_type": "proxy",
            "path": rule.path, "target_url": rule.target_url}


def _parse_or_400(config: str) -> list[Rule]:
    try:
        return parse_rules_from_config(config)
    except MissingMarkersError as e:
        raise HTTPException(400, str(e))


def _rules_payload(config: str) -> dict:
    if not has_markers(config):
        return {"has_markers": False, "rules": []}
    return {
        "has_markers": True,
        "rules": [_serialize_rule(r) for r in parse_rules_from_config(config)],
    }


async def _mark_drifted_pending(db: AsyncSession, profile: RemnawaveNginxProfile) -> None:
    """Помечает pending серверы, у которых per-server rendered hash разошёлся с профилем."""
    result = await db.execute(
        select(Server).where(
            Server.active_remnawave_nginx_profile_id == profile.id,
            Server.is_active.is_(True),
        )
    )
    drifted_ids: list[int] = []
    for server in result.scalars().all():
        rendered, error = render_profile_for_server(profile.config_content, server)
        expected = compute_config_hash(rendered) if not error else None
        if expected is None or server.remnawave_nginx_config_hash != expected:
            drifted_ids.append(server.id)

    if drifted_ids:
        await db.execute(
            update(Server).where(Server.id.in_(drifted_ids))
            .values(remnawave_nginx_sync_status="pending")
        )


async def _save_config_and_sync(
    profile: RemnawaveNginxProfile, new_config: str, db: AsyncSession, bg: BackgroundTasks,
) -> None:
    profile.config_content = new_config
    await _mark_drifted_pending(db, profile)
    await db.commit()
    bg.add_task(_bg_sync_profile, profile.id)


def _profile_summary(p: RemnawaveNginxProfile, total: int, synced: int) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "config_content": p.config_content,
        "options": _profile_options(p).to_dict(),
        "position": p.position,
        "linked_servers_count": total,
        "synced_servers_count": synced,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


# ==================== Static routes (must be before /{profile_id}) ====================

@router.get("/cloudflare-ranges")
async def get_cloudflare_ranges(_=Depends(verify_auth)):
    return {"ranges": CLOUDFLARE_RANGES}


@router.get("/available-servers")
async def get_available_servers(db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    result = await db.execute(
        select(
            Server.id, Server.name, Server.url,
            Server.active_remnawave_nginx_profile_id,
            Server.remnawave_nginx_sync_status,
            Server.remnawave_nginx_detected,
            Server.remnawave_nginx_domain,
            Server.folder,
        )
        .where(Server.is_active.is_(True))
        .order_by(Server.name)
    )
    return [
        {
            "id": row[0],
            "name": row[1],
            "url": row[2],
            "active_profile_id": row[3],
            "sync_status": row[4],
            "detected": row[5],
            "domain": row[6],
            "folder": row[7],
        }
        for row in result.fetchall()
    ]


@router.post("/validate")
async def validate_profile_config(data: ValidateRequest, _=Depends(verify_auth)):
    """Проверяет конфиг на панели через `nginx -t` до раскатки на серверы."""
    valid, message = await validate_config(data.config_content)
    return {"valid": valid, "message": message}


@router.post("/import-from-node")
async def import_from_node(
    data: ImportFromNodeRequest, db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    """Забирает текущий nginx.conf с ноды как основу профиля;
    распознанный домен заменяется на {{DOMAIN}}."""
    install_path = await get_remnawave_nginx_path(db)
    server = await get_server_by_id(data.server_id, db)
    result = await proxy_request(
        server, "/api/remnawave/nginx/config", params={"path": install_path}, timeout=20.0,
    )
    content = result.get("content")
    if not result.get("exists") or content is None:
        raise HTTPException(404, "nginx.conf не найден на ноде по настроенному пути")

    detected = detect_domain(content)
    if detected:
        content = replace_domain_with_placeholder(content, detected)
    return {"content": content, "detected_domain": detected, "has_markers": has_markers(content)}


# ==================== CRUD ====================

@router.get("/")
async def list_profiles(db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    result = await db.execute(
        select(RemnawaveNginxProfile).order_by(RemnawaveNginxProfile.position, RemnawaveNginxProfile.id)
    )
    profiles = result.scalars().all()
    profile_ids = [p.id for p in profiles]

    counts: dict[int, dict] = {pid: {"total": 0, "synced": 0} for pid in profile_ids}
    if profile_ids:
        srv_result = await db.execute(
            select(Server.active_remnawave_nginx_profile_id, Server.remnawave_nginx_sync_status, func.count())
            .where(Server.active_remnawave_nginx_profile_id.in_(profile_ids))
            .group_by(Server.active_remnawave_nginx_profile_id, Server.remnawave_nginx_sync_status)
        )
        for prof_id, sync_st, cnt in srv_result.fetchall():
            counts[prof_id]["total"] += cnt
            if sync_st == "synced":
                counts[prof_id]["synced"] += cnt

    return [_profile_summary(p, counts[p.id]["total"], counts[p.id]["synced"]) for p in profiles]


@router.post("/")
async def create_profile(data: ProfileCreate, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    existing = await db.execute(
        select(RemnawaveNginxProfile).where(RemnawaveNginxProfile.name == data.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"Profile '{data.name}' already exists")

    options = ProfileOptions.from_dict(data.options)
    config_content = data.config_content
    if not config_content:
        try:
            config_content = generate_full_config(options, [])
        except OptionsValidationError as e:
            raise HTTPException(400, str(e))

    max_pos = await db.execute(select(func.max(RemnawaveNginxProfile.position)))
    profile = RemnawaveNginxProfile(
        name=data.name,
        description=data.description,
        config_content=config_content,
        options=json.dumps(options.to_dict()),
        position=(max_pos.scalar() or 0) + 1,
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return _profile_summary(profile, 0, 0)


@router.get("/{profile_id}")
async def get_profile(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)

    result = await db.execute(
        select(Server).where(Server.active_remnawave_nginx_profile_id == profile_id).order_by(Server.name)
    )
    servers = result.scalars().all()

    server_items = []
    for s in servers:
        rendered, render_error = render_profile_for_server(profile.config_content, s)
        expected_hash = compute_config_hash(rendered) if not render_error else None
        server_items.append({
            "server_id": s.id,
            "server_name": s.name,
            "domain": s.remnawave_nginx_domain,
            "sync_status": s.remnawave_nginx_sync_status,
            "config_hash": s.remnawave_nginx_config_hash,
            "is_synced": expected_hash is not None and s.remnawave_nginx_config_hash == expected_hash,
            "detected": s.remnawave_nginx_detected,
            "last_sync_at": s.remnawave_nginx_last_sync_at.isoformat() if s.remnawave_nginx_last_sync_at else None,
        })

    summary = _profile_summary(
        profile,
        len(server_items),
        sum(1 for item in server_items if item["sync_status"] == "synced"),
    )
    summary["servers"] = server_items
    summary["has_markers"] = has_markers(profile.config_content)
    return summary


@router.put("/{profile_id}")
async def update_profile(
    profile_id: int, data: ProfileUpdate, bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)
    config_changed = False

    if data.name is not None and data.name != profile.name:
        dup = await db.execute(
            select(RemnawaveNginxProfile).where(
                RemnawaveNginxProfile.name == data.name,
                RemnawaveNginxProfile.id != profile_id,
            )
        )
        if dup.scalar_one_or_none():
            raise HTTPException(400, f"Profile '{data.name}' already exists")
        profile.name = data.name

    if data.description is not None:
        profile.description = data.description
    if data.config_content is not None:
        valid, message = await validate_config(data.config_content)
        if not valid:
            raise HTTPException(400, f"Конфиг не прошёл проверку nginx: {message}")
        profile.config_content = data.config_content
        config_changed = True
        await _mark_drifted_pending(db, profile)

    await db.commit()
    await db.refresh(profile)

    if config_changed:
        bg.add_task(_bg_sync_profile, profile_id)

    return {"success": True, "id": profile.id}


@router.delete("/{profile_id}")
async def delete_profile(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)

    # Отвязать серверы (домен — свойство ноды, сохраняем)
    await db.execute(
        update(Server)
        .where(Server.active_remnawave_nginx_profile_id == profile_id)
        .values(
            active_remnawave_nginx_profile_id=None,
            remnawave_nginx_sync_status=None,
        )
    )
    await db.delete(profile)
    await db.commit()
    return {"success": True}


# ==================== Options ====================

@router.put("/{profile_id}/options")
async def update_options(
    profile_id: int, data: OptionsUpdate, bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    """Опции меняют структуру server-блока — конфиг пересобирается целиком
    из шаблона с сохранением текущих правил."""
    profile = await _get_profile(profile_id, db)
    options = ProfileOptions.from_dict(data.options)

    rules = parse_rules_from_config(profile.config_content) if has_markers(profile.config_content) else []

    try:
        new_config = generate_full_config(options, rules)
    except (OptionsValidationError, RuleValidationError) as e:
        raise HTTPException(400, str(e))

    profile.options = json.dumps(options.to_dict())
    await _save_config_and_sync(profile, new_config, db, bg)
    return {"success": True, "config_content": new_config}


@router.post("/{profile_id}/regenerate-config")
async def regenerate_config(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    """Пересобрать конфиг по стандартному шаблону из опций и текущих правил (без сохранения)."""
    profile = await _get_profile(profile_id, db)
    options = _profile_options(profile)

    rules = parse_rules_from_config(profile.config_content) if has_markers(profile.config_content) else []

    try:
        regenerated = generate_full_config(options, rules)
    except (OptionsValidationError, RuleValidationError) as e:
        raise HTTPException(400, str(e))
    return {"config_content": regenerated}


# ==================== Server Binding ====================

@router.post("/{profile_id}/servers/{server_id}")
async def link_server(
    profile_id: int, server_id: int, data: LinkServerRequest, bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)

    server = await db.get(Server, server_id)
    if not server:
        raise HTTPException(404, "Server not found")

    try:
        apply_server_link(profile, server, data.domain)
    except NginxLinkError as e:
        raise HTTPException(400, str(e))
    await db.commit()

    bg.add_task(_bg_sync_server, profile_id, server_id)
    return {"success": True}


@router.put("/{profile_id}/servers/{server_id}/domain")
async def update_server_domain(
    profile_id: int, server_id: int, data: ServerDomainRequest, bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    await _get_profile(profile_id, db)

    server = await db.get(Server, server_id)
    if not server or server.active_remnawave_nginx_profile_id != profile_id:
        raise HTTPException(404, "Server not linked to this profile")

    server.remnawave_nginx_domain = data.domain
    server.remnawave_nginx_sync_status = "pending"
    await db.commit()

    bg.add_task(_bg_sync_server, profile_id, server_id)
    return {"success": True}


@router.delete("/{profile_id}/servers/{server_id}")
async def unlink_server(profile_id: int, server_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    server = await db.get(Server, server_id)
    if not server:
        raise HTTPException(404, "Server not found")

    # Домен сохраняем — это свойство ноды, пригодится при повторной привязке.
    # Конфиг на ноде не трогаем: nginx принадлежит Remnawave-установке.
    server.active_remnawave_nginx_profile_id = None
    server.remnawave_nginx_sync_status = None
    server.remnawave_nginx_config_hash = None
    server.remnawave_nginx_node_hash = None
    server.remnawave_nginx_last_sync_at = None
    await db.commit()
    return {"success": True}


# ==================== Sync ====================

@router.post("/{profile_id}/sync")
async def sync_all(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)
    results = await sync_profile_to_servers(profile, db)
    return {
        "results": [
            {"server_id": r.server_id, "server_name": r.server_name,
             "success": r.success, "message": r.message, "status": r.status}
            for r in results
        ]
    }


@router.post("/{profile_id}/sync/{server_id}")
async def sync_one(profile_id: int, server_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)
    results = await sync_profile_to_servers(profile, db, server_ids=[server_id])
    if not results:
        raise HTTPException(404, "Server not linked to this profile or inactive")
    r = results[0]
    return {"server_id": r.server_id, "server_name": r.server_name,
            "success": r.success, "message": r.message, "status": r.status}


# ==================== Sync Log ====================

@router.get("/{profile_id}/log")
async def get_sync_log(profile_id: int, limit: int = 50, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    await _get_profile(profile_id, db)

    result = await db.execute(
        select(RemnawaveNginxSyncLog)
        .where(RemnawaveNginxSyncLog.profile_id == profile_id)
        .order_by(RemnawaveNginxSyncLog.created_at.desc())
        .limit(limit)
    )
    logs = result.scalars().all()

    server_ids = list({l.server_id for l in logs})
    server_names = {}
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
            "config_hash": l.config_hash,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        }
        for l in logs
    ]


# ==================== Servers status (для поллинга страницы) ====================

@router.get("/{profile_id}/servers-status")
async def get_servers_status(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)

    result = await db.execute(
        select(Server).where(Server.active_remnawave_nginx_profile_id == profile_id).order_by(Server.name)
    )
    servers = list(result.scalars().all())

    items = []
    for s in servers:
        rendered, render_error = render_profile_for_server(profile.config_content, s)
        expected_hash = compute_config_hash(rendered) if not render_error else None
        items.append({
            "server_id": s.id,
            "server_name": s.name,
            "online": is_server_online(s),
            "domain": s.remnawave_nginx_domain,
            "sync_status": s.remnawave_nginx_sync_status,
            "is_synced": expected_hash is not None and s.remnawave_nginx_config_hash == expected_hash,
            "detected": s.remnawave_nginx_detected,
            "last_sync_at": s.remnawave_nginx_last_sync_at.isoformat() if s.remnawave_nginx_last_sync_at else None,
        })
    return items


# ==================== Rules ====================

@router.get("/{profile_id}/rules")
async def get_rules(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)
    return _rules_payload(profile.config_content)


@router.post("/{profile_id}/rules")
async def add_rule(
    profile_id: int, data: RuleData, bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)
    rule = _rule_from_data(data)

    rules = _parse_or_400(profile.config_content)
    if any(r.name == rule.name for r in rules):
        raise HTTPException(400, f"Rule '{rule.name}' already exists")

    new_config = _splice_or_400(profile, [*rules, rule])
    await _save_config_and_sync(profile, new_config, db, bg)
    return {"success": True, **_rules_payload(new_config)}


@router.put("/{profile_id}/rules/{rule_name}")
async def update_rule(
    profile_id: int, rule_name: str, data: RuleData, bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)
    rule = _rule_from_data(data)

    rules = _parse_or_400(profile.config_content)
    if not any(r.name == rule_name for r in rules):
        raise HTTPException(404, f"Rule '{rule_name}' not found")

    # Замена на месте, а не удаление с добавлением в конец: порядок локаций
    # определяет содержимое конфига, а значит и хэш синхронизации
    updated = [rule if r.name == rule_name else r for r in rules]

    new_config = _splice_or_400(profile, updated)
    await _save_config_and_sync(profile, new_config, db, bg)
    return {"success": True, **_rules_payload(new_config)}


@router.delete("/{profile_id}/rules/{rule_name}")
async def delete_rule(
    profile_id: int, rule_name: str, bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db), _=Depends(verify_auth),
):
    profile = await _get_profile(profile_id, db)

    rules = _parse_or_400(profile.config_content)
    if not any(r.name == rule_name for r in rules):
        raise HTTPException(404, f"Rule '{rule_name}' not found")

    new_config = _splice_or_400(profile, [r for r in rules if r.name != rule_name])
    await _save_config_and_sync(profile, new_config, db, bg)
    return {"success": True, **_rules_payload(new_config)}


def _splice_or_400(profile: RemnawaveNginxProfile, rules: list[Rule]) -> str:
    try:
        return splice_rules(profile.config_content, rules, _profile_options(profile))
    except (MissingMarkersError, RuleValidationError) as e:
        raise HTTPException(400, str(e))


# ==================== Background tasks ====================

async def _bg_sync_profile(profile_id: int):
    """Фоновая синхронизация профиля на все привязанные серверы."""
    async with async_session_maker() as db:
        try:
            profile = await db.get(RemnawaveNginxProfile, profile_id)
            if profile:
                await sync_profile_to_servers(profile, db)
        except Exception as e:
            logger.error("Background remnawave nginx sync failed for profile %s: %s", profile_id, e)


async def _bg_sync_server(profile_id: int, server_id: int):
    """Раскатка конфига на один (привязанный) сервер, с автоподъёмом контейнера."""
    async with async_session_maker() as db:
        try:
            profile = await db.get(RemnawaveNginxProfile, profile_id)
            if profile:
                await sync_profile_to_servers(profile, db, server_ids=[server_id], ensure_started=True)
        except Exception as e:
            logger.error("Background remnawave nginx sync failed for server %s: %s", server_id, e)
