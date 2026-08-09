from enum import Enum
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from pydantic import BaseModel, Field, ValidationError
import httpx
from app.services.http_client import get_node_client, node_auth_headers
import logging

from app.database import get_db
from app.models import Server
from app.services.traffic_import import (
    MIN_NODE_VERSION_FOR_TRAFFIC_V2,
    node_supports_traffic_v2,
)
from app.auth import verify_auth
from app.services.bulk_job_manager import BulkExecutor, get_bulk_job_manager, run_bulk
from app.services.server_status import get_offline_threshold, resolve_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bulk", tags=["bulk"])


class TrafficPortParams(BaseModel):
    port: int


class FirewallRuleParams(BaseModel):
    port: int
    protocol: str = "any"
    action: str = "allow"
    from_ip: Optional[str] = None
    direction: str = "in"


class FirewallRuleDeleteParams(BaseModel):
    port: int


class TerminalExecuteParams(BaseModel):
    command: str
    timeout: int = 30
    shell: str = "sh"


class BulkResult(BaseModel):
    server_id: int
    server_name: str
    success: bool
    message: str


class BulkTerminalResult(BulkResult):
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    execution_time_ms: int = 0


async def get_servers_by_ids(server_ids: list[int], db: AsyncSession) -> list[Server]:
    """Активные серверы по списку id — деактивированные в bulk-операциях не участвуют."""
    result = await db.execute(
        select(Server).where(Server.id.in_(server_ids), Server.is_active.is_(True))
    )
    return list(result.scalars().all())


def skip_offline(executor: BulkExecutor, offline_threshold: int) -> BulkExecutor:
    """Офлайн-ноды получают мгновенный fail-результат вместо ожидания сетевого таймаута."""

    async def run(server: Server) -> BaseModel:
        if resolve_status(server, offline_threshold) == "offline":
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=False,
                message="Node is offline — skipped",
            )
        return await executor(server)

    return run


async def run_bulk_for_ids(server_ids: list[int], executor: BulkExecutor, db: AsyncSession) -> list[BaseModel]:
    """Общий путь синхронных bulk-эндпоинтов: активные серверы, офлайн — пропуск без таймаута."""
    servers = await get_servers_by_ids(server_ids, db)

    if not servers:
        raise HTTPException(status_code=404)

    offline_threshold = await get_offline_threshold(db)
    return await run_bulk(servers, skip_offline(executor, offline_threshold))


async def proxy_request_safe(
    server: Server,
    endpoint: str,
    method: str = "GET",
    json_data: dict = None,
    params: dict = None,
    timeout: float = 30.0
) -> tuple[bool, dict | str]:
    """Make a proxy request and return (success, result/error)."""
    url = f"{server.url}{endpoint}"

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
            response = await client.delete(url, headers=headers, params=params, timeout=timeout)
        else:
            return False, "Invalid method"

        if response.status_code == 200:
            return True, response.json()
        else:
            error_detail = response.json().get("detail", f"Error {response.status_code}")
            return False, error_detail
    except httpx.TimeoutException:
        return False, "Connection timeout"
    except httpx.RequestError as e:
        return False, f"Connection error: {str(e)}"
    except Exception as e:
        return False, str(e)


# ==================== Executors ====================
# Действие над одним сервером. Одни и те же executor-функции используются
# синхронными эндпоинтами (через run_bulk) и фоновыми задачами (/bulk/jobs).

_HAPROXY_SERVICE_MESSAGES = {
    "start": "HAProxy started",
    "stop": "HAProxy stopped",
    "restart": "HAProxy restarted",
}


def haproxy_service_executor(op: str) -> BulkExecutor:
    default_msg = _HAPROXY_SERVICE_MESSAGES[op]

    async def run(server: Server) -> BulkResult:
        success, result = await proxy_request_safe(
            server, f"/api/haproxy/{op}", method="POST"
        )

        if success:
            msg = result.get("message", default_msg) if isinstance(result, dict) else default_msg
        else:
            msg = str(result)

        return BulkResult(
            server_id=server.id,
            server_name=server.name,
            success=success,
            message=msg
        )

    return run


