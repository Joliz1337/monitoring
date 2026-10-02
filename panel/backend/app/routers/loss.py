"""Страница «Потери»: сводка по адресам назначения релеев, ручная проверка и
трасса до адреса, правка бэкенда с этим адресом во всех профилях HAProxy и DNAT."""

import ipaddress
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import get_db
from app.models import Server
from app.services.backend_address import (
    Action,
    AddressEdit,
    AddressEditError,
    plan_batch,
    rollout_progress,
    suggest_addresses,
    validate_edit,
)
from app.services.backend_edit_jobs import get_edit_jobs
from app.services.loss_exclusions import LossExclusions, load_loss_exclusions, save_loss_exclusions, target_entry
from app.services.loss_overview import (
    TargetParseError,
    build_overview,
    check_from_servers,
    parse_endpoint,
    parse_target,
)
from app.services.loss_registry import ExcludedTarget, get_loss_registry
from app.services.loss_trace import TraceError, TraceErrorCode, analyze_trace, fetch_trace, start_trace
from app.services.server_alerter import get_server_alerter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/loss", tags=["loss"], dependencies=[Depends(verify_auth)])

MAX_CHECK_SERVERS = 500
MAX_BATCH_EDITS = 200
MAX_EXCLUDED_TARGETS = 2000


class LossCheckRequest(BaseModel):
    target: str = Field(..., min_length=1, max_length=64)
    server_ids: list[int] = Field(..., min_length=1, max_length=MAX_CHECK_SERVERS)


class TraceRequest(BaseModel):
    server_id: int
    target: str = Field(..., min_length=1, max_length=64)


TRACE_ERROR_STATUS = {
    TraceErrorCode.UNSUPPORTED: 409,
    TraceErrorCode.DENIED: 403,
    TraceErrorCode.BUSY: 429,
    TraceErrorCode.NOT_FOUND: 404,
    TraceErrorCode.UNREACHABLE: 502,
}


class BackendEditRequest(BaseModel):
    ip: str
    port: int = Field(..., ge=1, le=65535)
    all_ports: bool = False
    action: Action
    new_ip: Optional[str] = None
    new_port: Optional[int] = Field(None, ge=1, le=65535)

    @field_validator("ip", "new_ip")
    @classmethod
    def _unicast_ipv4(cls, value: Optional[str]) -> Optional[str]:
        # DNAT-профили держат только IPv4 — правка работает в тех же рамках
        if value is None:
            return None
        address = ipaddress.IPv4Address(value.strip())
        if address.is_unspecified or address.is_multicast:
            raise ValueError("must be a unicast IPv4 address")
        return str(address)

    def to_edit(self) -> AddressEdit:
        return AddressEdit(
            ip=self.ip, port=self.port, all_ports=self.all_ports, action=self.action,
            new_ip=self.new_ip, new_port=self.new_port,
        )


class ExcludedTargetIn(BaseModel):
    # "ip" — все порты адреса, "ip:port" / "[v6]:port" — один бэкенд
    target: str = Field(..., min_length=1, max_length=64)
    total_only: bool = False

    def to_excluded(self) -> ExcludedTarget:
        ip, port = parse_endpoint(self.target)
        return ExcludedTarget(ip, port, self.total_only)


class LossSettingsUpdate(BaseModel):
    excluded_server_ids: list[int] = Field(default_factory=list, max_length=5000)
    excluded_targets: list[ExcludedTargetIn] = Field(default_factory=list, max_length=MAX_EXCLUDED_TARGETS)


class BackendEditBatch(BaseModel):
    edits: list[BackendEditRequest] = Field(..., min_length=1, max_length=MAX_BATCH_EDITS)


@router.get("/overview")
async def get_overview():
    registry = get_loss_registry()
    return {
        "targets": build_overview(
            registry.fresh(), registry.owners(), get_server_alerter().loss_episodes(), registry.target_rules(),
        ),
    }


def _settings_response(exclusions: LossExclusions) -> dict:
    return {
        "excluded_server_ids": sorted(exclusions.server_ids),
        "excluded_targets": [target_entry(target) for target in exclusions.targets],
    }


@router.get("/settings")
async def get_loss_settings(db: AsyncSession = Depends(get_db)):
    return _settings_response(await load_loss_exclusions(db))


