from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, desc, update
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, Any, AsyncGenerator
from datetime import datetime
import asyncio
import httpx
import json
import logging
import time
from pydantic import BaseModel, Field
from app.services.http_client import get_node_client, node_auth_headers

from app.database import get_db
from app.models import Server, ServerCache, MetricsSnapshot
from app.auth import verify_auth
from app.services import update_channel
from app.services.metrics_history import HistoryPeriod, load_history
from app.services.metrics_rates import enrich_metrics_with_speeds
from app.services.node_capabilities import (
    Capability,
    denial_headers,
    denied_message,
    learn_from_denial,
    server_allows,
    server_allows_path,
)
from app.services.traffic_import import (
    MIN_NODE_VERSION_FOR_TRAFFIC_V2,
    node_supports_traffic_v2,
)
from app.services import network_transactions
from app.services.network_addresses import AddressInputError, expand_entries, normalize_ref, preview

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/proxy", tags=["proxy"])


def to_iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Convert datetime to ISO format with explicit UTC timezone suffix.
    
    All timestamps are stored as naive UTC, so we add 'Z' suffix for frontend.
    Truncates microseconds to milliseconds for better JS compatibility.
    """
    if dt is None:
        return None
    # Truncate to milliseconds (JS ISO format standard)
    dt = dt.replace(microsecond=(dt.microsecond // 1000) * 1000)
    # Format as ISO and append Z (all our times are UTC)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


async def get_server_by_id(server_id: int, db: AsyncSession) -> Server:
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404)
    # Освобождаем коннект пула перед сетевым вызовом к ноде: держать соединение БД
    # на всё время (медленного) проксирования — выгребает пул на зависших нодах.
    # expire_on_commit=False → server остаётся доступным; следующий запрос к db
    # (если есть) возьмёт коннект заново.
    await db.commit()
    return server


def require_capability(server: Server, capability: Capability, *, write: bool) -> None:
    """Отказать до сетевого вызова, если нода этот раздел закрыла.

    409, а не 403: 403 фронт трактует как «сессия протухла», а 502/503/504
    автоматически повторяет. Тем же кодом уже помечено «нода не в том
    состоянии» для устаревшего агента.
    """
    if server_allows(server, capability, write=write):
        return
    raise HTTPException(
        status_code=409,
        detail=denied_message(capability, write),
        headers=denial_headers(capability, write),
    )


def _require_endpoint(server: Server, endpoint: str, method: str) -> None:
    allowed, capability, write = server_allows_path(server, endpoint, method)
    if allowed:
        return
    raise HTTPException(
        status_code=409,
        detail=denied_message(capability, write),
        headers=denial_headers(capability, write),
    )


def _require_traffic_v2(server: Server) -> None:
    """Гейт версии для правил учёта портов.

    Счётчики цепочек iptables читает панель, а отдаёт их только агент 10.13.0+.
    На старом агенте правило добавилось бы в firewall, но в историю не попало бы
    ничего — молча выключенный учёт хуже явного отказа.
    """
    if node_supports_traffic_v2(server.node_version):
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"Node agent {server.node_version or 'unknown'} is too old for port traffic "
            f"accounting (needs >= {MIN_NODE_VERSION_FOR_TRAFFIC_V2}). "
            "Update the node agent first."
        ),
    )


async def get_latest_snapshot(server_id: int, db: AsyncSession) -> Optional[MetricsSnapshot]:
    """Get the most recent metrics snapshot for a server."""
    result = await db.execute(
        select(MetricsSnapshot)
        .where(MetricsSnapshot.server_id == server_id)
        .order_by(desc(MetricsSnapshot.timestamp))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def proxy_request(
    server: Server,
    endpoint: str,
    method: str = "GET",
    json_data: dict = None,
    params: dict = None,
    timeout: float = 15.0
) -> dict:
    _require_endpoint(server, endpoint, method)

    url = f"{server.url}{endpoint}"
    started = time.perf_counter()

    try:
        client = get_node_client(server)
        headers = node_auth_headers(server)

        if method == "GET":
            response = await client.get(url, headers=headers, params=params, timeout=timeout)
        elif method == "POST":
            response = await client.post(url, headers=headers, json=json_data, params=params, timeout=timeout)
        elif method == "PUT":
            response = await client.put(url, headers=headers, json=json_data, timeout=timeout)
        elif method == "DELETE":
            response = await client.delete(url, headers=headers, timeout=timeout)
        else:
            raise HTTPException(status_code=400)

        elapsed_ms = (time.perf_counter() - started) * 1000
        if response.status_code >= 500:
            logger.warning(
                f"node_request status={response.status_code} elapsed_ms={elapsed_ms:.0f} {method} {url}"
            )
        else:
            logger.debug(
                f"node_request status={response.status_code} elapsed_ms={elapsed_ms:.0f} {method} {url}"
            )

        if response.status_code == 200:
            if method != "GET" and "/haproxy/" in endpoint:
                try:
                    from app.services.metrics_collector import get_collector
                    get_collector().notify_activity(server.id)
                except Exception:
                    pass
            return response.json()
        else:
            detail = None
            body = None
            try:
                body = response.json()
                detail = body.get("detail")
            except Exception:
                pass
            if response.status_code == 403:
                # Права поменяли только что, наша копия карты ещё старая:
                # запоминаем сразу, а не ждём следующего цикла метрик
                await learn_from_denial(server.id, response.status_code, body)
                raise HTTPException(
                    status_code=409,
                    detail=detail or "Node closed this section (NODE_CAPABILITIES)",
                )
            raise HTTPException(status_code=response.status_code, detail=detail)
    except httpx.TimeoutException:
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.error(f"node_request timeout elapsed_ms={elapsed_ms:.0f} {method} {url}")
        raise HTTPException(status_code=504)
    except httpx.RequestError as e:
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.error(f"node_request conn_error elapsed_ms={elapsed_ms:.0f} {method} {url}: {e}")
        raise HTTPException(status_code=502)


@router.get("/{server_id}/metrics")
async def get_metrics(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get cached metrics from panel's database (collected by background worker).
    This avoids additional load on nodes when viewing dashboard.
    Enriches metrics with calculated network/disk speeds from latest snapshot.
    """
    server = await get_server_by_id(server_id, db)
    
    if server.last_metrics:
        try:
            metrics = json.loads(server.last_metrics)
            snapshot = await get_latest_snapshot(server_id, db)
            return enrich_metrics_with_speeds(metrics, snapshot)
        except json.JSONDecodeError:
            pass
    
    # No cached data
    raise HTTPException(status_code=503)


