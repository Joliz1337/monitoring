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

Правило HAProxy с `source` ходит к серверу не с основного адреса релея, а путь
оттуда бывает другим: с основного адрес недоступен, с выходного отвечает. Обе
проверки идут с выходных адресов правила, по одному за раунд по кругу.

С какого адреса дорога есть, а с какого нет, видно только в сравнении: по каждому
пути «адрес назначения — свой адрес» ведётся своё окно, а остальные адреса релея
проверяются справочно, отдельными задачами. Справочные результаты ничего не
решают — их читает только подсказка на странице «Потери».
"""

import asyncio
import ipaddress
import logging
import socket
import time
from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
from statistics import fmean
from typing import Awaitable, Callable, Iterable, Optional, Sequence

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
# Справочные проверки с адресов релея, которыми правило не ходит: окно короче
# основного — адреса проверяются по очереди, и длинное окно копилось бы часами
REFERENCE_WINDOW_ATTEMPTS = 30
MAX_CONCURRENT_REFERENCE_ATTEMPTS = 32
MAX_CONCURRENT_REFERENCE_CLIENT_PROBES = 16
# Каждый лишний адрес в очереди — реже проверка каждого: больше стольких не берётся
MAX_SPARE_WAYS = 8


class Source(str, Enum):
    HAPROXY = "haproxy"
    DNAT = "dnat"


# Адреса релея, с которых правила ходят к адресу назначения; None — адрес по умолчанию
Exits = tuple[Optional[str], ...]
DEFAULT_EXIT: Exits = (None,)
# Путь: адрес назначения и свой адрес релея, с которого к нему идут
Way = tuple[Target, Optional[str]]


@dataclass(frozen=True)
class Discovery:
    targets: dict[Target, set[Source]]
    # Источники, которые не удалось прочитать: их прежние адреса остаются
    unknown: frozenset[Source] = frozenset()
    # Каким PROXY-заголовком правила HAProxy открывают соединение с адресом;
    # None — HAProxy не прочитан, остаются прежние
    proxy_headers: Optional[dict[Target, ProxyHeader]] = None
    # Выходные адреса правил HAProxy; у адреса только с основным записи нет.
    # None — HAProxy не прочитан, остаются прежние
    exits: Optional[dict[Target, Exits]] = None
    # IPv4 физических интерфейсов релея; None — не прочитаны, остаются прежние
    local_ips: Optional[tuple[str, ...]] = None


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
class Outgoing:
    """Как правило HAProxy открывает соединение с сервером."""
    header: Optional[ProxyHeader] = None
    # Адрес релея из `source`; None — адрес по умолчанию
    local_ip: Optional[str] = None


@dataclass(frozen=True)
class ProxyRules:
    """Каким PROXY-заголовком и с какого адреса правила HAProxy открывают соединение
    с сервером.

    Ключ — правило и имя сервера, а не адрес: в конфиге сервер бывает задан
    доменом, и его IP известен только из show stat."""
    servers: dict[RowKey, Outgoing]
    # (правило, префикс) строк server-template: их серверы зовутся <префикс><номер>
    templates: dict[RowKey, Outgoing]
    # `source` правила: действует на его серверы без своего, где бы ни стояла строка
    section_sources: dict[str, str]

    def outgoing(self, row: RowKey) -> Outgoing:
        found = self.servers.get(row) or self._from_template(row) or Outgoing()
        if found.local_ip is None and row[0] in self.section_sources:
            return replace(found, local_ip=self.section_sources[row[0]])
        return found

    def _from_template(self, row: RowKey) -> Optional[Outgoing]:
        section, name = row
        for (template_section, prefix), outgoing in self.templates.items():
            if template_section == section and name.startswith(prefix) and name[len(prefix):].isdigit():
                return outgoing
        return None


def _proxy_option(options: list[str], inherited: Optional[ProxyHeader]) -> Optional[ProxyHeader]:
    if any(option.startswith("send-proxy-v2") for option in options):
        return ProxyHeader.V2
    if "send-proxy" in options:
        return ProxyHeader.V1
    if any(option.startswith("no-send-proxy") for option in options):
        return None
    return inherited


def _source_ip(value: str) -> Optional[str]:
    """Адрес из `source <адрес>[:<порт>]`. Не адрес (переменная окружения, 0.0.0.0
    при usesrc) — None: с какого адреса пойдёт HAProxy, отсюда не видно. Адрес с
    областью (fe80::1%eth0) тоже: после % библиотека пропускает любые символы, а
    адрес уходит в строку оболочки трассы."""
    for candidate in (value, value.rpartition(":")[0]):
        try:
            ip = ipaddress.ip_address(candidate.strip("[]"))
        except ValueError:
            continue
        scoped = ip.version == 6 and ip.scope_id is not None
        return None if ip.is_unspecified or scoped else str(ip)
    return None


def _server_options(options: list[str], inherited: Outgoing) -> Outgoing:
    """Заголовок и `source` из опций строки; чего в строке нет — как у default-server выше."""
    local_ip = inherited.local_ip
    if "source" in options[:-1]:
        local_ip = _source_ip(options[options.index("source") + 1])
    return Outgoing(_proxy_option(options, inherited.header), local_ip)


def haproxy_proxy_headers(config: str) -> ProxyRules:
    """Разбор строк server, server-template, default-server (она действует на серверы
    после себя, а из секции defaults — на все правила за ней) и source."""
    servers: dict[RowKey, Outgoing] = {}
    templates: dict[RowKey, Outgoing] = {}
    section_sources: dict[str, str] = {}
    kind = section = ""
    from_defaults = inherited = Outgoing()
    defaults_source = None
    for line in config.splitlines():
        words = line.split("#", 1)[0].split()
        if not words:
            continue
        keyword = words[0]
        if keyword in CONFIG_SECTIONS:
            kind, section = keyword, (words[1] if len(words) > 1 else "")
            if kind == "defaults":
                from_defaults, defaults_source = Outgoing(), None
            elif kind in ("backend", "listen") and defaults_source:
                section_sources[section] = defaults_source
            inherited = from_defaults
        elif keyword == "default-server":
            inherited = _server_options(words[1:], inherited)
            if kind == "defaults":
                from_defaults = inherited
        elif keyword == "source" and kind == "defaults" and len(words) >= 2:
            defaults_source = _source_ip(words[1])
        elif kind not in ("backend", "listen"):
            continue
        elif keyword == "source" and len(words) >= 2:
            section_sources.pop(section, None)
            if (local_ip := _source_ip(words[1])) is not None:
                section_sources[section] = local_ip
        elif keyword == "server" and len(words) >= 3:
            outgoing = _server_options(words[3:], inherited)
            if outgoing != Outgoing():
                servers[(section, words[1])] = outgoing
        elif keyword == "server-template" and len(words) >= 4:
            outgoing = _server_options(words[4:], inherited)
            if outgoing != Outgoing():
                templates[(section, words[1])] = outgoing
    return ProxyRules(servers, templates, section_sources)


def target_proxy_headers(rows: Iterable[tuple[RowKey, Target, Optional[int]]],
                         rules: ProxyRules) -> dict[Target, ProxyHeader]:
    """Адрес из нескольких правил берёт старший вид: сервер, ждущий v2, заголовок v1 не поймёт."""
    headers: dict[Target, ProxyHeader] = {}
    for row, target, _ in rows:
        header = rules.outgoing(row).header
        if header is ProxyHeader.V2 or (header is ProxyHeader.V1 and target not in headers):
            headers[target] = header
    return headers


def target_exits(rows: Iterable[tuple[RowKey, Target, Optional[int]]], rules: ProxyRules) -> dict[Target, Exits]:
    """С каких адресов релея правила ходят к адресу назначения: копии сервера под
    выходные IP (srv, srv_o2) дают по адресу каждая. Адрес, к которому ходят только
    с основного, в ответ не попадает."""
    exits: dict[Target, list[Optional[str]]] = {}
    for row, target, _ in rows:
        local_ip = rules.outgoing(row).local_ip
        known = exits.setdefault(target, [])
        if local_ip not in known:
            known.append(local_ip)
    return {target: tuple(known) for target, known in exits.items() if known != [None]}


def default_source_ip(ip: str, port: int) -> Optional[str]:
    """С какого своего адреса ядро пойдёт к `ip`: UDP-сокет после connect() знает
    маршрут, а пакетов при этом не уходит."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.connect((ip, port))
            return probe.getsockname()[0]
    except OSError:
        return None