def traffic_port_add_executor(port: int) -> BulkExecutor:
    async def run(server: Server) -> BulkResult:
        # Тот же гейт, что и на одиночном эндпоинте: на старом агенте правило в iptables
        # появилось бы, а счётчики панель читать не умеет — учёт молча не работал бы.
        if not node_supports_traffic_v2(server.node_version):
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=False,
                message=f"Agent {server.node_version or 'unknown'} is older than {MIN_NODE_VERSION_FOR_TRAFFIC_V2}",
            )
        success, result = await proxy_request_safe(
            server, "/api/traffic/ports/add", method="POST", json_data={"port": port}
        )
        return BulkResult(
            server_id=server.id,
            server_name=server.name,
            success=success,
            message=f"Port {port} added" if success else str(result)
        )

    return run


def traffic_port_remove_executor(port: int) -> BulkExecutor:
    async def run(server: Server) -> BulkResult:
        # First check if port is tracked
        success, tracked_result = await proxy_request_safe(server, "/api/traffic/ports/tracked")

        if not success:
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=False,
                message=f"Failed to get tracked ports: {tracked_result}"
            )

        tracked_ports = tracked_result.get("tracked_ports", [])
        if port not in tracked_ports:
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=False,
                message=f"Port {port} is not tracked"
            )

        success, result = await proxy_request_safe(
            server, "/api/traffic/ports/remove", method="POST", json_data={"port": port}
        )

        return BulkResult(
            server_id=server.id,
            server_name=server.name,
            success=success,
            message=f"Port {port} removed" if success else str(result)
        )

    return run


def firewall_rule_add_executor(params: FirewallRuleParams) -> BulkExecutor:
    rule_data = {
        "port": params.port,
        "protocol": params.protocol,
        "action": params.action,
        "from_ip": params.from_ip,
        "direction": params.direction,
    }

    async def run(server: Server) -> BulkResult:
        success, result = await proxy_request_safe(
            server, "/api/haproxy/firewall/rule", method="POST", json_data=rule_data
        )

        if success and isinstance(result, dict) and result.get("success") is False:
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=False,
                message=result.get("message", "Failed to add rule")
            )

        return BulkResult(
            server_id=server.id,
            server_name=server.name,
            success=success,
            message=f"Firewall rule added (port {params.port})" if success else str(result)
        )

    return run


def firewall_rule_delete_executor(port: int) -> BulkExecutor:
    async def run(server: Server) -> BulkResult:
        # First get firewall rules to check if port exists
        success, rules_result = await proxy_request_safe(server, "/api/haproxy/firewall/rules")

        if not success:
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=False,
                message=f"Failed to get firewall rules: {rules_result}"
            )

        rules = rules_result.get("rules", [])
        matching_rules = [r for r in rules if r.get("port") == port and not r.get("ipv6", False)]

        if not matching_rules:
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=False,
                message=f"No firewall rule found for port {port}"
            )

        # Delete all matching rules (there may be multiple for tcp/udp)
        deleted_count = 0
        errors = []

        for rule in matching_rules:
            rule_number = rule.get("number")
            if rule_number:
                success, result = await proxy_request_safe(
                    server, f"/api/haproxy/firewall/rule/{rule_number}", method="DELETE"
                )
                if success:
                    deleted_count += 1
                else:
                    errors.append(str(result))

        if deleted_count > 0:
            return BulkResult(
                server_id=server.id,
                server_name=server.name,
                success=True,
                message=f"Deleted {deleted_count} rule(s) for port {port}"
            )
        return BulkResult(
            server_id=server.id,
            server_name=server.name,
            success=False,
            message=f"Failed to delete rules: {'; '.join(errors)}"
        )

    return run


