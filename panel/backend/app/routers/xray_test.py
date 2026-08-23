"""Проверка прокси-конфигураций: разбор входа, запуск прогона, выгрузка.

Прогон возвращает job_id и живёт фоновой задачей; результаты читаются через
GET /xray-test/jobs/{id}/stream (NDJSON, переподключаемый) — прогон подписки
идёт минутами, и закрытая вкладка его не прерывает.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_auth
from app.database import async_session_maker, get_db
from app.routers.proxy import get_server_by_id, require_capability
from app.services.node_capabilities import Capability
from app.services.xray_test import (
    bundle,
    core_manager,
    core_registry,
    export,
    storage,
    subscription,
)
from app.services.xray_test.errors import XrayTestError
from app.services.xray_test.job_manager import XrayTestJob, get_xray_test_manager
from app.services.xray_test.matrix import build_matrix
from app.services.xray_test.models import Core, ProxyEndpoint
from app.services.xray_test.node_runner import NodeCoreRunner
from app.services.xray_test.parsers import json_config
from app.services.xray_test.probes import ProbeOptions
from app.services.xray_test.runner import CoreRunner, LocalCoreRunner
from app.services.xray_test.sanitize import sanitize_link

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xray-test", tags=["xray-test"])

SourceKind = Literal["links", "json", "subscription"]


class ParseRequest(BaseModel):
    source: SourceKind
    payload: str = Field(min_length=1, max_length=2_000_000)
    user_agent: Optional[str] = None


class RunRequest(BaseModel):
    source: SourceKind
    payload: str = Field(min_length=1, max_length=2_000_000)
    user_agent: Optional[str] = None
    source_name: Optional[str] = None
    selected: Optional[list[int]] = None
    sni_list: list[str] = Field(default_factory=list)
    sync_transport_host: bool = True
    location: str = "panel"
    concurrency: int = Field(default=4, ge=1, le=8)
    full: bool = True
    tls_inspect: bool = True
    measure_speed: bool = False


class SubscriptionProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["url", "links"] = "url"
    payload: str = Field(min_length=1, max_length=200_000)
    user_agent: Optional[str] = None


class SubscriptionProfileUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    payload: Optional[str] = Field(default=None, max_length=200_000)
    user_agent: Optional[str] = None


class CoreVersionRequest(BaseModel):
    core: Literal["xray", "sing-box"]
    version: Optional[str] = Field(default=None, max_length=40)


class SniSetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sni_list: list[str] = Field(default_factory=list)


class SniSetUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    sni_list: Optional[list[str]] = None


def _ndjson(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


def _domain_error(exc: XrayTestError) -> HTTPException:
    return HTTPException(400, {"code": exc.code, "message": str(exc)})


async def _load_endpoints(
    source: SourceKind, payload: str, user_agent: Optional[str]
) -> tuple[list[ProxyEndpoint], list[Optional[str]], list, list[str], Optional[str]]:
    """Вход любого вида → конфигурации, исходные ссылки, ошибки строк, отброшенное."""
    if source == "links":
        endpoints, links, errors = subscription.parse_links_text(payload)
        return endpoints, links, errors, [], None

    if source == "json":
        endpoints, dropped = json_config.parse_config(payload)
        return endpoints, [None] * len(endpoints), [], dropped, None

    body = await subscription.fetch_subscription(payload, user_agent)
    content = subscription.parse_subscription(body)
    return (
        content.endpoints, content.links, content.errors,
        content.dropped_sections, content.format.value,
    )


def _endpoint_view(index: int, endpoint: ProxyEndpoint, link: Optional[str]) -> dict:
    try:
        core: Optional[str] = core_manager.select_core(endpoint).value
        unsupported = None
    except XrayTestError as exc:
        core, unsupported = None, str(exc)

    return {
        "index": index,
        "remark": endpoint.remark,
        "protocol": endpoint.protocol.value,
        "address": endpoint.address,
        "port": endpoint.port,
        "sni": endpoint.tls.sni,
        "transport": endpoint.transport.kind.value,
        "security": endpoint.tls.security.value,
        "flow": endpoint.flow,
        "core": core,
        "unsupported": unsupported,
        "link": sanitize_link(link) if link else None,
    }


@router.post("/parse")
async def parse_input(req: ParseRequest, _: dict = Depends(verify_auth)):
    """Разобрать вход и показать, что будет проверяться, ничего не запуская."""
    try:
        endpoints, links, errors, dropped, detected = await _load_endpoints(
            req.source, req.payload, req.user_agent
        )
    except XrayTestError as exc:
        raise _domain_error(exc) from exc

    return {
        "format": detected,
        "dropped_sections": dropped,
        "configs": [
            _endpoint_view(index, endpoint, links[index] if index < len(links) else None)
            for index, endpoint in enumerate(endpoints)
        ],
        "errors": [
            {"line": item.line, "preview": item.preview, "reason": item.reason}
            for item in errors
        ],
    }


@router.post("/run")
async def start_run(req: RunRequest, _: dict = Depends(verify_auth)):
    try:
        endpoints, links, _errors, _dropped, _detected = await _load_endpoints(
            req.source, req.payload, req.user_agent
        )

        if req.selected is not None:
            chosen = set(req.selected)
            filtered = [(e, links[i] if i < len(links) else None)
                        for i, e in enumerate(endpoints) if i in chosen]
            endpoints = [item[0] for item in filtered]
            links = [item[1] for item in filtered]

        cells = build_matrix(
            endpoints, req.sni_list,
            sync_transport_host=req.sync_transport_host,
            links=links,
        )
        runner, location_name = await _resolve_runner(req.location, cells)
    except XrayTestError as exc:
        raise _domain_error(exc) from exc

    options = ProbeOptions(
        tcp=True,
        tls_inspect=req.tls_inspect,
        http=req.full,
        exit_identity=req.full,
        speed=req.measure_speed and req.full,
    )

    started_at = datetime.now(timezone.utc)

    async def persist(job: XrayTestJob) -> None:
        async with async_session_maker() as db:
            await storage.save_run(
                db,
                source=req.source,
                source_name=req.source_name,
                location=req.location,
                location_name=location_name,
                status=job.status,
                results=job.results,
                started_at=started_at,
            )

    try:
        job_id = get_xray_test_manager().start(
            cells, options, runner,
            location=req.location,
            concurrency=req.concurrency,
            on_finish=persist,
        )
    except XrayTestError as exc:
        raise _domain_error(exc) from exc

    return {"job_id": job_id, "total": len(cells)}


async def _resolve_runner(location: str, cells) -> tuple[CoreRunner, Optional[str]]:
    """panel — прогон у себя, node:<id> — на сервере из списка."""
    if location == "panel":
        return LocalCoreRunner(), None

    prefix, _, raw_id = location.partition(":")
    if prefix != "node" or not raw_id.isdigit():
        raise XrayTestError(f"Неизвестное место запуска: {location}")

    async with async_session_maker() as db:
        server = await get_server_by_id(int(raw_id), db)
    require_capability(server, Capability.EXEC, write=True)

    runner = NodeCoreRunner(server)
    await runner.prepare(cells)
    return runner, server.name


@router.get("/jobs")
async def list_jobs(_: dict = Depends(verify_auth)):
    return {"jobs": get_xray_test_manager().list_jobs()}


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str, _: dict = Depends(verify_auth)):
    manager = get_xray_test_manager()
    if manager.get(job_id) is None:
        raise HTTPException(404, "Задача проверки не найдена")

    async def generate():
        async for event in manager.subscribe(job_id):
            yield _ndjson(event)

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, _: dict = Depends(verify_auth)):
    if not get_xray_test_manager().cancel(job_id):
        raise HTTPException(404, "Задача не найдена или уже завершена")
    return {"success": True}


@router.get("/jobs/{job_id}/export")
async def export_job(
    job_id: str,
    fmt: Literal["links", "subscription", "csv", "json"] = Query("links"),
    include_degraded: bool = Query(True),
    _: dict = Depends(verify_auth),
):
    job = get_xray_test_manager().get(job_id)
    if job is None:
        raise HTTPException(404, "Задача не найдена")

    if fmt in ("links", "subscription"):
        links = export.working_links(job.results, include_degraded=include_degraded)
        body = "\n".join(links) if fmt == "links" else export.as_subscription(links)
        return PlainTextResponse(body, media_type="text/plain; charset=utf-8")

    if fmt == "csv":
        return PlainTextResponse(
            export.as_csv(job.results),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="xray-test-{job_id}.csv"'},
        )

    return PlainTextResponse(
        export.as_json(job.results), media_type="application/json; charset=utf-8"
    )


@router.get("/cores")
async def list_cores(_: dict = Depends(verify_auth)):
    """Состояние ядер: что выбрано, что установлено, какая версия сейчас в деле."""
    arch = core_manager.detect_arch()
    cores = []
    for core in Core:
        selected = core_manager.selected_version(core)
        installed = core_manager.installed_versions(core)
        resolved: Optional[str] = None
        error: Optional[str] = None
        try:
            resolved = (await core_manager.resolve_release(core)).version
        except XrayTestError as exc:
            error = str(exc)

        cores.append({
            "core": core.value,
            "selected": selected,
            "resolved": resolved,
            "installed": installed,
            "ready": bool(resolved and resolved in installed),
            "pinned": core_manager.PINNED_RELEASES[core].version,
            "error": error,
        })
    return {"cores": cores, "arch": arch}


@router.get("/cores/releases")
async def list_core_releases(
    core: Literal["xray", "sing-box"] = Query(...),
    refresh: bool = Query(False),
    _: dict = Depends(verify_auth),
):
    """Опубликованные версии ядра, включая пре-релизы."""
    target = Core(core)
    try:
        releases = await core_registry.list_releases(target, refresh=refresh)
    except XrayTestError as exc:
        raise _domain_error(exc) from exc

    installed = set(core_manager.installed_versions(target))
    return {
        "releases": [
            {
                "version": item.version,
                "tag": item.tag,
                "prerelease": item.prerelease,
                "published_at": item.published_at,
                "available": item.available,
                "size": item.asset_size,
                "verifiable": bool(item.digest_url) or item.version == core_manager.PINNED_RELEASES[target].version,
                "installed": item.version in installed,
            }
            for item in releases
        ],
        "selected": core_manager.selected_version(target),
    }


@router.put("/cores/version")
async def choose_core_version(
    req: CoreVersionRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Закрепить версию ядра. `latest` — всегда самая свежая, включая пре-релиз."""
    target = Core(req.core)
    version = (req.version or core_registry.LATEST).strip()

    if version != core_registry.LATEST:
        try:
            await core_registry.resolve_version(target, version)
        except XrayTestError as exc:
            raise _domain_error(exc) from exc

    core_manager.set_selected_version(target, version)
    await storage.save_core_version(db, core_manager.SETTING_KEYS[target], version)
    return {"success": True, "core": target.value, "selected": version}