def spare_ips(local_ips: Sequence[str], used: Iterable[str], target_ip: str) -> tuple[str, ...]:
    """Адреса релея, которыми правило к адресу назначения не ходит. Свои адреса — IPv4:
    к адресу назначения IPv6 с них не дойти."""
    if ":" in target_ip:
        return ()
    taken = set(used)
    return tuple(ip for ip in local_ips if ip not in taken)[:MAX_SPARE_WAYS]


def any_exit_answers(verdicts: Iterable[Optional[bool]]) -> Optional[bool]:
    """Сервер отвечает, если отвечает хотя бы с одного выходного адреса; молчит —
    если молчит со всех, откуда удалось подключиться."""
    known = [verdict for verdict in verdicts if verdict is not None]
    return any(known) if known else None


def _verdict_lifetime(queue: int) -> float:
    """Адреса одной очереди проверяются «как клиент» по одному за раунд: вердикт
    каждого живёт до его следующей очереди."""
    return CLIENT_PROBE_VALID_SEC + CLIENT_PROBE_INTERVAL_SEC * (queue - 1)


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


async def attempt(ip: str, port: int, timeout: float = ATTEMPT_TIMEOUT_SEC,
                  local_ip: Optional[str] = None) -> Optional[float]:
    """Задержка ответа в мс или None, если ответа не было. `local_ip` — адрес релея,
    с которого идти (`source` из правила); без него — адрес по умолчанию."""
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port, local_addr=(local_ip, 0) if local_ip else None), timeout,
        )
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
    attempt_fn: Callable[..., Awaitable[Optional[float]]] = attempt,
    spacing: float = CHECK_SPACING_SEC,
    exits: Sequence[Optional[str]] = DEFAULT_EXIT,
) -> ProbeStats:
    """Разовая серия попыток по запросу панели (страница «Потери»); попытки идут
    с выходных адресов правила по кругу."""
    async def spaced(index: int) -> Optional[float]:
        await asyncio.sleep(index * spacing)
        return await attempt_fn(ip, port, local_ip=exits[index % len(exits)])

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
        attempt_fn: Callable[..., Awaitable[Optional[float]]] = attempt,
        downs_fn: Callable[[Target], int] = lambda _target: 0,
        stalled_fn: Callable[[Target], Optional[bool]] = lambda _target: None,
        client_probe_fn: Callable[..., Awaitable[Optional[bool]]] = client_probe.probe,
        clock: Callable[[], float] = time.monotonic,
        source_ip_fn: Callable[[str, int], Optional[str]] = default_source_ip,
    ):
        self._discover = discover
        self._attempt = attempt_fn
        self._downs = downs_fn
        self._stalled = stalled_fn
        self._client_probe = client_probe_fn
        self._proxy_headers: dict[Target, ProxyHeader] = {}
        self._exits: dict[Target, Exits] = {}
        # Номер раунда выбирает выходной адрес: за раунд на адрес назначения уходит одна
        # проверка, сколько бы выходных у него ни было
        self._rounds = 0
        self._client_rounds = 0
        # Источники, которые панели разрешено читать: остальные адреса не проверяются
        self._client_sources: set[Source] = set()
        self._clock = clock
        # (адрес, выходной адрес) → ответил ли на последнюю проверку «как клиент» и когда она была
        self._answers: dict[tuple[Target, Optional[str]], tuple[bool, float]] = {}
        # (адрес, выходной адрес) → когда закончилась последняя проверка, чем бы ни кончилась
        self._tried: dict[tuple[Target, Optional[str]], float] = {}
        self._client_wanted_until = 0.0
        self._next_client_round = 0.0
        self._client_task: Optional[asyncio.Task] = None
        self._client_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CLIENT_PROBES)
        self._windows: dict[Target, AttemptWindow] = {}
        self._source_ip = source_ip_fn
        self._local_ips: tuple[str, ...] = ()
        # Адрес назначения → свой адрес, с которого ядро идёт к нему, когда правило адреса не задаёт
        self._default_ips: dict[Target, Optional[str]] = {}
        # Адрес назначения → адреса релея, которыми правило к нему не ходит
        self._spare: dict[Target, tuple[str, ...]] = {}
        # Окна по путям. Выходные адреса правила пишутся из попыток основного замера,
        # остальные — справочными попытками; в решения идёт только общее окно адреса
        self._exit_windows: dict[Way, AttemptWindow] = {}
        self._spare_windows: dict[Way, AttemptWindow] = {}
        self._spare_answers: dict[Way, tuple[bool, float]] = {}
        self._reference_rounds = 0
        self._reference_client_rounds = 0
        self._next_reference_client_round = 0.0
        self._reference_task: Optional[asyncio.Task] = None
        self._reference_client_task: Optional[asyncio.Task] = None
        self._reference_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REFERENCE_ATTEMPTS)
        self._reference_client_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REFERENCE_CLIENT_PROBES)
        self._sources: dict[Target, set[Source]] = {}
        self._unknown_since: dict[Source, float] = {}
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_ATTEMPTS)
        self._task: Optional[asyncio.Task] = None
        self._next_refresh = 0.0

    def refresh_targets(self, targets: dict[Target, set[Source]],
                        unknown: frozenset[Source] = frozenset(), now: float = 0.0,
                        proxy_headers: Optional[dict[Target, ProxyHeader]] = None,
                        exits: Optional[dict[Target, Exits]] = None,
                        local_ips: Optional[Sequence[str]] = None) -> None:
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
        self._sources = wanted
        if proxy_headers is not None:
            self._proxy_headers = proxy_headers
        if exits is not None:
            self._exits = exits
        if local_ips is not None:
            self._local_ips = tuple(local_ips)
        self._answers = self._of_current_ways(self._answers)
        self._tried = self._of_current_ways(self._tried)
        self._exit_windows = self._of_current_ways(self._exit_windows)
        self._refresh_spare()

    def _refresh_spare(self) -> None:
        """Какие адреса релея для каждого адреса назначения запасные; пути, которых
        больше нет, забываются вместе с окнами."""
        default_ips: dict[Target, Optional[str]] = {}
        spare: dict[Target, tuple[str, ...]] = {}
        for target in self._windows:
            exits = self.exits_for(target)
            default_ips[target] = self._source_ip(*target) if None in exits else None
            used = [ip for ip in (*exits, default_ips[target]) if ip is not None]
            spare[target] = spare_ips(self._local_ips, used, target[0])
        self._default_ips, self._spare = default_ips, spare
        current = {(target, local_ip) for target, local_ips in spare.items() for local_ip in local_ips}
        self._spare_windows = {way: window for way, window in self._spare_windows.items() if way in current}
        self._spare_answers = {way: seen for way, seen in self._spare_answers.items() if way in current}

    def spare_for(self, target: Target) -> tuple[str, ...]:
        return self._spare.get(target, ())

    def sources_of(self, target: Target) -> set[Source]:
        return set(self._sources.get(target, ()))

    def _of_current_ways(self, known: dict) -> dict:
        """Записи только по адресам назначения и выходным адресам, которые ещё проверяются."""
        return {
            (target, local_ip): value for (target, local_ip), value in known.items()
            if target in self._windows and local_ip in self.exits_for(target)
        }

    def exits_for(self, target: Target) -> Exits:
        return self._exits.get(target, DEFAULT_EXIT)

    def _exit_in_turn(self, target: Target, round_index: int) -> Optional[str]:
        exits = self.exits_for(target)
        return exits[round_index % len(exits)]

    def request_refresh(self) -> None:
        """Конфиг HAProxy или DNAT только что сменился — перечитать адреса скоро."""
        self._next_refresh = min(self._next_refresh, time.monotonic() + APPLY_SETTLE_SEC)

    async def tick(self, now: float) -> None:
        if now >= self._next_refresh:
            discovery = await asyncio.to_thread(self._discover)
            self.refresh_targets(discovery.targets, discovery.unknown, now, discovery.proxy_headers,
                                 discovery.exits, discovery.local_ips)
            self._next_refresh = now + TARGETS_REFRESH_SEC
        self._start_client_round(now)
        self._start_reference_client_round(now)
        self._start_reference_round()
        await self.run_round()

    def request_client_probe(self, sources: set[Source]) -> None:
        """Панель просит проверять адреса как клиент; без повторной просьбы проверка
        прекращается. `sources` — разделы, которые панели разрешено читать: адреса
        остальных она не увидит, и слать им рукопожатия незачем."""
        self._client_wanted_until = self._clock() + CLIENT_PROBE_LEASE_SEC
        self._client_sources = sources

    async def probe_as_client(self, target: Target) -> Optional[bool]:
        """Разовая проверка по запросу панели — с тем же заголовком и с тех же адресов,
        что у правила."""
        return any_exit_answers(await asyncio.gather(*(
            self._client_probe(target[0], target[1], self._proxy_headers.get(target), local_ip=local_ip)
            for local_ip in self.exits_for(target)
        )))

    def _start_client_round(self, now: float) -> None:
        """Раунд идёт отдельной задачей: его таймаут в разы длиннее такта замера подключений."""
        if now >= self._client_wanted_until:
            # Вердикт без свежей проверки вердиктом не считается
            self._answers.clear()
            self._tried.clear()
            return
        running = self._client_task is not None and not self._client_task.done()
        if running or now < self._next_client_round:
            return
        self._next_client_round = now + CLIENT_PROBE_INTERVAL_SEC
        self._client_task = asyncio.create_task(self._run_client_round())

    async def _run_client_round(self) -> None:
        turn, self._client_rounds = self._client_rounds, self._client_rounds + 1
        ways = [
            (target, self._exit_in_turn(target, turn))
            for target, sources in self._sources.items() if sources & self._client_sources
        ]
        verdicts = await asyncio.gather(
            *(self._limited_client_probe(target, local_ip) for target, local_ip in ways),
            return_exceptions=True,
        )
        # Раунд длится до десятков секунд: срок вердикта идёт с его конца, а не с начала
        finished = self._clock()
        failures = [verdict for verdict in verdicts if isinstance(verdict, BaseException)]
        if failures:
            # Сбой проверки одного адреса не лишает вердикта остальные
            logger.error("Client probe failed for %s of %s targets: %r", len(failures), len(ways), failures[0])
        for way, answered in zip(ways, verdicts):
            if isinstance(answered, BaseException) or way[0] not in self._windows:
                continue
            self._tried[way] = finished
            if isinstance(answered, bool):
                self._answers[way] = (answered, finished)

    async def _limited_client_probe(self, target: Target, local_ip: Optional[str]) -> Optional[bool]:
        async with self._client_semaphore:
            return await self._client_probe(target[0], target[1], self._proxy_headers.get(target), local_ip=local_ip)

    def _client_verdict(self, target: Target) -> Optional[bool]:
        exits = self.exits_for(target)
        valid_for = _verdict_lifetime(len(exits))
        now = self._clock()
        fresh = [
            seen[0] for local_ip in exits
            if (seen := self._answers.get((target, local_ip))) is not None and now - seen[1] <= valid_for
        ]
        if any(fresh):
            return True
        # Молчание — вердикт, только когда испробованы все выходные адреса: после запуска и у
        # только что добавленного адреса очередь могла не дойти до того, с которого сервер отвечает
        every_exit_tried = all(
            now - self._tried.get((target, local_ip), float("-inf")) <= valid_for for local_ip in exits
        )
        if not fresh or not every_exit_tried:
            return None
        if self._stalled(target) is False:
            # Счётчики HAProxy видят, что клиентам сервер отвечает: значит, он просто
            # не говорит с незнакомцем, и молчание на проверку ничего не значит
            return None
        return False

    async def run_round(self) -> None:
        turn, self._rounds = self._rounds, self._rounds + 1
        ways = [(target, self._exit_in_turn(target, turn)) for target in self._windows]
        results = await asyncio.gather(*(self._limited_attempt(target, local_ip) for target, local_ip in ways))
        for way, rtt_ms in zip(ways, results):
            window = self._windows.get(way[0])
            if window is not None:
                window.record(rtt_ms)
                # Та же попытка — в окно своего пути: разбивка по выходным адресам новых подключений не стоит
                self._exit_windows.setdefault(way, AttemptWindow()).record(rtt_ms)

    async def _limited_attempt(self, target: Target, local_ip: Optional[str]) -> Optional[float]:
        async with self._semaphore:
            return await self._attempt(target[0], target[1], local_ip=local_ip)

    def _spare_in_turn(self, turn: int, sources: Optional[set[Source]] = None) -> list[Way]:
        """По одному запасному адресу на адрес назначения за раунд, по кругу."""
        return [
            (target, spare[turn % len(spare)]) for target, spare in self._spare.items()
            if spare and (sources is None or self._sources.get(target, set()) & sources)
        ]

    def _start_reference_round(self) -> None:
        """Справочные попытки идут отдельной задачей: адрес без маршрута держит попытку
        до таймаута, и основному замеру ждать его незачем. Прошлый раунд не кончился —
        новый не начинается, задачи не копятся."""
        if not any(self._spare.values()):
            return
        if self._reference_task is None or self._reference_task.done():
            self._reference_task = asyncio.create_task(self.run_reference_round())

    async def run_reference_round(self) -> None:
        turn, self._reference_rounds = self._reference_rounds, self._reference_rounds + 1
        ways = self._spare_in_turn(turn)
        results = await asyncio.gather(*(self._reference_attempt(way) for way in ways), return_exceptions=True)
        for way, rtt_ms in zip(ways, results):
            # Пока раунд шёл, адрес могли убрать с интерфейса или отдать правилу
            if isinstance(rtt_ms, BaseException) or way[1] not in self.spare_for(way[0]):
                continue
            self._spare_windows.setdefault(way, AttemptWindow(REFERENCE_WINDOW_ATTEMPTS)).record(rtt_ms)

    async def _reference_attempt(self, way: Way) -> Optional[float]:
        async with self._reference_semaphore:
            return await self._attempt(way[0][0], way[0][1], local_ip=way[1])

    def _start_reference_client_round(self, now: float) -> None:
        if now >= self._client_wanted_until:
            self._spare_answers.clear()
            return
        running = self._reference_client_task is not None and not self._reference_client_task.done()
        if running or now < self._next_reference_client_round:
            return
        self._next_reference_client_round = now + CLIENT_PROBE_INTERVAL_SEC
        self._reference_client_task = asyncio.create_task(self.run_reference_client_round())

    async def run_reference_client_round(self) -> None:
        turn, self._reference_client_rounds = self._reference_client_rounds, self._reference_client_rounds + 1
        ways = self._spare_in_turn(turn, self._client_sources)
        verdicts = await asyncio.gather(*(self._reference_client_probe(way) for way in ways), return_exceptions=True)
        finished = self._clock()
        for way, answered in zip(ways, verdicts):
            if isinstance(answered, bool) and way[1] in self.spare_for(way[0]):
                self._spare_answers[way] = (answered, finished)

    async def _reference_client_probe(self, way: Way) -> Optional[bool]:
        target, local_ip = way
        async with self._reference_client_semaphore:
            return await self._client_probe(target[0], target[1], self._proxy_headers.get(target), local_ip=local_ip)

    def ways(self, target: Target) -> list[dict]:
        """Потери и ответ сервера по каждому своему адресу: выходные адреса правила, за
        ними запасные. Путь без единой попытки не показывается; [] — адрес не проверяется."""
        if target not in self._windows:
            return []
        exits, spare = self.exits_for(target), self.spare_for(target)
        # Счётчики HAProxy видят, что клиентам сервер отвечает: значит, он просто не говорит
        # с незнакомцем, и молчание на проверку ничего не значит — как в общем вердикте адреса
        silence_counts = self._stalled(target) is not False
        used = [
            self._way_row(self._exit_windows.get((target, local_ip)), self._answers.get((target, local_ip)),
                          local_ip or self._default_ips.get(target), True, len(exits), silence_counts)
            for local_ip in exits
        ]
        unused = [
            self._way_row(self._spare_windows.get((target, local_ip)), self._spare_answers.get((target, local_ip)),
                          local_ip, False, len(spare), silence_counts)
            for local_ip in spare
        ]
        return [row for row in used + unused if row is not None]

    def _way_row(self, window: Optional[AttemptWindow], seen: Optional[tuple[bool, float]],
                 local_ip: Optional[str], used: bool, queue: int, silence_counts: bool) -> Optional[dict]:
        stats = window.stats() if window else None
        if stats is None:
            return None
        fresh = seen is not None and self._clock() - seen[1] <= _verdict_lifetime(queue)
        answers = seen[0] if fresh else None
        return {
            "local_ip": local_ip, "used": used,
            "loss_pct": stats.loss_pct, "rtt_ms": stats.rtt_ms, "samples": stats.samples,
            "answers": None if answers is False and not silence_counts else answers,
            # Адреса очереди проверяются по одному за раунд: окно пути набрано за столько секунд
            "span_sec": round(stats.samples * ATTEMPT_INTERVAL_SEC * queue),
        }

    def stats_for(self, target: Optional[Target]) -> Optional[ProbeStats]:
        """Итог основного окна адреса; справочные проверки сюда не входят."""
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
        for task in (self._task, self._client_task, self._reference_task, self._reference_client_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = self._client_task = self._reference_task = self._reference_client_task = None

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
        proxy_rules = haproxy_proxy_headers(config)
        proxy_headers, exits = target_proxy_headers(rows, proxy_rules), target_exits(rows, proxy_rules)
        for _, target, _ in rows:
            found.setdefault(target, set()).add(Source.HAPROXY)
    else:
        unknown.add(Source.HAPROXY)
        proxy_headers = exits = None
    rules, _ = get_dnat_manager().load_state()
    for target in dnat_targets(rules):
        found.setdefault(target, set()).add(Source.DNAT)
    return Discovery(found, frozenset(unknown), proxy_headers, exits)


def discover_all() -> Discovery:
    """Адреса назначения и свои адреса релея: по вторым идут справочные проверки."""
    from app.services.metrics_collector import get_collector

    try:
        local_ips = tuple(get_collector().accounted_ipv4_addresses())
    except OSError as exc:
        # Адреса назначения от этого не зависят: запасные остаются прежними
        logger.warning("Loss probe: local addresses are unreadable: %s", exc)
        local_ips = None
    return replace(discover_targets(), local_ips=local_ips)


_probe: Optional[LossProbe] = None


def get_loss_probe() -> LossProbe:
    global _probe
    if _probe is None:
        _probe = LossProbe(
            discover=discover_all,
            downs_fn=get_down_counter().downs,
            stalled_fn=get_traffic_watch().stalled,
        )
    return _probe
