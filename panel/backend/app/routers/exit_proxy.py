"""Exit-прокси: настройки, ноды со статусом выходов, проверки, сниппет Remnawave, журнал, WARP.

Роутер тонкий: правила выбора выхода исполняет нода, доставка и статус — в
services/exit_proxy. Здесь только приём и валидация значений от UI.
"""

import asyncio
import json
import re
import secrets
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import ExitProxyEvent, ExitProxyNode, ExitProxySettings, Server
from app.routers.proxy import get_server_by_id, require_capability
from app.services.deploy_service import build_warp_install_command
from app.services.exit_proxy.node_client import (
    MIN_NODE_VERSION_EXIT_PROXY,
    ExitProxyNodeDenied,
    ExitProxyNodeError,
    ExitProxyNodeUnsupported,
    node_supports_exit_proxy,
)
from app.services.exit_proxy.render import remnawave_snippet
from app.services.exit_proxy.service import SYNC_PENDING, get_exit_proxy_service
from app.services.exit_proxy.settings import (
    BUILTIN_CHECK_KEYS,
    DEFAULT_BUILTIN_CHECKS,
    RESERVED_SERVICE_PORTS,
    get_or_create_settings,
    load_json,
)
from app.services.exit_proxy.views import node_view
from app.services.exit_proxy.warp_install import get_warp_install_manager
from app.services.haproxy_profile_sync import is_server_online
from app.services.node_capabilities import Capability
from app.services.reserved_ports_sync import apply_reserved_ports

router = APIRouter(prefix="/exit-proxy", tags=["exit-proxy"])

COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
TSV_FORBIDDEN_RE = re.compile(r"[\t\r\n\x1f]")
MAX_CUSTOM_CHECKS = 20
MAX_LOG_LIMIT = 500


# ── схемы ──


class SettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    check_interval_min: Optional[int] = Field(None, ge=1, le=1440)
    port: Optional[int] = Field(None, ge=1024, le=65535)
    blocked_countries: Optional[list[str]] = None
    notify_enabled: Optional[bool] = None

    @field_validator("port")
    @classmethod
    def _port_is_free(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value in RESERVED_SERVICE_PORTS:
            raise ValueError(f"port {value} is used by the node agent or its services")
        return value

    @field_validator("blocked_countries")
    @classmethod
    def _iso_countries(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        normalized: list[str] = []
        for raw in value:
            code = raw.strip().upper()
            if not COUNTRY_RE.match(code):
                raise ValueError(f"'{raw}' is not an ISO-2 country code")
            if code not in normalized:
                normalized.append(code)
        return normalized


class NodeUpdate(BaseModel):
    enabled: Optional[bool] = None
    select_mode: Optional[Literal["auto", "manual"]] = None
    pinned_candidate: Optional[str] = Field(None, max_length=64)
    candidates_order: Optional[list[str]] = None
    candidates_disabled: Optional[list[str]] = None

    @field_validator("candidates_order", "candidates_disabled")
    @classmethod
    def _short_ids(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        cleaned = [item.strip() for item in value if item and item.strip()]
        if any(len(item) > 64 for item in cleaned):
            raise ValueError("candidate id is too long")
        return cleaned


class SwitchRequest(BaseModel):
    tag: str = Field(..., min_length=1, max_length=64)


class CustomCheckInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    url: str = Field(..., max_length=512, pattern=r"^https?://")
    enabled: bool = True
    block_status: list[int] = Field(default_factory=list)
    block_regex: str = Field("", max_length=256)
    block_url_regex: str = Field("", max_length=256)
    expect_status: Optional[int] = Field(None, ge=100, le=599)

    @field_validator("name", "url", "block_regex", "block_url_regex")
    @classmethod
    def _plain_text(cls, value: str) -> str:
        if TSV_FORBIDDEN_RE.search(value):
            raise ValueError("control characters are not allowed")
        return value.strip()

    @field_validator("block_status")
    @classmethod
    def _valid_statuses(cls, value: list[int]) -> list[int]:
        unique = sorted(set(value))
        if any(not 100 <= code <= 599 for code in unique):
            raise ValueError("HTTP status must be within 100..599")
        return unique


class BuiltinCheckUpdate(BaseModel):
    enabled: bool


# ── хелперы ──


def _settings_view(row: ExitProxySettings) -> dict:
    return {
        "enabled": bool(row.enabled),
        "check_interval_min": row.check_interval_minutes,
        "port": row.port,
        "blocked_countries": load_json(row.blocked_countries, []),
        "notify_enabled": bool(row.telegram_enabled),
        "min_node_version": MIN_NODE_VERSION_EXIT_PROXY,
        "last_cycle_at": row.last_cycle_at.isoformat() if row.last_cycle_at else None,
        "last_cycle_error": row.last_cycle_error,
    }


def _checks_view(row: ExitProxySettings) -> dict:
    builtin = dict(DEFAULT_BUILTIN_CHECKS)
    builtin.update({key: bool(value) for key, value in load_json(row.builtin_checks, {}).items() if key in builtin})
    return {
        "builtin": [{"key": key, "enabled": enabled} for key, enabled in builtin.items()],
        "custom": load_json(row.custom_checks, []),
    }


async def _mark_all_pending(db: AsyncSession) -> None:
    """Конфиг ноды содержит общие настройки — после их смены его надо довезти всем."""
    await db.execute(update(ExitProxyNode).values(sync_status=SYNC_PENDING))


async def _view_for(db: AsyncSession, server_id: int) -> dict:
    row = (await db.execute(
        select(Server, ExitProxyNode)
        .outerjoin(ExitProxyNode, ExitProxyNode.server_id == Server.id)
        .where(Server.id == server_id)
    )).first()
    if row is None:
        raise HTTPException(status_code=404)
    server, node = row
    return node_view(server, node, is_server_online(server))


def _node_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


def _ndjson(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


# ── настройки и проверки ──


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    return _settings_view(await get_or_create_settings(db))


@router.put("/settings")
async def update_settings(
    body: SettingsUpdate, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth),
):
    row = await get_or_create_settings(db)
    config_changed = False
    ports_changed = False
    if body.enabled is not None and body.enabled != bool(row.enabled):
        row.enabled = body.enabled
        ports_changed = True
    if body.check_interval_min is not None and body.check_interval_min != row.check_interval_minutes:
        row.check_interval_minutes = body.check_interval_min
        config_changed = True
    if body.port is not None and body.port != row.port:
        row.port = body.port
        config_changed = ports_changed = True
    if body.blocked_countries is not None:
        encoded = json.dumps(body.blocked_countries)
        if encoded != (row.blocked_countries or ""):
            row.blocked_countries = encoded
            config_changed = True
    if body.notify_enabled is not None:
        row.telegram_enabled = body.notify_enabled
    if config_changed:
        await _mark_all_pending(db)
    await db.commit()
    await db.refresh(row)

    if config_changed or ports_changed:
        get_exit_proxy_service().trigger()
    if ports_changed:
        asyncio.create_task(apply_reserved_ports(reason="exit proxy port changed"))
    return _settings_view(row)


@router.get("/status")
async def get_status(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    row = await get_or_create_settings(db)
    return {
        **get_exit_proxy_service().get_status(),
        "enabled": bool(row.enabled),
        "port": row.port,
        "min_node_version": MIN_NODE_VERSION_EXIT_PROXY,
    }


@router.get("/checks")
async def get_checks(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    return _checks_view(await get_or_create_settings(db))


@router.put("/checks/builtin/{key}")
async def update_builtin_check(
    key: str, body: BuiltinCheckUpdate, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth),
):
    if key not in BUILTIN_CHECK_KEYS:
        raise HTTPException(status_code=404, detail="unknown builtin check")
    row = await get_or_create_settings(db)
    builtin = dict(DEFAULT_BUILTIN_CHECKS)
    builtin.update(load_json(row.builtin_checks, {}))
    builtin[key] = body.enabled
    row.builtin_checks = json.dumps(builtin)
    await _mark_all_pending(db)
    await db.commit()
    get_exit_proxy_service().trigger()
    return _checks_view(row)


@router.post("/checks/custom")
async def add_custom_check(
    body: CustomCheckInput, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth),
):
    row = await get_or_create_settings(db)
    checks = load_json(row.custom_checks, [])
    if len(checks) >= MAX_CUSTOM_CHECKS:
        raise HTTPException(status_code=400, detail=f"at most {MAX_CUSTOM_CHECKS} custom checks")
    checks.append({"id": secrets.token_hex(4), **body.model_dump()})
    row.custom_checks = json.dumps(checks, ensure_ascii=False)
    await _mark_all_pending(db)
    await db.commit()
    get_exit_proxy_service().trigger()
    return _checks_view(row)


@router.put("/checks/custom/{check_id}")
async def update_custom_check(
    check_id: str, body: CustomCheckInput, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth),
):
    row = await get_or_create_settings(db)
    checks = load_json(row.custom_checks, [])
    if not any(check.get("id") == check_id for check in checks):
        raise HTTPException(status_code=404, detail="check not found")
    row.custom_checks = json.dumps(
        [{"id": check_id, **body.model_dump()} if check.get("id") == check_id else check for check in checks],
        ensure_ascii=False,
    )
    await _mark_all_pending(db)
    await db.commit()
    get_exit_proxy_service().trigger()
    return _checks_view(row)


@router.delete("/checks/custom/{check_id}")
async def delete_custom_check(
    check_id: str, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth),
):
    row = await get_or_create_settings(db)
    checks = load_json(row.custom_checks, [])
    remaining = [check for check in checks if check.get("id") != check_id]
    if len(remaining) == len(checks):
        raise HTTPException(status_code=404, detail="check not found")
    row.custom_checks = json.dumps(remaining, ensure_ascii=False)
    await _mark_all_pending(db)
    await db.commit()
    get_exit_proxy_service().trigger()
    return _checks_view(row)


# ── ноды ──


@router.get("/nodes")
async def list_nodes(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    rows = (await db.execute(
        select(Server, ExitProxyNode)
        .outerjoin(ExitProxyNode, ExitProxyNode.server_id == Server.id)
        .where(Server.is_active == True)  # noqa: E712
        .order_by(Server.position, Server.id)
    )).all()
    return {"nodes": [node_view(server, node, is_server_online(server)) for server, node in rows]}


@router.put("/nodes/{server_id}")
async def update_node(
    server_id: int, body: NodeUpdate, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth),
):
    server = await get_server_by_id(server_id, db)
    node = (await db.execute(select(ExitProxyNode).where(ExitProxyNode.server_id == server_id))).scalar_one_or_none()
    enabling = body.enabled is True and (node is None or not node.enabled)
    if enabling:
        if not node_supports_exit_proxy(server.node_version):
            raise HTTPException(
                status_code=409,
                detail=f"агент {server.node_version or 'unknown'} старше {MIN_NODE_VERSION_EXIT_PROXY} — обновите ноду",
            )
        require_capability(server, Capability.SYSTEM, write=True)

    if node is None:
        if body.enabled is not True:
            raise HTTPException(status_code=409, detail="сначала включите exit-прокси на этой ноде")
        node = ExitProxyNode(server_id=server_id, enabled=True, sync_status=SYNC_PENDING)
        db.add(node)
    if body.enabled is not None:
        node.enabled = body.enabled
    if body.select_mode is not None:
        node.select_mode = body.select_mode
    if body.pinned_candidate is not None:
        node.pinned_candidate = body.pinned_candidate or None
    if body.candidates_order is not None:
        node.candidates_order = json.dumps(body.candidates_order)
    if body.candidates_disabled is not None:
        node.candidates_disabled = json.dumps(body.candidates_disabled)
    node.sync_status = SYNC_PENDING
    await db.commit()

    # Довезти сразу, не дожидаясь тика: ошибка ложится в sync_error, а офлайн-нода — в очередь
    await get_exit_proxy_service().sync_one(server_id)
    return await _view_for(db, server_id)


@router.post("/nodes/{server_id}/check-now")
async def check_now(server_id: int, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    try:
        await get_exit_proxy_service().check_now(server_id)
    except (LookupError, ExitProxyNodeError) as exc:
        raise _node_error(exc)
    return await _view_for(db, server_id)


@router.post("/nodes/{server_id}/switch")
async def switch_exit(
    server_id: int, body: SwitchRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth),
):
    try:
        await get_exit_proxy_service().switch(server_id, body.tag)
    except (LookupError, ExitProxyNodeError) as exc:
        raise _node_error(exc)
    return await _view_for(db, server_id)


@router.post("/nodes/{server_id}/install-warp")
async def install_warp(server_id: int, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    """Поставить Cloudflare WARP через агента: после установки он станет кандидатом-выходом."""
    server = await get_server_by_id(server_id, db)
    require_capability(server, Capability.EXEC, write=True)
    job_id = get_warp_install_manager().start(server, build_warp_install_command())
    return {"job_id": job_id}


@router.get("/warp-install/jobs")
async def list_warp_install_jobs(_: dict = Depends(verify_auth)):
    return {"jobs": get_warp_install_manager().list_jobs()}


@router.get("/warp-install/{job_id}/stream")
async def stream_warp_install_job(job_id: str, _: dict = Depends(verify_auth)):
    manager = get_warp_install_manager()
    if manager.get(job_id) is None:
        raise HTTPException(404, "Задача установки не найдена")

    async def generate():
        async for event in manager.subscribe(job_id):
            yield _ndjson(event)

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── сниппет и журнал ──


@router.get("/snippet")
async def get_snippet(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    row = await get_or_create_settings(db)
    return remnawave_snippet(row.port)


@router.get("/log")
async def get_log(
    limit: int = Query(100, ge=1, le=MAX_LOG_LIMIT),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    rows = (await db.execute(
        select(ExitProxyEvent, Server.name)
        .join(Server, Server.id == ExitProxyEvent.server_id)
        .order_by(ExitProxyEvent.created_at.desc(), ExitProxyEvent.id.desc())
        .limit(limit)
    )).all()
    return {
        "events": [
            {
                "id": event.id,
                "at": event.created_at.isoformat() if event.created_at else None,
                "server_id": event.server_id,
                "server_name": name,
                "kind": event.kind,
                "from": event.from_value,
                "to": event.to_value,
                "reason": event.reason,
            }
            for event, name in rows
        ]
    }
