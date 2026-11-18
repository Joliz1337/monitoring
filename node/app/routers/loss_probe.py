"""Ручная проверка потерь и трасса до адреса с этой ноды (страница «Потери» в панели)."""

from fastapi import APIRouter, HTTPException, Query

from app.capabilities import get_policy
from app.models.loss_probe import AllWaysResponse, LossCheckRequest, ProbeStats, TargetRequest, WaysResponse
from app.services.loss_probe import check_target, get_loss_probe, readable_sources
from app.services.path_trace import TraceBusyError, get_path_trace_manager

router = APIRouter(prefix="/api/loss-probe", tags=["loss-probe"])


@router.post("/check", response_model=ProbeStats)
async def check(request: LossCheckRequest) -> ProbeStats:
    """Серия попыток подключения к ip:port прямо сейчас; по просьбе — и проверка «как клиент».
    Обе идут с тех же адресов релея, что и трафик правила."""
    probe, target = get_loss_probe(), (request.ip, request.port)
    stats = await check_target(request.ip, request.port, request.attempts, exits=probe.exits_for(target))
    if request.client_probe:
        stats.answers = await probe.probe_as_client(target)
    return stats


@router.get("/ways", response_model=WaysResponse)
async def ways(ip: str, port: int = Query(443, ge=1, le=65535)) -> WaysResponse:
    """Потери и ответ сервера с каждого своего адреса релея: выходные адреса правила и
    остальные, проверяемые справочно. Читается из уже набранных окон — подключений не делает."""
    try:
        target = TargetRequest(ip=ip, port=port)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    probe, key = get_loss_probe(), (target.ip, target.port)
    # Адрес из раздела, который панели читать нельзя, в метрики не попадает — не раскрывается и здесь
    visible = probe.sources_of(key) & readable_sources(get_policy())
    return WaysResponse(ip=target.ip, port=target.port, ways=probe.ways(key) if visible else [])


@router.get("/ways/all", response_model=AllWaysResponse)
async def all_ways() -> AllWaysResponse:
    """То же по всем адресам назначения релея одним ответом — для страницы HAProxy панели."""
    return AllWaysResponse(targets=get_loss_probe().all_ways(readable_sources(get_policy())))


@router.post("/trace")
async def start_trace(request: TargetRequest) -> dict:
    """Запустить mtr до ip:port в фоне; ход — GET /trace/{id}. Трасса одна, поэтому
    идёт с первого выходного адреса правила."""
    exits = get_loss_probe().exits_for((request.ip, request.port))
    try:
        trace = get_path_trace_manager().start(request.ip, request.port, local_ip=exits[0])
    except TraceBusyError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    return {"id": trace.id}


@router.get("/trace/{trace_id}")
async def get_trace(trace_id: str) -> dict:
    manager = get_path_trace_manager()
    trace = manager.get(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return manager.view(trace)