@router.put("/settings")
async def update_loss_settings(data: LossSettingsUpdate, db: AsyncSession = Depends(get_db)):
    targets = []
    for item in data.excluded_targets:
        try:
            targets.append(item.to_excluded())
        except TargetParseError as exc:
            raise HTTPException(status_code=400, detail=f"{item.target}: {exc}")
    exclusions = await save_loss_exclusions(db, data.excluded_server_ids, targets)
    logger.info(
        "loss_exclusions_updated servers=%s targets=%s", len(exclusions.server_ids), len(exclusions.targets),
    )
    return _settings_response(exclusions)


@router.post("/check")
async def check_target(data: LossCheckRequest, db: AsyncSession = Depends(get_db)):
    try:
        ip, port = parse_target(data.target)
    except TargetParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result = await db.execute(
        select(Server).where(Server.id.in_(data.server_ids), Server.is_active == True)  # noqa: E712
    )
    servers = list(result.scalars().all())
    # Сессию отпускаем до похода по нодам: медленная нода не держит коннект пула
    await db.commit()
    return {"ip": ip, "port": port, "results": await check_from_servers(servers, ip, port)}


@router.post("/backends/preview")
async def preview_backend_edits(data: BackendEditBatch, db: AsyncSession = Depends(get_db)):
    """Где стоят адреса и что сделает каждая правка списка; ничего не сохраняет."""
    edits = [item.to_edit() for item in data.edits]
    plan = await plan_batch(db, edits)
    snapshots = get_loss_registry().fresh()
    return {
        "items": [[change.to_dict() for change in changes] for changes in plan.items],
        "suggestions": {ip: suggest_addresses(snapshots, ip) for ip in dict.fromkeys(e.ip for e in edits)},
    }


@router.post("/backends/apply")
async def apply_backend_edits(data: BackendEditBatch, bg: BackgroundTasks):
    """Запускает задание: правка профилей, затем раскатка изменённых — прогресс в /loss/jobs."""
    edits = [item.to_edit() for item in data.edits]
    for index, edit in enumerate(edits):
        try:
            validate_edit(edit)
        except AddressEditError as exc:
            raise HTTPException(status_code=400, detail=f"#{index + 1} {edit.ip}:{edit.port}: {exc}")
    jobs = get_edit_jobs()
    job = jobs.create(edits)
    logger.info("backend_edit_job_started job=%s edits=%s", job.id, len(edits))
    bg.add_task(jobs.run, job)
    return {"job": job.to_dict(progress=[])}


async def _active_server(server_id: int, db: AsyncSession) -> Server:
    server = (await db.execute(
        select(Server).where(Server.id == server_id, Server.is_active == True)  # noqa: E712
    )).scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    # Сессию отпускаем до похода на ноду
    await db.commit()
    return server


def _trace_http_error(exc: TraceError) -> HTTPException:
    # detail — код: фронт сам переводит его в понятный текст
    return HTTPException(status_code=TRACE_ERROR_STATUS[exc.code], detail=exc.code.value)


@router.post("/trace")
async def start_path_trace(data: TraceRequest, db: AsyncSession = Depends(get_db)):
    """mtr с релея до адреса в фоне на ноде; ход — GET /loss/trace/{server_id}/{trace_id}."""
    try:
        ip, port = parse_target(data.target)
    except TargetParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    server = await _active_server(data.server_id, db)
    try:
        trace_id = await start_trace(server, ip, port)
    except TraceError as exc:
        raise _trace_http_error(exc)
    return {"trace_id": trace_id, "ip": ip, "port": port}


@router.get("/trace/{server_id}/{trace_id}")
async def get_path_trace(server_id: int, trace_id: str, db: AsyncSession = Depends(get_db)):
    server = await _active_server(server_id, db)
    try:
        trace = await fetch_trace(server, trace_id)
    except TraceError as exc:
        raise _trace_http_error(exc)
    owners = get_loss_registry().owners()
    for hop in trace.get("hops", []):
        hop["owner"] = owners.get(hop.get("host") or "")
    trace["analysis"] = analyze_trace(
        trace.get("hops", []), trace.get("ip", ""), trace.get("state") != "running", trace.get("rounds_done", 0),
    )
    return trace


@router.get("/jobs")
async def list_edit_jobs(db: AsyncSession = Depends(get_db)):
    return {
        "jobs": [job.to_dict(await rollout_progress(db, job.changed)) for job in get_edit_jobs().recent()],
    }
