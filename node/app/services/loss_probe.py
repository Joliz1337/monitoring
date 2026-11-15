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

Подключение проходит и там, где данные не идут, поэтому по просьбе панели к
замеру добавляется проверка «как клиент» (client_probe): раз в полминуты с
каждым адресом начинается TLS-рукопожатие. Просьба приходит с каждым запросом
метрик и действует две минуты — без панели нода серверам ничего не шлёт.
"""

import asyncio
import ipaddress
import logging
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from statistics import fmean
from typing import Awaitable, Callable, Iterable, Optional

from app.capabilities import CapabilityPolicy
from app.models.dnat import DnatRule, DnatStateResponse
from app.models.haproxy import HAProxyStatsResponse
from app.models.loss_probe import ProbeStats
from app.services import client_probe
from app.services.client_probe import ProxyHeader
from app.services.haproxy_down_counter import RowKey, Target, get_down_counter
from app.services.haproxy_traffic_watch import (
    CONFIG_SECTIONS,
    Counters,
    counts_bytes_continuously,
    get_traffic_watch,
)

logger = logging.getLogger(__name__)

ATTEMPT_INTERVAL_SEC = 2.0
ATTEMPT_TIMEOUT_SEC = 0.8
WINDOW_ATTEMPTS = 60
TARGETS_REFRESH_SEC = 10.0
# Потолок на случай конфига с сотнями бэкендов: нагрузка проверки остаётся
# ограниченной при любом размере парка
MAX_TARGETS = 256
MAX_CONCURRENT_ATTEMPTS = 64
# После применения конфига HAProxy/DNAT список адресов перечитывается не в
# обычный срок, а через эту паузу: reload HAProxy возвращается раньше, чем новый
# процесс займёт stats socket, а удалённый адрес не должен висеть в потерях
APPLY_SETTLE_SEC = 3.0
# Ручная проверка стартует попытки с паузой: поток, а не один всплеск SYN,
# который сам мог бы упереться в ограничители на пути
CHECK_SPACING_SEC = 0.05
# Источник, который сейчас не прочитался (show stat молчит во время reload),
# держит прежние адреса не дольше этого срока: HAProxy, остановленный насовсем,
# не должен оставлять проверки своих бэкендов навсегда
UNKNOWN_GRACE_SEC = 60.0
# Рукопожатие тяжелее одного SYN и на сервере с сайтом-заглушкой уходит дальше,
# на сам сайт: полминуты хватает, чтобы заметить молчание, и не выглядит потоком
CLIENT_PROBE_INTERVAL_SEC = 30.0
CLIENT_PROBE_LEASE_SEC = 120.0
# Раунд, в котором подключиться не удалось, вердикта не даёт: прежний живёт столько
CLIENT_PROBE_VALID_SEC = 90.0
MAX_CONCURRENT_CLIENT_PROBES = 32


class Source(str, Enum):
    HAPROXY = "haproxy"
    DNAT = "dnat"


@dataclass(frozen=True)
class Discovery:
    targets: dict[Target, set[Source]]
    # Источники, которые не удалось прочитать: их прежние адреса остаются
    unknown: frozenset[Source] = frozenset()
    # Каким PROXY-заголовком правила HAProxy открывают соединение с адресом;
    # None — HAProxy не прочитан, остаются прежние
    proxy_headers: Optional[dict[Target, ProxyHeader]] = None


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


def haproxy_rows(stats: HAProxyStatsResponse) -> list[tuple[RowKey, Target, Optional[int]]]:
    """Строки серверов с разобранным адресом и счётчиком падений HAProxy."""
    return [
        ((proxy.name, server.name), target, server.chkdown)
        for proxy in stats.proxies
        for server in proxy.servers
        if (target := parse_addr(server.addr or "")) is not None
    ]


def haproxy_traffic(stats: HAProxyStatsResponse) -> list[tuple[RowKey, Target, Counters]]:
    """Строки серверов со счётчиками сессий и байт в обе стороны."""
    return [
        ((proxy.name, server.name), target, Counters(server.stot, server.bin, server.bout))
        for proxy in stats.proxies
        for server in proxy.servers
        if (target := parse_addr(server.addr or "")) is not None
    ]


@dataclass(frozen=True)
class ProxyRules:
    """Каким PROXY-заголовком правила HAProxy открывают соединение с сервером.

    Ключ — правило и имя сервера, а не адрес: в конфиге сервер бывает задан
    доменом, и его IP известен только из show stat."""
    servers: dict[RowKey, ProxyHeader]
    # (правило, префикс) строк server-template: их серверы зовутся <префикс><номер>
    templates: dict[RowKey, ProxyHeader]

    def header(self, row: RowKey) -> Optional[ProxyHeader]:
        if row in self.servers:
            return self.servers[row]
        section, name = row
        for (template_section, prefix), header in self.templates.items():
            if template_section == section and name.startswith(prefix) and name[len(prefix):].isdigit():
                return header
        return None


def _proxy_option(options: list[str], inherited: Optional[ProxyHeader]) -> Optional[ProxyHeader]:
    if any(option.startswith("send-proxy-v2") for option in options):
        return ProxyHeader.V2
    if "send-proxy" in options:
        return ProxyHeader.V1
    if any(option.startswith("no-send-proxy") for option in options):
        return None
    return inherited


def haproxy_proxy_headers(config: str) -> ProxyRules:
    """Разбор строк server, server-template и default-server (она действует на серверы
    после себя, а из секции defaults — на все правила за ней)."""
    servers: dict[RowKey, ProxyHeader] = {}
    templates: dict[RowKey, ProxyHeader] = {}
    kind = section = ""
    from_defaults = inherited = None
    for line in config.splitlines():
        words = line.split("#", 1)[0].split()
        if not words:
            continue
        keyword = words[0]
        if keyword in CONFIG_SECTIONS:
            kind, section = keyword, (words[1] if len(words) > 1 else "")
            if kind == "defaults":
                from_defaults = None
            inherited = from_defaults
        elif keyword == "default-server":
            inherited = _proxy_option(words[1:], inherited)
            if kind == "defaults":
                from_defaults = inherited
        elif kind not in ("backend", "listen"):
            continue
        elif keyword == "server" and len(words) >= 3:
            header = _proxy_option(words[3:], inherited)
            if header is not None:
                servers[(section, words[1])] = header
        elif keyword == "server-template" and len(words) >= 4:
            header = _proxy_option(words[4:], inherited)
            if header is not None:
                templates[(section, words[1])] = header
    return ProxyRules(servers, templates)


def target_proxy_headers(rows: Iterable[tuple[RowKey, Target, Optional[int]]],
                         rules: ProxyRules) -> dict[Target, ProxyHeader]:
    """Адрес из нескольких правил берёт старший вид: сервер, ждущий v2, заголовок v1 не поймёт."""
    headers: dict[Target, ProxyHeader] = {}
    for row, target, _ in rows:
        header = rules.header(row)
        if header is ProxyHeader.V2 or (header is ProxyHeader.V1 and target not in headers):
            headers[target] = header
    return headers


def haproxy_targets(stats: HAProxyStatsResponse) -> set[Target]:
    return {target for _, target, _ in haproxy_rows(stats)}


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


async def check_target(
    ip: str,
    port: int,
    attempts: int,
    attempt_fn: Callable[[str, int], Awaitable[Optional[float]]] = attempt,
    spacing: float = CHECK_SPACING_SEC,
) -> ProbeStats:
    """Разовая серия попыток по запросу панели (страница «Потери»)."""
    async def spaced(index: int) -> Optional[float]:
        await asyncio.sleep(index * spacing)
        return await attempt_fn(ip, port)

    window = AttemptWindow(size=attempts)
    for rtt_ms in await asyncio.gather(*(spaced(index) for index in range(attempts))):
        window.record(rtt_ms)
    return window.stats()


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
        discover: Callable[[], Discovery],
        attempt_fn: Callable[[str, int], Awaitable[Optional[float]]] = attempt,
        downs_fn: Callable[[Target], int] = lambda _target: 0,
        stalled_fn: Callable[[Target], Optional[bool]] = lambda _target: None,
        client_probe_fn: Callable[[str, int, Optional[ProxyHeader]], Awaitable[Optional[bool]]] = client_probe.probe,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._discover = discover
        self._attempt = attempt_fn
        self._downs = downs_fn
        self._stalled = stalled_fn
        self._client_probe = client_probe_fn
        self._proxy_headers: dict[Target, ProxyHeader] = {}
        # Источники, которые панели разрешено читать: остальные адреса не проверяются
        self._client_sources: set[Source] = set()
        self._clock = clock
        # Адрес → ответил ли на последнюю проверку «как клиент» и когда она была
        self._answers: dict[Target, tuple[bool, float]] = {}
        self._client_wanted_until = 0.0
        self._next_client_round = 0.0
        self._client_task: Optional[asyncio.Task] = None
        self._client_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CLIENT_PROBES)
        self._windows: dict[Target, AttemptWindow] = {}
        self._sources: dict[Target, set[Source]] = {}
        self._unknown_since: dict[Source, float] = {}
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_ATTEMPTS)
        self._task: Optional[asyncio.Task] = None
        self._next_refresh = 0.0

    def refresh_targets(self, targets: dict[Target, set[Source]],
                        unknown: frozenset[Source] = frozenset(), now: float = 0.0,
                        proxy_headers: Optional[dict[Target, ProxyHeader]] = None) -> None:
        """История адреса переживает обновление списка; пропавший адрес забывается.
        Адреса непрочитанного источника остаются: одно неудачное чтение show stat
        стёрло бы окна всех бэкендов."""
        self._unknown_since = {source: self._unknown_since.get(source, now) for source in unknown}
        held = {source for source, since in self._unknown_since.items() if now - since < UNKNOWN_GRACE_SEC}
        merged = {target: set(sources) for target, sources in targets.items()}
        for target, sources in self._sources.items():
            if kept := sources & held:
                merged.setdefault(target, set()).update(kept)
        if len(merged) > MAX_TARGETS:
            logger.warning("Loss probe: %s targets, probing the first %s", len(merged), MAX_TARGETS)
        wanted = dict(sorted(merged.items())[:MAX_TARGETS])
        self._windows = {target: self._windows.get(target) or AttemptWindow() for target in wanted}
        self._answers = {target: seen for target, seen in self._answers.items() if target in wanted}
        self._sources = wanted
        if proxy_headers is not None:
            self._proxy_headers = proxy_headers

    def request_refresh(self) -> None:
        """Конфиг HAProxy или DNAT только что сменился — перечитать адреса скоро."""
        self._next_refresh = min(self._next_refresh, time.monotonic() + APPLY_SETTLE_SEC)

    async def tick(self, now: float) -> None:
        if now >= self._next_refresh:
            discovery = await asyncio.to_thread(self._discover)
            self.refresh_targets(discovery.targets, discovery.unknown, now, discovery.proxy_headers)
            self._next_refresh = now + TARGETS_REFRESH_SEC
        self._start_client_round(now)
        await self.run_round()

    def request_client_probe(self, sources: set[Source]) -> None:
        """Панель просит проверять адреса как клиент; без повторной просьбы проверка
        прекращается. `sources` — разделы, которые панели разрешено читать: адреса
        остальных она не увидит, и слать им рукопожатия незачем."""
        self._client_wanted_until = self._clock() + CLIENT_PROBE_LEASE_SEC
        self._client_sources = sources

    async def probe_as_client(self, target: Target) -> Optional[bool]:
        """Разовая проверка по запросу панели — с тем же заголовком, что у правила."""
        return await self._client_probe(target[0], target[1], self._proxy_headers.get(target))

    def _start_client_round(self, now: float) -> None:
        """Раунд идёт отдельной задачей: его таймаут в разы длиннее такта замера подключений."""
        if now >= self._client_wanted_until:
            # Вердикт без свежей проверки вердиктом не считается
            self._answers.clear()
            return
        running = self._client_task is not None and not self._client_task.done()
        if running or now < self._next_client_round:
            return
        self._next_client_round = now + CLIENT_PROBE_INTERVAL_SEC
        self._client_task = asyncio.create_task(self._run_client_round())

    async def _run_client_round(self) -> None:
        targets = [target for target, sources in self._sources.items() if sources & self._client_sources]
        verdicts = await asyncio.gather(
            *(self._limited_client_probe(target, self._proxy_headers.get(target)) for target in targets),
            return_exceptions=True,
        )
        # Раунд длится до десятков секунд: срок вердикта идёт с его конца, а не с начала
        finished = self._clock()
        failures = [verdict for verdict in verdicts if isinstance(verdict, BaseException)]
        if failures:
            # Сбой проверки одного адреса не лишает вердикта остальные
            logger.error("Client probe failed for %s of %s targets: %r", len(failures), len(targets), failures[0])
        for target, answered in zip(targets, verdicts):
            if isinstance(answered, bool) and target in self._windows:
                self._answers[target] = (answered, finished)

    async def _limited_client_probe(self, target: Target, proxy: Optional[ProxyHeader]) -> Optional[bool]:
        async with self._client_semaphore:
            return await self._client_probe(target[0], target[1], proxy)

    def _client_verdict(self, target: Target) -> Optional[bool]:
        seen = self._answers.get(target)
        if seen is None or self._clock() - seen[1] > CLIENT_PROBE_VALID_SEC:
            return None
        answered = seen[0]
        if not answered and self._stalled(target) is False:
            # Счётчики HAProxy видят, что клиентам сервер отвечает: значит, он просто
            # не говорит с незнакомцем, и молчание на проверку ничего не значит
            return None
        return answered

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
        stats = window.stats() if window else None
        if stats is not None:
            stats.answers = self._client_verdict(target)
        return stats

    def annotate_haproxy(self, stats: HAProxyStatsResponse) -> HAProxyStatsResponse:
        for proxy in stats.proxies:
            for server in proxy.servers:
                server.probe = self.stats_for(parse_addr(server.addr or ""))
        return stats

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
        for ip, port in self._windows:
            stats = self.stats_for((ip, port))
            shown = self._sources.get((ip, port), set()) & readable
            if stats is None or not shown:
                continue
            # Падения и ответы сервера — сведения HAProxy: у цели DNAT их нет
            in_haproxy = Source.HAPROXY in shown
            entries.append({
                "ip": ip, "port": port, **stats.model_dump(),
                "downs": self._downs((ip, port)) if in_haproxy else None,
                "stalled": self._stalled((ip, port)) if in_haproxy else None,
            })
        return entries

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if not self._task:
            return
        for task in (self._task, self._client_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = self._client_task = None

    async def _loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.tick(started)
            except Exception as exc:
                logger.error("Loss probe round failed: %s", exc, exc_info=True)
            await asyncio.sleep(max(0.0, ATTEMPT_INTERVAL_SEC - (time.monotonic() - started)))


def discover_targets() -> Discovery:
    from app.services.dnat_manager import get_dnat_manager
    from app.services.haproxy_manager import get_haproxy_manager

    found: dict[Target, set[Source]] = {}
    unknown: set[Source] = set()
    haproxy = get_haproxy_manager()
    stats = HAProxyStatsResponse.model_validate(haproxy.get_stats())
    if stats.available:
        rows = haproxy_rows(stats)
        get_down_counter().observe(rows)
        # Без наблюдений вердикт слежения устаревает сам: признака нет, а не «не молчит»
        config = haproxy.get_config()
        if counts_bytes_continuously(config):
            get_traffic_watch().observe(haproxy_traffic(stats))
        proxy_headers = target_proxy_headers(rows, haproxy_proxy_headers(config))
        for _, target, _ in rows:
            found.setdefault(target, set()).add(Source.HAPROXY)
    else:
        unknown.add(Source.HAPROXY)
        proxy_headers = None
    rules, _ = get_dnat_manager().load_state()
    for target in dnat_targets(rules):
        found.setdefault(target, set()).add(Source.DNAT)
    return Discovery(found, frozenset(unknown), proxy_headers)


_probe: Optional[LossProbe] = None


def get_loss_probe() -> LossProbe:
    global _probe
    if _probe is None:
        _probe = LossProbe(
            discover=discover_targets,
            downs_fn=get_down_counter().downs,
            stalled_fn=get_traffic_watch().stalled,
        )
    return _probe
