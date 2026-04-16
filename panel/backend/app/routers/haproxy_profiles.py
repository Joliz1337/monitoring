import asyncio
import socket

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy import select, update, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional
import json
import logging

from app.database import get_db, async_session_maker
from app.models import Server, HAProxyConfigProfile, HAProxySyncLog, ServerCache, MetricsSnapshot
from app.auth import verify_auth
from app.services.haproxy_profile_sync import sync_profile_to_servers, compute_config_hash
from app.services.haproxy_config import HAProxyRule, BackendServer, BalancerOptions, get_config_generator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/haproxy-profiles", tags=["haproxy-profiles"])


# ==================== Schemas ====================

class ProfileCreate(BaseModel):
    name: str
    description: Optional[str] = None
    config_content: Optional[str] = None  # None = шаблон по умолчанию


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    config_content: Optional[str] = None


class ReorderRequest(BaseModel):
    profile_ids: list[int]


class BackendServerData(BaseModel):
    name: str
    address: str
    port: int
    weight: int = 1
    maxconn: Optional[int] = None
    check: bool = True
    inter: str = "5s"
    fall: int = 3
    rise: int = 2
    send_proxy: bool = False
    send_proxy_v2: bool = False
    backup: bool = False
    slowstart: Optional[str] = None
    on_marked_down: Optional[str] = None
    on_marked_up: Optional[str] = None
    disabled: bool = False


class BalancerOptionsData(BaseModel):
    algorithm: str = "roundrobin"
    algorithm_param: Optional[str] = None
    hash_type: Optional[str] = None
    health_check_type: Optional[str] = None
    httpchk_method: Optional[str] = None
    httpchk_uri: Optional[str] = None
    httpchk_expect: Optional[str] = None
    sticky_type: Optional[str] = None
    cookie_name: Optional[str] = None
    cookie_options: Optional[str] = None
    stick_table_type: Optional[str] = None
    stick_table_size: Optional[str] = None
    stick_table_expire: Optional[str] = None
    retries: int = 3
    redispatch: bool = True
    allbackups: bool = False
    fullconn: Optional[int] = None
    timeout_queue: Optional[str] = None


class RuleData(BaseModel):
    name: str
    rule_type: str = "tcp"
    listen_port: int
    target_ip: str = ""
    target_port: int = 0
    cert_domain: Optional[str] = None
    target_ssl: bool = False
    send_proxy: bool = False
    use_wildcard: bool = False
    is_balancer: bool = False
    servers: list[BackendServerData] = []
    balancer_options: Optional[BalancerOptionsData] = None


# ==================== Available servers (must be before /{profile_id}) ====================

class ServerCoresRequest(BaseModel):
    profile_id: int
    addresses: list[str]


@router.post("/server-cores")
async def get_server_cores(
    data: ServerCoresRequest,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_auth),
):
    """Возвращает address → cores для backend-серверов, матчит по IP с нодами профиля."""
    profile_id = data.profile_id
    addresses = data.addresses
    if not addresses:
        return {}

    # Все серверы панели (backend-адреса могут указывать на любой сервер)
    result = await db.execute(select(Server))
    servers = list(result.scalars().all())
    if not servers:
        return {}

    loop = asyncio.get_event_loop()

    def _resolve(host: str) -> str | None:
        try:
            return socket.gethostbyname(host)
        except socket.gaierror:
            return None

    def _is_ip(s: str) -> bool:
        try:
            socket.inet_aton(s)
            return True
        except socket.error:
            return False

    from urllib.parse import urlparse

    # IP → cores для каждой ноды
    ip_cores: dict[str, int] = {}
    for s in servers:
        cores_val = None
        if s.last_metrics:
            try:
                m = json.loads(s.last_metrics)
                cores_val = m.get("cpu", {}).get("cores_logical")
            except (json.JSONDecodeError, AttributeError):
                pass
        if not cores_val or cores_val <= 0:
            logger.info("server-cores: server %s (%s) — no cores data", s.name, s.url)
            continue

        try:
            host = urlparse(s.url).hostname or ""
        except Exception:
            continue

        # Резолвим hostname ноды в IP
        if _is_ip(host):
            ip_cores[host] = cores_val
        else:
            resolved = await loop.run_in_executor(None, _resolve, host)
            if resolved:
                ip_cores[resolved] = cores_val
        logger.info("server-cores: node %s (%s) → host=%s, cores=%d", s.name, s.url, host, cores_val)

    logger.info("server-cores: ip_cores=%s", ip_cores)

    # Резолвим адреса backend-серверов и матчим
    result_map: dict[str, int] = {}
    for addr in addresses[:50]:
        if _is_ip(addr):
            ip = addr
        else:
            ip = await loop.run_in_executor(None, _resolve, addr)
        logger.info("server-cores: backend %s → ip=%s, match=%s", addr, ip, ip in ip_cores if ip else False)
        if ip and ip in ip_cores:
            result_map[addr] = ip_cores[ip]

    logger.info("server-cores: result=%s", result_map)
    return result_map


