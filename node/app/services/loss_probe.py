"""Потери до адресов назначения: релей сам проверяет, доходят ли его подключения.

Зачем. Статус сервера в HAProxy — одна проверка раз в несколько секунд с ответом
«жив / не жив»: адрес, до которого фильтрация на пути съедает 40% подключений,
остаётся UP. Такая деградация видна только статистикой многих попыток.

Как. Каждые ATTEMPT_INTERVAL_SEC на каждый адрес — одна попытка connect() с
таймаутом короче первого повтора SYN в ядре (1 с): уходит ровно один SYN, и
неответ означает потерянный пакет, а не медленную сеть, замаскированную
повтором. Отказ (RST) — тоже ответ: сеть до адреса жива, закрыт только порт.
Окно — последние WINDOW_ATTEMPTS попыток.

Адреса нода берёт сама: серверы бэкендов из `show stat` HAProxy и цели
TCP-правил DNAT. Ничего из этого нет — цикл простаивает.
"""

import asyncio
import ipaddress
import logging
import time
from collections import deque
from enum import Enum
from statistics import fmean
from typing import Awaitable, Callable, Iterable, Optional

from app.capabilities import CapabilityPolicy
from app.models.dnat import DnatRule, DnatStateResponse
from app.models.haproxy import HAProxyStatsResponse
from app.models.loss_probe import ProbeStats

logger = logging.getLogger(__name__)

ATTEMPT_INTERVAL_SEC = 2.0
ATTEMPT_TIMEOUT_SEC = 0.8
WINDOW_ATTEMPTS = 60
TARGETS_REFRESH_SEC = 10.0
# Потолок на случай конфига с сотнями бэкендов: нагрузка проверки остаётся
# ограниченной при любом размере парка
MAX_TARGETS = 256
MAX_CONCURRENT_ATTEMPTS = 64

Target = tuple[str, int]


class Source(str, Enum):
    HAPROXY = "haproxy"
    DNAT = "dnat"


# Адрес уходит в метрики, только если панели можно читать раздел, откуда он
# взят: иначе метрики раскрывали бы бэкенды в обход NODE_CAPABILITIES
SOURCE_READ_PATHS = {
    Source.HAPROXY: "/api/haproxy/stats",
    Source.DNAT: "/api/dnat/state",
}


def parse_addr(addr: str) -> Optional[Target]:
    """`10.0.0.5:443` или `[2001:db8::1]:443` из колонки addr `show stat`."""
    host, separator, port_text = (addr or "").rpartition(":")
    if not separator or not port_text.isdigit():
        return None
    port = int(port_text)
    if not 1 <= port <= 65535:
        return None
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None
    if ip.is_unspecified:
        return None
    return str(ip), port


def haproxy_targets(stats: HAProxyStatsResponse) -> set[Target]:
    return {
        target
        for proxy in stats.proxies
        for server in proxy.servers
        if (target := parse_addr(server.addr or "")) is not None
    }


def dnat_rule_port(rule: DnatRule) -> Optional[int]:
    """Порт цели TCP-правила; None — правило без TCP, проверять нечем."""
    if not rule.enabled or "tcp" not in rule.protocols():
        return None
    return rule.target_port or rule.listen_port


def dnat_targets(rules: Iterable[DnatRule]) -> set[Target]:
    return {
        (ip, port)
        for rule in rules
        if (port := dnat_rule_port(rule)) is not None
        for ip in rule.targets()
    }


def readable_sources(policy: CapabilityPolicy) -> set[Source]:
    return {source for source, path in SOURCE_READ_PATHS.items() if policy.check("GET", path) is None}


async def attempt(ip: str, port: int, timeout: float = ATTEMPT_TIMEOUT_SEC) -> Optional[float]:
    """Задержка ответа в мс или None, если ответа не было."""
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except ConnectionRefusedError:
        return (time.perf_counter() - started) * 1000
    except (asyncio.TimeoutError, OSError):
        return None
    elapsed_ms = (time.perf_counter() - started) * 1000
    writer.transport.abort()
    return elapsed_ms


