"""Веса серверов HAProxy, которые панель меняет на лету по нагрузке бэкендов.

Команда `set server <backend>/<server> weight N` идёт через admin-сокет: без
reload, открытые соединения остаются на своих серверах. Источник правды —
панель, поэтому на диске ничего не хранится. В памяти — только срок каждого
веса: если панель замолчала, нода сама вернёт вес из конфига (`100%` — вес,
с которым сервер стартовал).
"""

import asyncio
import logging
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

from app.services.haproxy_manager import get_haproxy_manager

logger = logging.getLogger(__name__)

MAX_ITEMS = 4096
EXPIRY_CHECK_SECONDS = 5
CONFIG_WEIGHT = "100%"
PERMISSION_DENIED = "Permission denied"
NO_CHECK_STATUS = "no check"


class SocketPermissionError(Exception):
    """Сокет HAProxy без level admin — менять через него веса нельзя."""


@dataclass(frozen=True)
class WeightItem:
    backend: str
    server: str
    weight: int


@dataclass
class ItemResult:
    backend: str
    server: str
    ok: bool
    error: Optional[str] = None


@dataclass
class ApplyResult:
    results: list[ItemResult] = field(default_factory=list)
    guarded_backends: list[str] = field(default_factory=list)


def _weight_command(backend: str, server: str, weight: str) -> str:
    return f"set server {backend}/{server} weight {weight}"


def _alive(server: dict) -> bool:
    status = server.get("status") or ""
    return status.startswith("UP") or status == NO_CHECK_STATUS


def _restorable(server: dict) -> bool:
    # DRAIN — сервер жив, но вес 0; DOWN вес не спасёт
    return _alive(server) or (server.get("status") or "").startswith("DRAIN")


class RuntimeWeights:
    def __init__(
        self,
        run_command: Callable[[str], str],
        read_proxies: Callable[[], list[dict]],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._run_command = run_command
        self._read_proxies = read_proxies
        self._clock = clock
        self._deadlines: dict[tuple[str, str], float] = {}
        # apply и expire_due идут из тред-пула одновременно
        self._lock = threading.Lock()
        self._task: Optional[asyncio.Task] = None

    def apply(self, items: Sequence[WeightItem], ttl_seconds: int) -> ApplyResult:
        with self._lock:
            result = ApplyResult()
            deadline = self._clock() + ttl_seconds
            for item in items:
                error = self._command(_weight_command(item.backend, item.server, str(item.weight)))
                result.results.append(ItemResult(item.backend, item.server, error is None, error))
                if error is None:
                    self._deadlines[(item.backend, item.server)] = deadline
            result.guarded_backends = self._guard({item.backend for item in items})
            return result

    def expire_due(self) -> list[tuple[str, str]]:
        with self._lock:
            now = self._clock()
            restored = []
            for backend, server in sorted(key for key, at in self._deadlines.items() if at <= now):
                try:
                    self._command(_weight_command(backend, server, CONFIG_WEIGHT))
                except OSError:
                    # Сокет недоступен — вес остался прежним, попробуем на следующем круге
                    continue
                # Ответ с ошибкой — сервера больше нет (правило удалили), держать срок незачем
                del self._deadlines[(backend, server)]
                restored.append((backend, server))
            return restored

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._expiry_loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _expiry_loop(self) -> None:
        while True:
            await asyncio.sleep(EXPIRY_CHECK_SECONDS)
            try:
                restored = await asyncio.to_thread(self.expire_due)
            except SocketPermissionError as e:
                logger.warning("Runtime weights expiry skipped: %s", e)
                continue
            if restored:
                logger.info("Panel stopped refreshing weights, config weights restored: %s", restored)

    def _command(self, command: str) -> Optional[str]:
        """None — успех; иначе текст ошибки HAProxy для этого сервера."""
        reply = self._run_command(command).strip()
        if PERMISSION_DENIED in reply:
            raise SocketPermissionError(reply)
        return reply or None

    def _guard(self, backends: Iterable[str]) -> list[str]:
        """Бэкенд без живого сервера с весом больше 0 отрезал бы всех новых клиентов:
        такого не допускаем, даже если панель видит серверы иначе, чем этот релей."""
        proxies = {proxy.get("name"): proxy for proxy in self._read_proxies()}
        guarded = []
        for backend in sorted(backends):
            servers = [s for s in (proxies.get(backend) or {}).get("servers") or [] if not s.get("backup")]
            if any(_alive(s) and (s.get("weight") or 0) > 0 for s in servers):
                continue
            restorable = [s for s in servers if _restorable(s)]
            if not restorable:
                continue
            for server in restorable:
                self._command(_weight_command(backend, server["name"], CONFIG_WEIGHT))
            guarded.append(backend)
        if guarded:
            logger.info("No live weighted server left, config weights restored in %s", guarded)
        return guarded


_runtime_weights: Optional[RuntimeWeights] = None


def get_runtime_weights() -> RuntimeWeights:
    global _runtime_weights
    if _runtime_weights is None:
        manager = get_haproxy_manager()
        _runtime_weights = RuntimeWeights(manager.runtime_command, manager.read_live_proxies)
    return _runtime_weights