def terminal_execute_executor(params: TerminalExecuteParams) -> BulkExecutor:
    exec_data = {
        "command": params.command,
        "timeout": min(max(params.timeout, 1), 600),
        "shell": params.shell if params.shell in ("sh", "bash") else "sh",
    }
    request_timeout = float(exec_data["timeout"] + 15)

    async def run(server: Server) -> BulkTerminalResult:
        success, result = await proxy_request_safe(
            server, "/api/system/execute", method="POST",
            json_data=exec_data, timeout=request_timeout
        )

        if success and isinstance(result, dict):
            return BulkTerminalResult(
                server_id=server.id,
                server_name=server.name,
                success=result.get("success", False),
                message=f"exit {result.get('exit_code', -1)}",
                stdout=result.get("stdout", ""),
                stderr=result.get("stderr", ""),
                exit_code=result.get("exit_code", -1),
                execution_time_ms=result.get("execution_time_ms", 0),
            )

        return BulkTerminalResult(
            server_id=server.id,
            server_name=server.name,
            success=False,
            message=str(result),
        )

    return run


# ==================== Background Jobs ====================
# Операция выполняется в фоне на бэкенде: обрыв связи с фронтом её не прерывает.
# Фронт опрашивает GET /bulk/jobs/{id} и показывает прогресс.

class BulkJobAction(str, Enum):
    HAPROXY_START = "haproxy_start"
    HAPROXY_STOP = "haproxy_stop"
    HAPROXY_RESTART = "haproxy_restart"
    TRAFFIC_PORT_ADD = "traffic_port_add"
    TRAFFIC_PORT_REMOVE = "traffic_port_remove"
    FIREWALL_RULE_ADD = "firewall_rule_add"
    FIREWALL_RULE_DELETE = "firewall_rule_delete"
    TERMINAL_EXECUTE = "terminal_execute"


class BulkJobCreate(BaseModel):
    action: BulkJobAction
    server_ids: list[int]
    params: dict = Field(default_factory=dict)


def build_job_executor(action: BulkJobAction, params: dict) -> BulkExecutor:
    try:
        if action is BulkJobAction.HAPROXY_START:
            return haproxy_service_executor("start")
        if action is BulkJobAction.HAPROXY_STOP:
            return haproxy_service_executor("stop")
        if action is BulkJobAction.HAPROXY_RESTART:
            return haproxy_service_executor("restart")
        if action is BulkJobAction.TRAFFIC_PORT_ADD:
            return traffic_port_add_executor(TrafficPortParams(**params).port)
        if action is BulkJobAction.TRAFFIC_PORT_REMOVE:
            return traffic_port_remove_executor(TrafficPortParams(**params).port)
        if action is BulkJobAction.FIREWALL_RULE_ADD:
            return firewall_rule_add_executor(FirewallRuleParams(**params))
        if action is BulkJobAction.FIREWALL_RULE_DELETE:
            return firewall_rule_delete_executor(FirewallRuleDeleteParams(**params).port)
        return terminal_execute_executor(TerminalExecuteParams(**params))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/jobs", status_code=202)
async def create_bulk_job(
    data: BulkJobCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    executor = build_job_executor(data.action, data.params)
    servers = await get_servers_by_ids(data.server_ids, db)

    if not servers:
        raise HTTPException(status_code=404)

    offline_threshold = await get_offline_threshold(db)
    job_id = get_bulk_job_manager().start(
        data.action.value, servers, skip_offline(executor, offline_threshold)
    )
    return {"job_id": job_id}


@router.get("/jobs")
async def list_bulk_jobs(_: dict = Depends(verify_auth)):
    return {"jobs": get_bulk_job_manager().list_jobs()}


@router.get("/jobs/{job_id}")
async def get_bulk_job(job_id: str, _: dict = Depends(verify_auth)):
    manager = get_bulk_job_manager()
    job = manager.get(job_id)

    if job is None:
        raise HTTPException(status_code=404)

    return manager.job_state(job)


# ==================== HAProxy Service ====================

# ==================== HAProxy Rules ====================

# ==================== Traffic Ports ====================

# ==================== Firewall Rules ====================

# ==================== Terminal ====================

# ==================== HAProxy Config ====================

# ==================== HAProxy Config Find & Replace ====================