class AttemptWindow:
    def __init__(self, size: int = WINDOW_ATTEMPTS):
        self._results: deque[Optional[float]] = deque(maxlen=size)

    def record(self, rtt_ms: Optional[float]) -> None:
        self._results.append(rtt_ms)

    def stats(self) -> Optional[ProbeStats]:
        if not self._results:
            return None
        answered = [rtt for rtt in self._results if rtt is not None]
        lost = len(self._results) - len(answered)
        return ProbeStats(
            loss_pct=round(lost * 100 / len(self._results), 1),
            rtt_ms=round(fmean(answered), 1) if answered else None,
            samples=len(self._results),
        )


class LossProbe:
    def __init__(
        self,
        discover: Callable[[], dict[Target, set[Source]]],
        attempt_fn: Callable[[str, int], Awaitable[Optional[float]]] = attempt,
    ):
        self._discover = discover
        self._attempt = attempt_fn
        self._windows: dict[Target, AttemptWindow] = {}
        self._sources: dict[Target, set[Source]] = {}
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_ATTEMPTS)
        self._task: Optional[asyncio.Task] = None

    def refresh_targets(self, targets: dict[Target, set[Source]]) -> None:
        """История адреса переживает обновление списка; пропавший адрес забывается."""
        if len(targets) > MAX_TARGETS:
            logger.warning("Loss probe: %s targets, probing the first %s", len(targets), MAX_TARGETS)
        wanted = dict(sorted(targets.items())[:MAX_TARGETS])
        self._windows = {target: self._windows.get(target) or AttemptWindow() for target in wanted}
        self._sources = wanted

    async def run_round(self) -> None:
        targets = list(self._windows)
        results = await asyncio.gather(*(self._limited_attempt(ip, port) for ip, port in targets))
        for target, rtt_ms in zip(targets, results):
            window = self._windows.get(target)
            if window is not None:
                window.record(rtt_ms)

    async def _limited_attempt(self, ip: str, port: int) -> Optional[float]:
        async with self._semaphore:
            return await self._attempt(ip, port)

    def stats_for(self, target: Optional[Target]) -> Optional[ProbeStats]:
        window = self._windows.get(target) if target else None
        return window.stats() if window else None

    def annotate_haproxy(self, stats: HAProxyStatsResponse) -> HAProxyStatsResponse:
        annotated = stats.model_copy(deep=True)
        for proxy in annotated.proxies:
            for server in proxy.servers:
                server.probe = self.stats_for(parse_addr(server.addr or ""))
        return annotated

    def annotate_dnat(self, state: DnatStateResponse) -> DnatStateResponse:
        ports = {rule.name: dnat_rule_port(rule) for rule in state.rules}
        for counters in state.counters:
            port = ports.get(counters.name)
            for target in counters.targets:
                target.probe = self.stats_for((target.ip, port)) if port else None
        return state

    def snapshot(self, readable: set[Source]) -> list[dict]:
        """Все адреса с результатами — для метрик и алертов панели."""
        entries = []
        for (ip, port), window in self._windows.items():
            stats = window.stats()
            if stats is None or not self._sources.get((ip, port), set()) & readable:
                continue
            entries.append({"ip": ip, "port": port, **stats.model_dump()})
        return entries

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        next_refresh = 0.0
        while True:
            started = time.monotonic()
            try:
                if started >= next_refresh:
                    self.refresh_targets(await asyncio.to_thread(self._discover))
                    next_refresh = started + TARGETS_REFRESH_SEC
                await self.run_round()
            except Exception as exc:
                logger.error("Loss probe round failed: %s", exc, exc_info=True)
            await asyncio.sleep(max(0.0, ATTEMPT_INTERVAL_SEC - (time.monotonic() - started)))


def discover_targets() -> dict[Target, set[Source]]:
    from app.services.dnat_manager import get_dnat_manager
    from app.services.haproxy_manager import get_haproxy_manager

    found: dict[Target, set[Source]] = {}
    rules, _ = get_dnat_manager().load_state()
    for source, targets in (
        (Source.HAPROXY, haproxy_targets(get_haproxy_manager().get_stats())),
        (Source.DNAT, dnat_targets(rules)),
    ):
        for target in targets:
            found.setdefault(target, set()).add(source)
    return found


_probe: Optional[LossProbe] = None


def get_loss_probe() -> LossProbe:
    global _probe
    if _probe is None:
        _probe = LossProbe(discover=discover_targets)
    return _probe