@router.get("/available-servers")
async def get_available_servers(db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    result = await db.execute(
        select(Server.id, Server.name, Server.url, Server.active_haproxy_profile_id, Server.haproxy_sync_status)
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
            update(HAProxyConfigProfile)
            .where(HAProxyConfigProfile.id == pid)
            .values(position=i)
        )
    await db.commit()
    return {"success": True}


# ==================== CRUD ====================

@router.get("/")
async def list_profiles(db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    result = await db.execute(
        select(HAProxyConfigProfile).order_by(HAProxyConfigProfile.position, HAProxyConfigProfile.id)
    )
    profiles = result.scalars().all()

    # Собираем ID всех серверов привязанных к профилям
    all_profile_ids = [p.id for p in profiles]
    profile_servers: dict[int, list[int]] = {pid: [] for pid in all_profile_ids}

    if all_profile_ids:
        srv_result = await db.execute(
            select(Server.id, Server.active_haproxy_profile_id, Server.haproxy_sync_status)
            .where(Server.active_haproxy_profile_id.in_(all_profile_ids))
        )
        for srv_id, prof_id, sync_st in srv_result.fetchall():
            profile_servers[prof_id].append(srv_id)

        # Последние снапшоты для подсчёта скорости сети
        all_server_ids = [sid for sids in profile_servers.values() for sid in sids]
        snap_speeds: dict[int, tuple[float, float]] = {}
        if all_server_ids:
            latest_sub = (
                select(
                    MetricsSnapshot.server_id,
                    func.max(MetricsSnapshot.id).label("max_id"),
                )
                .where(MetricsSnapshot.server_id.in_(all_server_ids))
                .group_by(MetricsSnapshot.server_id)
                .subquery()
            )
            snaps = await db.execute(
                select(MetricsSnapshot.server_id, MetricsSnapshot.net_rx_bytes_per_sec, MetricsSnapshot.net_tx_bytes_per_sec)
                .join(latest_sub, and_(
                    MetricsSnapshot.server_id == latest_sub.c.server_id,
                    MetricsSnapshot.id == latest_sub.c.max_id,
                ))
            )
            for sid, rx, tx in snaps.fetchall():
                snap_speeds[sid] = (rx or 0, tx or 0)

    # Считаем статистику
    srv_all = await db.execute(
        select(Server.active_haproxy_profile_id, Server.haproxy_sync_status, func.count())
        .where(Server.active_haproxy_profile_id.in_(all_profile_ids))
        .group_by(Server.active_haproxy_profile_id, Server.haproxy_sync_status)
    )
    counts: dict[int, dict] = {pid: {"total": 0, "synced": 0} for pid in all_profile_ids}
    for prof_id, sync_st, cnt in srv_all.fetchall():
        counts[prof_id]["total"] += cnt
        if sync_st == "synced":
            counts[prof_id]["synced"] += cnt

    items = []
    for p in profiles:
        total_rx = sum(snap_speeds.get(sid, (0, 0))[0] for sid in profile_servers.get(p.id, []))
        total_tx = sum(snap_speeds.get(sid, (0, 0))[1] for sid in profile_servers.get(p.id, []))

        items.append({
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "config_content": p.config_content,
            "position": p.position,
            "linked_servers_count": counts[p.id]["total"],
            "synced_servers_count": counts[p.id]["synced"],
            "total_net_rx": total_rx,
            "total_net_tx": total_tx,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        })

    return items


@router.post("/")
async def create_profile(data: ProfileCreate, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    existing = await db.execute(
        select(HAProxyConfigProfile).where(HAProxyConfigProfile.name == data.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"Profile '{data.name}' already exists")

    max_pos = await db.execute(select(func.max(HAProxyConfigProfile.position)))
    position = (max_pos.scalar() or 0) + 1

    config_content = data.config_content
    if not config_content:
        gen = get_config_generator()
        config_content = gen.generate_base_config()

    profile = HAProxyConfigProfile(
        name=data.name,
        description=data.description,
        config_content=config_content,
        position=position,
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)

    return {
        "id": profile.id,
        "name": profile.name,
        "description": profile.description,
        "config_content": profile.config_content,
        "position": profile.position,
        "linked_servers_count": 0,
        "synced_servers_count": 0,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


@router.get("/{profile_id}")
async def get_profile(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)

    result = await db.execute(
        select(Server).where(Server.active_haproxy_profile_id == profile_id).order_by(Server.name)
    )
    servers = result.scalars().all()

    config_hash = compute_config_hash(profile.config_content)

    return {
        "id": profile.id,
        "name": profile.name,
        "description": profile.description,
        "config_content": profile.config_content,
        "position": profile.position,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
        "servers": [
            {
                "server_id": s.id,
                "server_name": s.name,
                "sync_status": s.haproxy_sync_status,
                "config_hash": s.haproxy_config_hash,
                "is_synced": s.haproxy_config_hash == config_hash,
                "last_sync_at": s.haproxy_last_sync_at.isoformat() if s.haproxy_last_sync_at else None,
            }
            for s in servers
        ],
    }


@router.put("/{profile_id}")
async def update_profile(profile_id: int, data: ProfileUpdate, bg: BackgroundTasks, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)
    config_changed = False

    if data.name is not None and data.name != profile.name:
        dup = await db.execute(
            select(HAProxyConfigProfile).where(
                HAProxyConfigProfile.name == data.name,
                HAProxyConfigProfile.id != profile_id,
            )
        )
        if dup.scalar_one_or_none():
            raise HTTPException(400, f"Profile '{data.name}' already exists")
        profile.name = data.name

    if data.description is not None:
        profile.description = data.description
    if data.config_content is not None:
        profile.config_content = data.config_content
        config_changed = True

        new_hash = compute_config_hash(data.config_content)
        await db.execute(
            update(Server)
            .where(
                Server.active_haproxy_profile_id == profile_id,
                Server.haproxy_config_hash != new_hash,
            )
            .values(haproxy_sync_status="pending")
        )

    await db.commit()
    await db.refresh(profile)

    if config_changed:
        bg.add_task(_bg_sync_profile, profile_id)

    return {"success": True, "id": profile.id}


@router.delete("/{profile_id}")
async def delete_profile(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)

    # Отвязать серверы
    await db.execute(
        update(Server)
        .where(Server.active_haproxy_profile_id == profile_id)
        .values(
            active_haproxy_profile_id=None,
            haproxy_sync_status=None,
        )
    )

    await db.delete(profile)
    await db.commit()

    return {"success": True}


# ==================== Server Binding ====================

@router.post("/{profile_id}/servers/{server_id}")
async def link_server(profile_id: int, server_id: int, bg: BackgroundTasks, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    await _get_profile(profile_id, db)

    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Server not found")

    server.active_haproxy_profile_id = profile_id
    server.haproxy_sync_status = "pending"
    await db.commit()

    bg.add_task(_bg_sync_profile, profile_id)
    return {"success": True}


@router.delete("/{profile_id}/servers/{server_id}")
async def unlink_server(profile_id: int, server_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Server not found")

    server.active_haproxy_profile_id = None
    server.haproxy_sync_status = None
    server.haproxy_config_hash = None
    server.haproxy_last_sync_at = None
    await db.commit()

    return {"success": True}


# ==================== Sync ====================

@router.post("/{profile_id}/sync")
async def sync_all(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)
    results = await sync_profile_to_servers(profile, db)

    return {
        "results": [
            {"server_id": r.server_id, "server_name": r.server_name, "success": r.success, "message": r.message}
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
    return {"server_id": r.server_id, "server_name": r.server_name, "success": r.success, "message": r.message}


# ==================== Import ====================


# ==================== Sync Log ====================

@router.get("/{profile_id}/log")
async def get_sync_log(profile_id: int, limit: int = 50, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    await _get_profile(profile_id, db)

    result = await db.execute(
        select(HAProxySyncLog)
        .where(HAProxySyncLog.profile_id == profile_id)
        .order_by(HAProxySyncLog.created_at.desc())
        .limit(limit)
    )
    logs = result.scalars().all()

    # Получить имена серверов
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


@router.post("/{profile_id}/regenerate-config")
async def regenerate_config(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    """Перегенерировать конфиг из текущих правил по стандартному шаблону."""
    profile = await _get_profile(profile_id, db)
    gen = get_config_generator()
    rules = gen.parse_rules_from_config(profile.config_content)
    regenerated = gen.generate_full_config(rules)
    return {"config_content": regenerated}


async def _bg_sync_profile(profile_id: int):
    """Фоновая синхронизация профиля на все привязанные серверы."""
    async with async_session_maker() as db:
        try:
            result = await db.execute(
                select(HAProxyConfigProfile).where(HAProxyConfigProfile.id == profile_id)
            )
            profile = result.scalar_one_or_none()
            if profile:
                await sync_profile_to_servers(profile, db)
        except Exception as e:
            logger.error("Background sync failed for profile %s: %s", profile_id, e)


# ==================== Rules Management ====================

def _serialize_server(s: BackendServer) -> dict:
    return {
        "name": s.name, "address": s.address, "port": s.port,
        "weight": s.weight, "maxconn": s.maxconn,
        "check": s.check, "inter": s.inter, "fall": s.fall, "rise": s.rise,
        "send_proxy": s.send_proxy, "send_proxy_v2": s.send_proxy_v2,
        "backup": s.backup, "slowstart": s.slowstart,
        "on_marked_down": s.on_marked_down, "on_marked_up": s.on_marked_up,
        "disabled": s.disabled,
    }


def _serialize_balancer_options(opts: BalancerOptions) -> dict:
    return {
        "algorithm": opts.algorithm, "algorithm_param": opts.algorithm_param,
        "hash_type": opts.hash_type,
        "health_check_type": opts.health_check_type,
        "httpchk_method": opts.httpchk_method, "httpchk_uri": opts.httpchk_uri,
        "httpchk_expect": opts.httpchk_expect,
        "sticky_type": opts.sticky_type, "cookie_name": opts.cookie_name,
        "cookie_options": opts.cookie_options,
        "stick_table_type": opts.stick_table_type,
        "stick_table_size": opts.stick_table_size,
        "stick_table_expire": opts.stick_table_expire,
        "retries": opts.retries, "redispatch": opts.redispatch,
        "allbackups": opts.allbackups,
        "fullconn": opts.fullconn, "timeout_queue": opts.timeout_queue,
    }


def _serialize_rule(r: HAProxyRule) -> dict:
    result = {
        "name": r.name,
        "rule_type": r.rule_type,
        "listen_port": r.listen_port,
        "target_ip": r.target_ip,
        "target_port": r.target_port,
        "cert_domain": r.cert_domain,
        "target_ssl": r.target_ssl,
        "send_proxy": r.send_proxy,
        "use_wildcard": r.use_wildcard,
        "is_balancer": r.is_balancer,
        "servers": [_serialize_server(s) for s in r.servers],
        "balancer_options": _serialize_balancer_options(r.balancer_options) if r.balancer_options else None,
    }
    return result


def _rule_from_data(data: RuleData) -> HAProxyRule:
    servers = [BackendServer(**s.model_dump()) for s in data.servers]
    balancer_options = BalancerOptions(**data.balancer_options.model_dump()) if data.balancer_options else None
    return HAProxyRule(
        name=data.name, rule_type=data.rule_type,
        listen_port=data.listen_port, target_ip=data.target_ip,
        target_port=data.target_port, cert_domain=data.cert_domain,
        target_ssl=data.target_ssl, send_proxy=data.send_proxy,
        use_wildcard=data.use_wildcard, is_balancer=data.is_balancer,
        servers=servers, balancer_options=balancer_options,
    )


@router.get("/{profile_id}/rules")
async def get_rules(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)
    gen = get_config_generator()
    rules = gen.parse_rules_from_config(profile.config_content)
    return [_serialize_rule(r) for r in rules]


@router.post("/{profile_id}/rules")
async def add_rule(profile_id: int, data: RuleData, bg: BackgroundTasks, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)
    gen = get_config_generator()

    rule = _rule_from_data(data)
    ok, msg = gen.validate_rule(rule)
    if not ok:
        raise HTTPException(400, msg)

    existing_rules = gen.parse_rules_from_config(profile.config_content)
    if any(r.name == rule.name for r in existing_rules):
        raise HTTPException(400, f"Rule '{rule.name}' already exists")

    existing_rules.append(rule)
    profile.config_content = gen.generate_full_config(existing_rules)

    new_hash = compute_config_hash(profile.config_content)
    await db.execute(
        update(Server)
        .where(Server.active_haproxy_profile_id == profile_id, Server.haproxy_config_hash != new_hash)
        .values(haproxy_sync_status="pending")
    )
    await db.commit()

    bg.add_task(_bg_sync_profile, profile_id)
    return {"success": True, "rules": [_serialize_rule(r) for r in existing_rules]}


@router.put("/{profile_id}/rules/{rule_name}")
async def update_rule(profile_id: int, rule_name: str, data: RuleData, bg: BackgroundTasks, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)
    gen = get_config_generator()

    rule = _rule_from_data(data)
    ok, msg = gen.validate_rule(rule)
    if not ok:
        raise HTTPException(400, msg)

    existing_rules = gen.parse_rules_from_config(profile.config_content)
    idx = next((i for i, r in enumerate(existing_rules) if r.name == rule_name), None)
    if idx is None:
        raise HTTPException(404, f"Rule '{rule_name}' not found")

    existing_rules[idx] = rule
    profile.config_content = gen.generate_full_config(existing_rules)

    new_hash = compute_config_hash(profile.config_content)
    await db.execute(
        update(Server)
        .where(Server.active_haproxy_profile_id == profile_id, Server.haproxy_config_hash != new_hash)
        .values(haproxy_sync_status="pending")
    )
    await db.commit()

    bg.add_task(_bg_sync_profile, profile_id)
    return {"success": True, "rules": [_serialize_rule(r) for r in existing_rules]}


@router.delete("/{profile_id}/rules/{rule_name}")
async def delete_rule(profile_id: int, rule_name: str, bg: BackgroundTasks, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    profile = await _get_profile(profile_id, db)
    gen = get_config_generator()

    existing_rules = gen.parse_rules_from_config(profile.config_content)
    new_rules = [r for r in existing_rules if r.name != rule_name]
    if len(new_rules) == len(existing_rules):
        raise HTTPException(404, f"Rule '{rule_name}' not found")

    profile.config_content = gen.generate_full_config(new_rules)

    new_hash = compute_config_hash(profile.config_content)
    await db.execute(
        update(Server)
        .where(Server.active_haproxy_profile_id == profile_id, Server.haproxy_config_hash != new_hash)
        .values(haproxy_sync_status="pending")
    )
    await db.commit()

    bg.add_task(_bg_sync_profile, profile_id)
    return {"success": True, "rules": [_serialize_rule(r) for r in new_rules]}


# ==================== Server Metrics ====================

@router.get("/{profile_id}/servers-status")
async def get_servers_status(profile_id: int, db: AsyncSession = Depends(get_db), _=Depends(verify_auth)):
    await _get_profile(profile_id, db)

    result = await db.execute(
        select(Server).where(Server.active_haproxy_profile_id == profile_id).order_by(Server.name)
    )
    servers = list(result.scalars().all())
    if not servers:
        return []

    server_ids = [s.id for s in servers]

    cache_result = await db.execute(
        select(ServerCache).where(ServerCache.server_id.in_(server_ids))
    )
    cache_map = {c.server_id: c for c in cache_result.scalars().all()}

    # Последний снапшот метрик для каждого сервера (содержит скорость сети)
    latest_snapshots_sub = (
        select(
            MetricsSnapshot.server_id,
            func.max(MetricsSnapshot.id).label("max_id"),
        )
        .where(MetricsSnapshot.server_id.in_(server_ids))
        .group_by(MetricsSnapshot.server_id)
        .subquery()
    )
    snap_result = await db.execute(
        select(MetricsSnapshot)
        .join(latest_snapshots_sub, and_(
            MetricsSnapshot.server_id == latest_snapshots_sub.c.server_id,
            MetricsSnapshot.id == latest_snapshots_sub.c.max_id,
        ))
    )
    snap_map = {snap.server_id: snap for snap in snap_result.scalars().all()}

    items = []
    for s in servers:
        metrics = None
        haproxy_running = None

        snap = snap_map.get(s.id)
        if s.last_metrics:
            try:
                m = json.loads(s.last_metrics)
                cpu_data = m.get("cpu", {})
                mem_data = m.get("memory", {}).get("ram", {})
                metrics = {
                    "cpu": cpu_data.get("usage_percent"),
                    "ram": mem_data.get("percent"),
                    "net_rx": snap.net_rx_bytes_per_sec if snap else None,
                    "net_tx": snap.net_tx_bytes_per_sec if snap else None,
                    "la1": cpu_data.get("load_avg_1"),
                    "cores": cpu_data.get("cores_logical"),
                }
            except (json.JSONDecodeError, AttributeError):
                pass

        cache = cache_map.get(s.id)
        if cache and cache.last_haproxy_data:
            try:
                hdata = json.loads(cache.last_haproxy_data)
                status = hdata.get("status", {})
                haproxy_running = status.get("running")
            except (json.JSONDecodeError, AttributeError):
                pass

        items.append({
            "server_id": s.id,
            "server_name": s.name,
            "server_url": s.url,
            "sync_status": s.haproxy_sync_status,
            "config_hash": s.haproxy_config_hash,
            "last_sync_at": s.haproxy_last_sync_at.isoformat() if s.haproxy_last_sync_at else None,
            "haproxy_running": haproxy_running,
            "metrics": metrics,
        })

    return items


# ==================== Helpers ====================

async def _get_profile(profile_id: int, db: AsyncSession) -> HAProxyConfigProfile:
    result = await db.execute(
        select(HAProxyConfigProfile).where(HAProxyConfigProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile
