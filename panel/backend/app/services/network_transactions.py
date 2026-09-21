"""Транзакции доп. IP-адресов ноды: apply → проверка связи → подтверждение.

Нода применяет адреса и взводит таймер отката; доказательство того, что связь
не пропала, — панель снова достучалась до ноды. Проверка идёт по НОВОМУ
TCP-соединению (одноразовый клиент без keepalive): живое соединение из пула
могло пережить смену адресов и «доказать» ложное.

Вся цепочка — фоновая задача: apply на ноде может идти до минуты, а nginx
панели режет /api/ на 60 с. HTTP-ответ на apply ждёт задачу не дольше
INLINE_WAIT_SECONDS и отдаёт снимок; дальше UI поллит состояние.
"""

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.models import Server
from app.services.http_client import (
    get_node_apply_client,
    get_node_client,
    new_node_probe_client,
    node_auth_headers,
)
from app.services.net_utils import host_to_ip
from app.services.network_addresses import AddressSpec
from app.services.reserved_ports_sync import _version_tuple

logger = logging.getLogger(__name__)

MIN_NODE_VERSION_NETWORK = "10.29.0"
ROLLBACK_TIMEOUT_SEC = 120
STATE_TIMEOUT_SECONDS = 10.0
# Не больше proxy_read_timeout у location /api/system/network/ на ноде (тест-инвариант)
APPLY_TIMEOUT_SECONDS = 180.0
CONTROL_TIMEOUT_SECONDS = 20.0
CONFIRM_POLL_INTERVAL_SEC = 3.0
INLINE_WAIT_SECONDS = 20.0
# Часы ноды и панели расходятся; после дедлайна нода откатывает сама
DEADLINE_GRACE_SECONDS = 15.0
REACHABILITY_TIMEOUT_SECONDS = 3.0
REACHABILITY_MAX_ADDRESSES = 16
FINISHED_TTL_SECONDS = 600

STATE_PATH = "/api/system/network/state"
APPLY_PATH = "/api/system/network/apply"
CONFIRM_PATH = "/api/system/network/confirm"
ROLLBACK_PATH = "/api/system/network/rollback"


class TransactionStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class JobPhase(str, Enum):
    APPLYING = "applying"
    CONFIRMING = "confirming"
    DONE = "done"


class NodeNetworkError(Exception):
    """Нода ответила не-200."""

    def __init__(self, status_code: int, detail: Optional[str], body: Any = None):
        super().__init__(detail or f"node responded {status_code}")
        self.status_code = status_code
        self.detail = detail
        self.body = body


class NodeUnreachableError(Exception):
    def __init__(self, reason: str, timeout: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.timeout = timeout


class PendingTransactionError(Exception):
    def __init__(self, transaction: Optional[dict]):
        super().__init__("a transaction is already in progress")
        self.transaction = transaction


class InterfaceNotFoundError(Exception):
    def __init__(self, interface: str):
        super().__init__(interface)
        self.interface = interface


class NothingToApplyError(Exception):
    pass


class ProtectedIpUnknownError(Exception):
    pass


@dataclass
class NetworkJob:
    id: str
    server_id: int
    interface: str
    add: list[AddressSpec]
    remove: list[AddressSpec]
    started_at: float
    phase: JobPhase = JobPhase.APPLYING
    status: TransactionStatus = TransactionStatus.PENDING
    transaction_id: Optional[str] = None
    deadline_at: Optional[datetime] = None
    attempts: int = 0
    last_error: Optional[str] = None
    message: Optional[str] = None
    error_log: Optional[str] = None
    rolled_back: bool = False
    warnings: list[str] = field(default_factory=list)
    reachability: Optional[dict[str, bool]] = None
    finished_at: Optional[float] = None
    task: Optional[asyncio.Task] = None

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "phase": self.phase.value,
            "status": self.status.value,
            "transaction_id": self.transaction_id,
            "interface": self.interface,
            "added": [spec.payload() for spec in self.add],
            "removed": [spec.payload() for spec in self.remove],
            "started_at": _iso(datetime.fromtimestamp(self.started_at, timezone.utc)),
            "deadline_at": _iso(self.deadline_at),
            "attempts": self.attempts,
            "last_error": self.last_error,
            "message": self.message,
            "error_log": self.error_log,
            "rolled_back": self.rolled_back,
            "warnings": list(self.warnings),
            "reachability": dict(self.reachability) if self.reachability is not None else None,
        }


_jobs: dict[int, NetworkJob] = {}
_locks: dict[int, asyncio.Lock] = {}


# ------------------------------------------------------------ чистые хелперы


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def node_supports_network(node_version: Optional[str]) -> bool:
    if not node_version:
        return False
    return _version_tuple(node_version) >= _version_tuple(MIN_NODE_VERSION_NETWORK)


def node_host(server_url: str) -> Optional[str]:
    return urlparse(server_url or "").hostname or None


def node_api_port(server_url: str) -> int:
    parsed = urlparse(server_url or "")
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def parse_deadline(value: Optional[str], fallback_from: datetime) -> datetime:
    """ISO-дата ноды → aware UTC; naive трактуется как UTC; нет значения — считаем сами."""
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    return fallback_from + timedelta(seconds=ROLLBACK_TIMEOUT_SEC)


def deadline_passed(deadline_at: Optional[datetime], now: datetime) -> bool:
    if deadline_at is None:
        return False
    return now > deadline_at + timedelta(seconds=DEADLINE_GRACE_SECONDS)


def missing_on_interface(specs: list[AddressSpec], iface_state: dict) -> list[AddressSpec]:
    present = {(addr.get("address"), addr.get("prefix")) for addr in iface_state.get("addresses") or []}
    return [spec for spec in specs if (spec.address, spec.prefix) not in present]


def managed_on_interface(specs: list[AddressSpec], iface_state: dict) -> list[AddressSpec]:
    managed = {
        (addr.get("address"), addr.get("prefix"))
        for addr in iface_state.get("addresses") or []
        if addr.get("managed")
    }
    return [spec for spec in specs if (spec.address, spec.prefix) in managed]


def _status_from(value: Optional[str]) -> Optional[TransactionStatus]:
    try:
        return TransactionStatus(value or "")
    except ValueError:
        return None


# ---------------------------------------------------------------- вызовы ноды


async def _request(client: httpx.AsyncClient, method: str, url: str, *, headers: dict, timeout: float,
                   json_data: Optional[dict] = None) -> dict:
    try:
        response = await client.request(method, url, headers=headers, json=json_data, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise NodeUnreachableError(f"таймаут: {exc.__class__.__name__}", timeout=True)
    except httpx.RequestError as exc:
        raise NodeUnreachableError(str(exc) or exc.__class__.__name__)
    body: Any = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code == 200 and isinstance(body, dict):
        return body
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, (dict, list)):
        detail = str(detail)
    raise NodeNetworkError(response.status_code, detail, body)


async def fetch_state(server: Server, *, fresh_connection: bool = False) -> dict:
    url = f"{server.url}{STATE_PATH}"
    headers = node_auth_headers(server)
    if fresh_connection:
        async with new_node_probe_client(server) as client:
            return await _request(client, "GET", url, headers=headers, timeout=STATE_TIMEOUT_SECONDS)
    return await _request(get_node_client(server), "GET", url, headers=headers, timeout=STATE_TIMEOUT_SECONDS)


async def send_apply(server: Server, payload: dict) -> dict:
    return await _request(get_node_apply_client(server), "POST", f"{server.url}{APPLY_PATH}",
                          headers=node_auth_headers(server), timeout=APPLY_TIMEOUT_SECONDS, json_data=payload)


async def send_confirm(server: Server, transaction_id: str) -> dict:
    return await _request(get_node_client(server), "POST", f"{server.url}{CONFIRM_PATH}",
                          headers=node_auth_headers(server), timeout=CONTROL_TIMEOUT_SECONDS,
                          json_data={"transaction_id": transaction_id})


async def send_rollback(server: Server, transaction_id: str) -> dict:
    return await _request(get_node_apply_client(server), "POST", f"{server.url}{ROLLBACK_PATH}",
                          headers=node_auth_headers(server), timeout=APPLY_TIMEOUT_SECONDS,
                          json_data={"transaction_id": transaction_id})


async def resolve_protected_ip(server: Server) -> str:
    ip = await host_to_ip(node_host(server.url) or "")
    if not ip:
        raise ProtectedIpUnknownError()
    return ip