@router.post("/cores/download")
async def download_core(
    core: Literal["xray", "sing-box"] = Query(...),
    version: Optional[str] = Query(None),
    _: dict = Depends(verify_auth),
):
    try:
        target = Core(core)
        release = await core_manager.resolve_release(target, version)
        path = await core_manager.ensure_core(target, release.version)
    except XrayTestError as exc:
        raise _domain_error(exc) from exc
    return {"success": True, "version": release.version, "path": str(path)}


@router.delete("/cores/{core}/{version}")
async def delete_core_version(
    core: Literal["xray", "sing-box"], version: str, _: dict = Depends(verify_auth)
):
    try:
        core_manager.remove_version(Core(core), version)
    except XrayTestError as exc:
        raise _domain_error(exc) from exc
    return {"success": True}


@router.get("/bundle/{token}")
async def download_bundle(token: str):
    """Отдача ядра ноде.

    Единственный эндпоинт раздела без cookie-авторизации: нода приходит сюда
    без сессии панели. Пропуском служит одноразовый токен со сроком жизни в
    пять минут, а содержимое нода сверяет по SHA-256 из своей команды.
    """
    path = bundle.redeem(token)
    if path is None or not path.is_file():
        raise HTTPException(404, "Ссылка недействительна")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@router.get("/subscriptions")