@router.get("/{server_id}/metrics/live")
async def get_live_metrics(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get live metrics directly from node (use sparingly, causes load on node).
    Enriches metrics with calculated network/disk speeds from latest snapshot.
    """
    server = await get_server_by_id(server_id, db)
    metrics, snapshot = await asyncio.gather(
        proxy_request(server, "/api/metrics"),
        get_latest_snapshot(server_id, db),
    )
    return enrich_metrics_with_speeds(metrics, snapshot)


@router.get("/{server_id}/metrics/history")
async def get_metrics_history(
    server_id: int,
    period: HistoryPeriod = Query(default="1h"),
    include_per_cpu: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    await get_server_by_id(server_id, db)
    return await load_history(db, server_id, period, include_per_cpu)


@router.get("/{server_id}/haproxy/cached")
async def get_haproxy_cached(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get cached HAProxy data from server_cache table."""
    await get_server_by_id(server_id, db)
    
    result = await db.execute(
        select(ServerCache).where(ServerCache.server_id == server_id)
    )
    cache = result.scalar_one_or_none()
    if cache and cache.last_haproxy_data:
        try:
            return json.loads(cache.last_haproxy_data)
        except json.JSONDecodeError:
            pass
    
    raise HTTPException(status_code=503)


@router.get("/{server_id}/haproxy/status")
async def get_haproxy_status(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/status")


@router.get("/{server_id}/haproxy/stats")
async def get_haproxy_stats(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/stats", timeout=10.0)


@router.get("/{server_id}/haproxy/rules")
async def get_haproxy_rules(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/rules")


@router.post("/{server_id}/haproxy/reload")
async def reload_haproxy(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/reload", method="POST")


@router.post("/{server_id}/haproxy/restart")
async def restart_haproxy(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/restart", method="POST")


@router.post("/{server_id}/haproxy/start")
async def start_haproxy(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/start", method="POST")


@router.post("/{server_id}/haproxy/stop")
async def stop_haproxy(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/stop", method="POST")


@router.get("/{server_id}/haproxy/config")
async def get_haproxy_config(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/config")


@router.get("/{server_id}/haproxy/certs")
async def get_haproxy_certs(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/certs")


@router.get("/{server_id}/haproxy/certs/all")
async def get_all_certs(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/certs/all")


@router.post("/{server_id}/haproxy/certs/generate")
async def generate_cert(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    # Certificate generation can take 2-3 minutes
    return await proxy_request(server, "/api/haproxy/certs/generate", method="POST", json_data=data, timeout=300.0)


@router.post("/{server_id}/haproxy/certs/{domain}/renew")
async def renew_single_cert(
    server_id: int,
    domain: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    # Certificate renewal can take 2-3 minutes
    return await proxy_request(server, f"/api/haproxy/certs/{domain}/renew", method="POST", timeout=300.0)


@router.delete("/{server_id}/haproxy/certs/{domain}")
async def delete_cert(
    server_id: int,
    domain: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, f"/api/haproxy/certs/{domain}", method="DELETE")


@router.post("/{server_id}/haproxy/certs/upload")
async def upload_cert(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/certs/upload", method="POST", json_data=data)


# ==================== Firewall Management ====================

@router.get("/{server_id}/haproxy/firewall/rules")
async def get_firewall_rules(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/firewall/rules")


@router.post("/{server_id}/haproxy/firewall/rule")
async def add_firewall_rule(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/firewall/rule", method="POST", json_data=data)


@router.delete("/{server_id}/haproxy/firewall/rule/{rule_number}")
async def delete_firewall_rule_by_number(
    server_id: int,
    rule_number: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, f"/api/haproxy/firewall/rule/{rule_number}", method="DELETE")


@router.post("/{server_id}/haproxy/firewall/enable")
async def enable_firewall(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/firewall/enable", method="POST")


@router.post("/{server_id}/haproxy/firewall/disable")
async def disable_firewall(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/haproxy/firewall/disable", method="POST")


# ==================== DNAT (проброс портов) ====================

@router.get("/{server_id}/dnat/state")
async def get_dnat_state(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/dnat/state", timeout=20.0)


@router.post("/{server_id}/dnat/reapply")
async def reapply_dnat(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/dnat/reapply", method="POST", timeout=60.0)


@router.post("/{server_id}/dnat/clear")
async def clear_dnat(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    result = await proxy_request(server, "/api/dnat/clear", method="POST", timeout=60.0)
    if result.get("success"):
        # Нода больше ничего не пробрасывает: контрольная сумма профиля на ней
        # недействительна, привязанный профиль снова ждёт раскатки
        await db.execute(
            update(Server).where(Server.id == server_id).values(
                dnat_rules_hash=None,
                dnat_sync_status="pending" if server.active_dnat_profile_id else None,
            )
        )
        await db.commit()
    return result


@router.get("/{server_id}/system/bandwidth-limit")
async def get_bandwidth_limit(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/system/bandwidth-limit", timeout=20.0)


@router.post("/{server_id}/system/bandwidth-limit")
async def set_bandwidth_limit(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/system/bandwidth-limit", method="POST", json_data=data, timeout=40.0)


# ==================== Network: extra IP addresses ====================
# Нода применяет адреса транзакцией с таймером отката; панель подтверждает,
# заново подключившись к ноде (см. services/network_transactions.py).


class NetworkPreviewRequest(BaseModel):
    add_text: str = ""


class NetworkAddressRef(BaseModel):
    address: str = Field(..., max_length=64)
    prefix: int = Field(..., ge=0, le=128)


class NetworkApplyRequest(BaseModel):
    interface: str = Field(..., min_length=1, max_length=32)
    add_text: str = Field("", max_length=20000)
    remove: list[NetworkAddressRef] = Field(default_factory=list)


class NetworkRollbackRequest(BaseModel):
    transaction_id: str = Field(..., min_length=1, max_length=64)


def _network_unsupported(server: Server) -> dict:
    return {
        "supported": False,
        "min_node_version": network_transactions.MIN_NODE_VERSION_NETWORK,
        "node_version": server.node_version,
        "interfaces": [],
        "managed": [],
        "transaction": None,
        "history": [],
        "job": network_transactions.job_snapshot(server.id),
    }


def _require_network_support(server: Server) -> None:
    if network_transactions.node_supports_network(server.node_version):
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"Node agent {server.node_version or 'unknown'} is too old for extra IP management "
            f"(needs >= {network_transactions.MIN_NODE_VERSION_NETWORK}). Update the node agent first."
        ),
    )


async def _raise_for_node_error(server: Server, exc: Exception) -> None:
    if isinstance(exc, network_transactions.NodeUnreachableError):
        raise HTTPException(status_code=504 if exc.timeout else 502, detail=exc.reason)
    if isinstance(exc, network_transactions.NodeNetworkError):
        if exc.status_code == 403:
            await learn_from_denial(server.id, exc.status_code, exc.body)
            raise HTTPException(status_code=409, detail=exc.detail or "Node closed this section (NODE_CAPABILITIES)")
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    raise exc


@router.get("/{server_id}/network/state")
async def get_network_state(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    require_capability(server, Capability.SYSTEM, write=False)
    if not network_transactions.node_supports_network(server.node_version):
        return _network_unsupported(server)
    try:
        state = await network_transactions.fetch_state(server)
    except network_transactions.NodeNetworkError as exc:
        if exc.status_code == 404:
            return _network_unsupported(server)
        await _raise_for_node_error(server, exc)
    except network_transactions.NodeUnreachableError as exc:
        await _raise_for_node_error(server, exc)
    state.setdefault("supported", True)
    state["min_node_version"] = network_transactions.MIN_NODE_VERSION_NETWORK
    state["node_version"] = server.node_version
    state["job"] = network_transactions.job_snapshot(server.id)
    return state


@router.post("/{server_id}/network/preview")
async def preview_network_addresses(
    server_id: int,
    data: NetworkPreviewRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    await get_server_by_id(server_id, db)
    try:
        return preview(data.add_text)
    except AddressInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{server_id}/network/apply")
async def apply_network_addresses(
    server_id: int,
    data: NetworkApplyRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    require_capability(server, Capability.SYSTEM, write=True)
    _require_network_support(server)
    try:
        add = expand_entries(data.add_text) if data.add_text.strip() else []
        remove = [normalize_ref(ref.address, ref.prefix) for ref in data.remove]
    except AddressInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not add and not remove:
        raise HTTPException(status_code=400, detail="Нечего применять: укажите адреса для добавления или удаления")
    try:
        job = await network_transactions.start_apply(server, interface=data.interface, add=add, remove=remove)
    except network_transactions.PendingTransactionError:
        raise HTTPException(status_code=409, detail="На ноде уже идёт транзакция — дождитесь её завершения")
    except network_transactions.InterfaceNotFoundError as exc:
        raise HTTPException(status_code=400, detail=f"Интерфейс {exc.interface} не найден на ноде")
    except network_transactions.NothingToApplyError:
        raise HTTPException(status_code=400, detail="Нечего применять: адреса уже настроены или не управляются панелью")
    except network_transactions.ProtectedIpUnknownError:
        raise HTTPException(status_code=400, detail="Не удалось определить адрес ноды из её URL — защитить его от удаления нельзя")
    except (network_transactions.NodeNetworkError, network_transactions.NodeUnreachableError) as exc:
        await _raise_for_node_error(server, exc)
    await network_transactions.wait_for_job(job, network_transactions.INLINE_WAIT_SECONDS)
    return job.snapshot()


@router.post("/{server_id}/network/rollback")
async def rollback_network_transaction(
    server_id: int,
    data: NetworkRollbackRequest,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    require_capability(server, Capability.SYSTEM, write=True)
    _require_network_support(server)
    try:
        return await network_transactions.rollback(server, data.transaction_id)
    except (network_transactions.NodeNetworkError, network_transactions.NodeUnreachableError) as exc:
        await _raise_for_node_error(server, exc)


# ==================== Traffic Tracking ====================
# История трафика живёт в PostgreSQL панели и отдаётся роутером /api/traffic.
# Здесь остаётся только управление правилами учёта портов в iptables ноды.

@router.post("/{server_id}/traffic/ports/add")
async def add_tracked_port(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    _require_traffic_v2(server)
    return await proxy_request(server, "/api/traffic/ports/add", method="POST", json_data=data)


@router.post("/{server_id}/traffic/ports/remove")
async def remove_tracked_port(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    # Удаление намеренно без гейта версии: правило могли создать до обновления агента,
    # и запрет снять его оставил бы оператора с мусором в iptables без способа убрать.
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/traffic/ports/remove", method="POST", json_data=data)


# ==================== System / Updates ====================

@router.post("/{server_id}/system/update")
async def trigger_node_update(
    server_id: int,
    data: dict = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """
    Trigger node update.
    Optional data: { "target_version": "v1.1.0" }
    If not specified, updates to the selected update channel (main/dev).
    """
    server = await get_server_by_id(server_id, db)
    data = data or {}
    if not data.get("target_version"):
        data["target_version"] = update_channel.current_branch()
    return await proxy_request(server, "/api/system/update", method="POST", json_data=data)


@router.post("/{server_id}/system/execute")
async def execute_command_on_node(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """
    Execute a shell command on the node's host system.
    
    Uses nsenter to run commands in the host namespace from Docker container.
    
    Request body:
        command: str - Shell command to execute (required)
        timeout: int - Timeout in seconds, 1-600 (default: 30)
        shell: str - Shell to use: "sh" or "bash" (default: "sh")
    
    Response:
        success: bool - Whether command exited with code 0
        exit_code: int - Command exit code
        stdout: str - Standard output
        stderr: str - Standard error
        execution_time_ms: int - Execution time in milliseconds
        error: str | null - Error message if execution failed
    
    Examples:
        {"command": "sysctl -p /etc/sysctl.d/99-network-tuning.conf"}
        {"command": "systemctl restart nginx", "timeout": 60}
        {"command": "cat /etc/os-release && uname -a", "shell": "bash"}
    """
    server = await get_server_by_id(server_id, db)
    # Use longer timeout for potentially long-running commands
    request_timeout = min((data.get("timeout", 30) or 30) + 10, 620)
    return await proxy_request(
        server,
        "/api/system/execute",
        method="POST",
        json_data=data,
        timeout=float(request_timeout)
    )


@router.post("/{server_id}/system/execute-stream")
async def execute_command_stream_on_node(
    server_id: int,
    data: dict,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """
    Execute a shell command on the node's host system with streaming output (SSE).
    
    Returns Server-Sent Events with real-time stdout/stderr output.
    Proxies SSE stream from the node to the client.
    
    SSE Event types:
        - stdout: {"line": "output line"}
        - stderr: {"line": "error line"}
        - done: {"exit_code": 0, "execution_time_ms": 1234, "success": true}
        - error: {"message": "error description"}
    
    Request body:
        command: str - Shell command to execute (required)
        timeout: int - Timeout in seconds, 1-600 (default: 30)
        shell: str - Shell to use: "sh" or "bash" (default: "sh")
    """
    server = await get_server_by_id(server_id, db)
    require_capability(server, Capability.EXEC, write=True)
    url = f"{server.url}/api/system/execute-stream"
    request_timeout = min((data.get("timeout", 30) or 30) + 15, 620)
    
    async def stream_proxy() -> AsyncGenerator[bytes, None]:
        try:
            client = get_node_client(server)
            async with client.stream(
                "POST",
                url,
                headers=node_auth_headers(server),
                json=data,
                timeout=request_timeout,
            ) as response:
                if response.status_code != 200:
                    error_event = f'event: error\ndata: {{"message": "Node returned status {response.status_code}"}}\n\n'
                    yield error_event.encode()
                    return

                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.TimeoutException:
            error_event = 'event: error\ndata: {"message": "Connection to node timed out"}\n\n'
            yield error_event.encode()
        except httpx.RequestError as e:
            error_event = f'event: error\ndata: {{"message": "Connection error: {str(e)}"}}\n\n'
            yield error_event.encode()
        except Exception as e:
            logger.error(f"SSE proxy error: {e}")
            error_event = f'event: error\ndata: {{"message": "Proxy error: {str(e)}"}}\n\n'
            yield error_event.encode()
    
    return StreamingResponse(
        stream_proxy(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


# ==================== System Optimizations ====================

@router.get("/{server_id}/system/nic-info")
async def get_node_nic_info(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Get NIC tuning mode and hardware multiqueue capabilities (per-server cache 20s)."""
    from app.routers.system import get_cached_nic_info

    server = await get_server_by_id(server_id, db)

    async def _fetch() -> Any:
        return await proxy_request(server, "/api/system/nic-info", timeout=15.0)

    return await get_cached_nic_info(server_id, _fetch)


@router.post("/{server_id}/system/optimizations/apply")
async def apply_node_optimizations(
    server_id: int,
    body: dict = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """
    Apply system optimizations to a node.

    Fetches latest configs from GitHub and applies them.
    Accepts optional body with nic_mode: "rps" (default), "multiqueue", or "hybrid".
    """
    server = await get_server_by_id(server_id, db)
    # До опроса версии и до нескольких запросов на GitHub: иначе за отказ,
    # известный заранее, платили бы полуминутой ожидания
    require_capability(server, Capability.SYSTEM, write=True)

    from app.routers.settings import cpu_affinity_enabled
    from app.routers.system import (
        MIN_NODE_VERSION_FOR_RENDER,
        get_node_all_versions,
        get_optimizations_from_github,
        invalidate_node_cache,
        node_supports_renderer,
    )

    nic_mode = (body or {}).get("nic_mode", "rps")
    opt_profile = (body or {}).get("opt_profile", "vpn")

    # Version gate, not a compatibility shim. The node now runs the renderer
    # itself; an older agent would write the base files verbatim and every line
    # with an @@TOKEN@@ in it would be rejected by sysctl. There is no safe way
    # to serve both contracts from one payload.
    versions = await get_node_all_versions(server)
    node_version = (versions or {}).get("node_version")
    if not node_supports_renderer(node_version):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Node agent {node_version or 'unknown'} is too old for the current "
                f"tuning layer (needs >= {MIN_NODE_VERSION_FOR_RENDER}). "
                "Update the node agent first."
            ),
        )

    github_data = await get_optimizations_from_github(profile=opt_profile)

    required = (
        "renderer_content",
        "common_base_content",
        "profile_base_content",
        "limits_tmpl_content",
        "systemd_limits_tmpl_content",
    )
    missing = [k for k in required if not github_data.get(k)]
    if missing:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch tuning inputs from GitHub: {', '.join(missing)}",
        )

    if nic_mode == "hybrid" and not github_data.get("hybrid_tune_content"):
        raise HTTPException(
            status_code=502,
            detail="Hybrid NIC scripts not found on GitHub. Update configs or pick another mode.",
        )

    apply_data = {
        "renderer_content": github_data["renderer_content"],
        "common_base_content": github_data["common_base_content"],
        "profile_base_content": github_data["profile_base_content"],
        "limits_tmpl_content": github_data["limits_tmpl_content"],
        "systemd_limits_tmpl_content": github_data["systemd_limits_tmpl_content"],
        "network_tune_content": github_data.get("network_tune_content"),
        "network_tune_service_content": github_data.get("network_tune_service_content"),
        "multiqueue_tune_content": github_data.get("multiqueue_tune_content"),
        "multiqueue_tune_service_content": github_data.get("multiqueue_tune_service_content"),
        "hybrid_tune_content": github_data.get("hybrid_tune_content"),
        "hybrid_tune_service_content": github_data.get("hybrid_tune_service_content"),
        "nic_mode": nic_mode,
        "opt_profile": opt_profile,
        "version": github_data.get("version"),
        "cpu_affinity": await cpu_affinity_enabled(db),
    }

    result = await proxy_request(
        server,
        "/api/system/optimizations/apply",
        method="POST",
        json_data=apply_data,
        timeout=60.0
    )
    invalidate_node_cache(server_id)
    return result


@router.post("/{server_id}/system/optimizations/remove")
async def remove_node_optimizations(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Remove all system optimizations from a node"""
    from app.routers.system import invalidate_node_cache

    server = await get_server_by_id(server_id, db)
    result = await proxy_request(
        server,
        "/api/system/optimizations/remove",
        method="POST",
        timeout=60.0
    )
    invalidate_node_cache(server_id)
    return result


# ==================== Anti-DDoS (per-node) ====================

@router.post("/{server_id}/antiddos/emergency")
async def set_node_antiddos_emergency(
    server_id: int,
    body: dict = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Manually toggle emergency mode on a node (manual pin — watchdog won't undo it)."""
    from app.services.antiddos_manager import get_antiddos_manager
    server = await get_server_by_id(server_id, db)
    require_capability(server, Capability.ANTIDDOS, write=True)
    enabled = bool((body or {}).get("enabled", False))
    manager = get_antiddos_manager()
    ok, msg, status = await manager.set_node_emergency(server, enabled)
    if ok and status:
        await manager._store_status(server_id, status)
    return {"success": ok, "message": msg, "status": status}


@router.post("/{server_id}/antiddos/watchdog")
async def set_node_antiddos_watchdog(
    server_id: int,
    body: dict = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Enable/disable the auto-detection watchdog on a node."""
    from app.services.antiddos_manager import get_antiddos_manager
    server = await get_server_by_id(server_id, db)
    require_capability(server, Capability.ANTIDDOS, write=True)
    enabled = bool((body or {}).get("enabled", True))
    manager = get_antiddos_manager()
    ok, msg = await manager.set_node_watchdog(server, enabled)
    status = await manager.get_node_status(server)
    if status:
        await manager._store_status(server_id, status)
    return {"success": ok, "message": msg, "status": status}


@router.post("/{server_id}/antiddos/install")
async def install_node_antiddos(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Install/refresh the watchdog service on a node (fetches script from GitHub)."""
    from app.services.antiddos_manager import get_antiddos_manager
    server = await get_server_by_id(server_id, db)
    require_capability(server, Capability.ANTIDDOS, write=True)
    manager = get_antiddos_manager()
    ok, msg = await manager.install_to_node(server)
    status = await manager.get_node_status(server) if ok else None
    if status:
        await manager._store_status(server_id, status)
    return {"success": ok, "message": msg, "status": status}


# ==================== Remnawave nginx ====================

async def _remnawave_nginx_path(db: AsyncSession) -> str:
    from app.services.remnawave_nginx_sync import get_remnawave_nginx_path
    return await get_remnawave_nginx_path(db)


@router.get("/{server_id}/remnawave-nginx/status")
async def get_remnawave_nginx_status(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    """Лёгкий статус контейнера remnawave-nginx + хэш конфига."""
    path = await _remnawave_nginx_path(db)
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/remnawave/nginx/status", params={"path": path})


@router.post("/{server_id}/remnawave-nginx/restart")
async def restart_remnawave_nginx(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    server = await get_server_by_id(server_id, db)
    return await proxy_request(server, "/api/remnawave/nginx/restart", method="POST", timeout=120.0)


# ==================== IPSet Management ====================
