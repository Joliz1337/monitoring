"""Веса серверов HAProxy, которые панель меняет на лету по нагрузке бэкендов.

Команда `set server <backend>/<server> weight N` идёт через admin-сокет: без
reload, открытые соединения остаются на своих серверах. Источник правды —
панель, поэтому на диске ничего не хранится. В памяти — вес и срок каждого:
если панель замолчала, нода сама вернёт вес из конфига (`100%` — вес, с
которым сервер стартовал), а после reload пришлёт действующие веса новому
воркеру — он стартует с весами из конфига.
"""

import asyncio
import logging
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from app.services.haproxy_manager import get_haproxy_manager

logger = logging.getLogger(__name__)

EXPIRY_CHECK_SECONDS = 5
CONFIG_WEIGHT = "100%"
PERMISSION_DENIED = "Permission denied"
NO_CHECK_STATUS = "no check"
GUARDED_ERROR = "Skipped: the backend would be left without a live weighted server"


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


def _can_serve(server: dict) -> bool:
    # Нулевой вес HAProxy показывает как UP. DRAIN — ручной слив, DOWN — упал:
    # новых клиентов такой сервер не возьмёт при любом весе
    status = server.get("status") or ""
    return status.startswith("UP") or status == NO_CHECK_STATUS


def _serving_servers(proxy: dict) -> list[dict]:
    return [s for s in proxy.get("servers") or [] if not s.get("backup") and _can_serve(s)]


def _would_be_empty(proxy: dict, new_weights: Mapping[str, int]) -> bool:
    """Остался бы backend без живого сервера с весом больше 0. Где живых нет
    вовсе, веса не помогут — такой backend предохранитель не трогает."""
    serving = _serving_servers(proxy)
    return bool(serving) and not any(new_weights.get(s["name"], s.get("weight") or 0) > 0 for s in serving)


@dataclass(frozen=True)
class _Lease:
    weight: int
    deadline: float


class RuntimeWeights:
    def __init__(
        self,
        run_command: Callable[[str], str],
        read_proxies: Callable[[], list[dict]],
        read_worker: Callable[[], Optional[str]],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._run_command = run_command
        self._read_proxies = read_proxies
        self._read_worker = read_worker
        self._clock = clock
        self._leases: dict[tuple[str, str], _Lease] = {}
        # Воркер, который получил веса последним: другой — значит, был reload
        self._worker: Optional[str] = None
        # apply и expire_due идут из тред-пула одновременно
        self._lock = threading.Lock()
        self._task: Optional[asyncio.Task] = None

    def apply(self, items: Sequence[WeightItem], ttl_seconds: int) -> ApplyResult:
        with self._lock:
            self._worker = self._read_worker()
            deadline = self._clock() + ttl_seconds
            return self._apply_locked([(item, deadline) for item in items])

    def reapply_after_reload(self) -> int:
        """Новый воркер стартует с весами из конфига, а панель пришлёт свои только
        при следующей отправке — до минуты. Действующие веса шлются ему сразу,
        сроки не продлеваются. Возвращает число применённых весов."""
        with self._lock:
            if not self._leases:
                return 0
            worker = self._read_worker()
            if worker == self._worker:
                return 0
            self._worker = worker
            now = self._clock()
            live = [(WeightItem(backend, server, lease.weight), lease.deadline)
                    for (backend, server), lease in self._leases.items() if lease.deadline > now]
            if not live:
                return 0
            return sum(1 for item in self._apply_locked(live).results if item.ok)

    def _apply_locked(self, entries: Sequence[tuple[WeightItem, float]]) -> ApplyResult:
        """Предохранитель смотрит до команд: иначе последний живой сервер на миг
        получал бы 0, и новые клиенты этого мига уходили бы в отказ."""
        proxies = self._proxies_by_name()
        new_weights: dict[str, dict[str, int]] = {}
        for item, _ in entries:
            new_weights.setdefault(item.backend, {})[item.server] = item.weight
        guarded = sorted(
            backend for backend, weights in new_weights.items()
            if backend in proxies and _would_be_empty(proxies[backend], weights)
        )

        result = ApplyResult(guarded_backends=guarded)
        for item, deadline in entries:
            if item.backend in guarded:
                result.results.append(ItemResult(item.backend, item.server, False, GUARDED_ERROR))
                continue
            error = self._command(_weight_command(item.backend, item.server, str(item.weight)))
            result.results.append(ItemResult(item.backend, item.server, error is None, error))
            if error is None:
                self._leases[(item.backend, item.server)] = _Lease(item.weight, deadline)
        self._restore(guarded, proxies)
        return result

    def check_guard(self) -> list[str]:
        """Сервер мог упасть уже после отправки весов: панель узнает об этом не
        сразу, а до её следующей отправки бэкенд остался бы без серверов."""
        with self._lock:
            weighted = {backend for backend, _ in self._leases}
            if not weighted:
                return []
            proxies = self._proxies_by_name()
            guarded = sorted(b for b in weighted if b in proxies and _would_be_empty(proxies[b], {}))
            self._restore(guarded, proxies)
            return guarded

    def expire_due(self) -> list[tuple[str, str]]:
        with self._lock:
            now = self._clock()
            restored = []
            for backend, server in sorted(key for key, lease in self._leases.items() if lease.deadline <= now):
                try:
                    self._command(_weight_command(backend, server, CONFIG_WEIGHT))
                except OSError:
                    # Сокет недоступен или завис: каждая следующая попытка ждала бы
                    # свой таймаут под блокировкой — остаток круга переносится
                    break
                # Ответ с ошибкой — сервера больше нет (правило удалили), держать срок незачем
                del self._leases[(backend, server)]
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
                resent = await asyncio.to_thread(self.reapply_after_reload)
                restored = await asyncio.to_thread(self.expire_due)
                await asyncio.to_thread(self.check_guard)
            except (OSError, SocketPermissionError) as e:
                logger.warning("Runtime weights round skipped: %s", e)
                continue
            if resent:
                logger.info("HAProxy reloaded, %d panel weights re-sent to the new worker", resent)
            if restored:
                logger.info("Panel stopped refreshing weights, config weights restored: %s", restored)

    def _command(self, command: str) -> Optional[str]:
        """None — успех; иначе текст ошибки HAProxy для этого сервера."""
        reply = self._run_command(command).strip()
        if PERMISSION_DENIED in reply:
            raise SocketPermissionError(reply)
        return reply or None

    def _proxies_by_name(self) -> dict[str, dict]:
        return {proxy.get("name"): proxy for proxy in self._read_proxies()}

    def _restore(self, backends: Sequence[str], proxies: Mapping[str, dict]) -> None:
        """Бэкенд без живого сервера с весом отрезал бы всех новых клиентов: живым
        серверам возвращается вес из конфига, даже если панель видит их иначе, чем релей."""
        for backend in backends:
            for server in _serving_servers(proxies[backend]):
                self._command(_weight_command(backend, server["name"], CONFIG_WEIGHT))
        if backends:
            logger.info("No live weighted server left, config weights restored in %s", list(backends))


_runtime_weights: Optional[RuntimeWeights] = None


def get_runtime_weights() -> RuntimeWeights:
    global _runtime_weights
    if _runtime_weights is None:
        manager = get_haproxy_manager()
        _runtime_weights = RuntimeWeights(manager.runtime_command, manager.read_live_proxies, manager.worker_pid)
    return _runtime_weights