async def list_subscription_profiles(
    db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)
):
    return {"profiles": await storage.list_subscriptions(db)}


@router.post("/subscriptions")
async def create_subscription_profile(
    req: SubscriptionProfileRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    try:
        return await storage.create_subscription(
            db, name=req.name, kind=req.kind, payload=req.payload, user_agent=req.user_agent
        )
    except XrayTestError as exc:
        raise _domain_error(exc) from exc


@router.patch("/subscriptions/{profile_id}")
async def update_subscription_profile(
    profile_id: int,
    req: SubscriptionProfileUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    try:
        return await storage.update_subscription(
            db, profile_id, name=req.name, payload=req.payload, user_agent=req.user_agent
        )
    except XrayTestError as exc:
        raise _domain_error(exc) from exc


@router.delete("/subscriptions/{profile_id}")
async def delete_subscription_profile(
    profile_id: int, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)
):
    try:
        await storage.delete_subscription(db, profile_id)
    except XrayTestError as exc:
        raise _domain_error(exc) from exc
    return {"success": True}


@router.get("/sni-sets")
async def list_sni_sets(db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)):
    return {"profiles": await storage.list_sni_sets(db)}


@router.post("/sni-sets")
async def create_sni_set(
    req: SniSetRequest, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)
):
    try:
        return await storage.create_sni_set(db, name=req.name, sni_list=req.sni_list)
    except XrayTestError as exc:
        raise _domain_error(exc) from exc


@router.patch("/sni-sets/{profile_id}")
async def update_sni_set(
    profile_id: int,
    req: SniSetUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    try:
        return await storage.update_sni_set(db, profile_id, name=req.name, sni_list=req.sni_list)
    except XrayTestError as exc:
        raise _domain_error(exc) from exc


@router.delete("/sni-sets/{profile_id}")
async def delete_sni_set(
    profile_id: int, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)
):
    try:
        await storage.delete_sni_set(db, profile_id)
    except XrayTestError as exc:
        raise _domain_error(exc) from exc
    return {"success": True}


@router.get("/history")
async def list_history(
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    return {"runs": await storage.list_runs(db, limit)}


@router.get("/history/{run_id}")
async def get_history_run(
    run_id: int, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)
):
    return {"results": await storage.get_run_results(db, run_id)}


@router.delete("/history/{run_id}")
async def delete_history_run(
    run_id: int, db: AsyncSession = Depends(get_db), _: dict = Depends(verify_auth)
):
    await storage.delete_run(db, run_id)
    return {"success": True}