# ------------------------------------------------------------------- реестр


def active_job(server_id: int) -> Optional[NetworkJob]:
    job = _jobs.get(server_id)
    if job and job.phase != JobPhase.DONE:
        return job
    return None


def job_snapshot(server_id: int) -> Optional[dict]:
    job = _jobs.get(server_id)
    if job is None:
        return None
    if job.phase == JobPhase.DONE and job.finished_at and time.time() - job.finished_at > FINISHED_TTL_SECONDS:
        del _jobs[server_id]
        return None
    return job.snapshot()


def _finish(job: NetworkJob, status: TransactionStatus, *, message: Optional[str] = None,
            rolled_back: bool = False, error_log: Optional[str] = None) -> None:
    job.phase = JobPhase.DONE
    job.status = status
    job.finished_at = time.time()
    if message:
        job.message = message
    job.rolled_back = job.rolled_back or rolled_back
    if error_log:
        job.error_log = error_log


async def start_apply(server: Server, *, interface: str, add: list[AddressSpec],
                      remove: list[AddressSpec]) -> NetworkJob:
    lock = _locks.setdefault(server.id, asyncio.Lock())
    if lock.locked():
        raise PendingTransactionError(None)
    async with lock:
        running = active_job(server.id)
        if running:
            raise PendingTransactionError(running.snapshot())
        state = await fetch_state(server)
        transaction = state.get("transaction") or None
        if transaction and transaction.get("status") in ("pending", "applying"):
            raise PendingTransactionError(transaction)
        iface_state = next((i for i in state.get("interfaces") or [] if i.get("name") == interface), None)
        if iface_state is None:
            raise InterfaceNotFoundError(interface)
        add = missing_on_interface(add, iface_state)
        remove = managed_on_interface(remove, iface_state)
        if not add and not remove:
            raise NothingToApplyError()
        protected = await resolve_protected_ip(server)

        job = NetworkJob(
            id=f"{server.id}-{int(time.time())}", server_id=server.id, interface=interface,
            add=add, remove=remove, started_at=time.time(),
        )
        _jobs[server.id] = job
        payload = {
            "interface": interface,
            "add": [spec.payload() for spec in add],
            "remove": [spec.payload() for spec in remove],
            "protected": [protected],
            "rollback_timeout_sec": ROLLBACK_TIMEOUT_SEC,
        }
        job.task = asyncio.create_task(_run_job(server, job, payload))
        return job


async def wait_for_job(job: NetworkJob, timeout: float) -> None:
    if job.task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(job.task), timeout)
    except asyncio.TimeoutError:
        pass


async def _run_job(server: Server, job: NetworkJob, payload: dict) -> None:
    try:
        try:
            result = await send_apply(server, payload)
        except NodeUnreachableError as exc:
            # Ответ потерян: apply мог пройти, а связь — оборваться именно из-за
            # смены адресов. Цикл подтверждения найдёт pending-транзакцию сам.
            job.last_error = exc.reason
            job.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=ROLLBACK_TIMEOUT_SEC)
            job.phase = JobPhase.CONFIRMING
        except NodeNetworkError as exc:
            _finish(job, TransactionStatus.FAILED, message=exc.detail or f"нода ответила {exc.status_code}")
            return
        else:
            job.transaction_id = result.get("transaction_id") or None
            job.error_log = result.get("error_log") or None
            job.warnings = [w for w in result.get("warnings") or [] if w]
            if not result.get("success"):
                _finish(job, TransactionStatus.FAILED, message=result.get("message") or "применение не удалось",
                        rolled_back=bool(result.get("rolled_back")))
                return
            job.deadline_at = parse_deadline(result.get("deadline_at"), datetime.now(timezone.utc))
            job.phase = JobPhase.CONFIRMING
        await _confirm_loop(server, job)
        if job.status == TransactionStatus.CONFIRMED:
            job.reachability = await _reachability(server, job)
        logger.info(
            "network_apply_finished server_id=%s status=%s added=%d removed=%d attempts=%d",
            server.id, job.status.value, len(job.add), len(job.remove), job.attempts,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("network apply job crashed for server %s", server.id)
        _finish(job, TransactionStatus.FAILED, message=f"внутренняя ошибка панели: {exc}")
    finally:
        if job.phase != JobPhase.DONE:
            _finish(job, job.status)


async def _confirm_loop(server: Server, job: NetworkJob) -> None:
    while True:
        try:
            state = await fetch_state(server, fresh_connection=True)
        except NodeUnreachableError as exc:
            job.attempts += 1
            job.last_error = exc.reason
            if deadline_passed(job.deadline_at, datetime.now(timezone.utc)):
                _finish(job, TransactionStatus.ROLLED_BACK, rolled_back=True,
                        message="Панель не смогла достучаться до ноды до истечения таймера — нода откатила изменения сама")
                return
            await asyncio.sleep(CONFIRM_POLL_INTERVAL_SEC)
            continue
        except NodeNetworkError as exc:
            _finish(job, TransactionStatus.FAILED, message=exc.detail or f"нода ответила {exc.status_code}")
            return

        transaction = state.get("transaction") or {}
        if job.transaction_id is None and transaction.get("id"):
            job.transaction_id = transaction["id"]
        if not transaction or transaction.get("id") != job.transaction_id:
            history = state.get("history") or []
            found = next((h for h in history if h.get("id") == job.transaction_id), None)
            if found:
                status = _status_from(found.get("status")) or TransactionStatus.FAILED
                _finish(job, status, message=found.get("message"), rolled_back=status == TransactionStatus.ROLLED_BACK)
                return
            if job.transaction_id is None:
                _finish(job, TransactionStatus.FAILED, message="нода не получила запрос — изменения не применялись")
                return
            _finish(job, TransactionStatus.FAILED, message="нода не сообщила о транзакции")
            return

        status = transaction.get("status")
        if status == "applying":
            await asyncio.sleep(CONFIRM_POLL_INTERVAL_SEC)
            continue
        if status == "pending":
            try:
                result = await send_confirm(server, job.transaction_id)
            except NodeUnreachableError as exc:
                job.attempts += 1
                job.last_error = exc.reason
                await asyncio.sleep(CONFIRM_POLL_INTERVAL_SEC)
                continue
            except NodeNetworkError as exc:
                _finish(job, TransactionStatus.FAILED, message=exc.detail or f"нода ответила {exc.status_code}")
                return
            confirmed = _status_from(result.get("status")) or TransactionStatus.CONFIRMED
            _finish(job, confirmed, message=result.get("message") or "подтверждено",
                    rolled_back=confirmed == TransactionStatus.ROLLED_BACK)
            return
        final = _status_from(status) or TransactionStatus.FAILED
        _finish(job, final, message=transaction.get("message"), rolled_back=final == TransactionStatus.ROLLED_BACK)
        return


async def rollback(server: Server, transaction_id: str) -> dict:
    job = _jobs.get(server.id)
    if job and job.task and not job.task.done():
        job.task.cancel()
    result = await send_rollback(server, transaction_id)
    if job and job.phase != JobPhase.DONE:
        _finish(job, TransactionStatus.ROLLED_BACK, rolled_back=True,
                message=result.get("message") or "откачено вручную")
    return result


async def cancel_all_jobs() -> None:
    for job in list(_jobs.values()):
        if job.task and not job.task.done():
            job.task.cancel()
    _jobs.clear()


# ------------------------------------------------------------ достижимость


async def _tcp_probe(address: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(address, port), REACHABILITY_TIMEOUT_SECONDS)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    return True


async def check_reachability(addresses: list[str], port: int) -> dict[str, bool]:
    results = await asyncio.gather(*(_tcp_probe(address, port) for address in addresses))
    return dict(zip(addresses, results))


async def _reachability(server: Server, job: NetworkJob) -> Optional[dict[str, bool]]:
    """Информационно: отвечает ли новый адрес на порт ноды. Через SOCKS-прокси
    смысла нет, приватные адреса из панели недостижимы по определению."""
    if getattr(server, "proxy_url", None) or not job.add or len(job.add) > REACHABILITY_MAX_ADDRESSES:
        return None
    public = [spec.address for spec in job.add if ipaddress.ip_address(spec.address).is_global]
    if not public:
        return None
    return await check_reachability(public, node_api_port(server.url))
