"""Blocklist manager for IP/CIDR blocking

Handles:
- GitHub list fetching and parsing
- Deduplication and validation
- Syncing to nodes via API (both incoming and outgoing directions)
"""

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cached_property, partial
from typing import Awaitable, Callable, Iterable, Optional

import httpx
from sqlalchemy import select, and_

from app.services.allowlist_sources import get_allowlist_sources
from app.services.haproxy_profile_sync import is_server_online
from app.services.http_client import get_node_client, get_external_client, node_auth_headers
from app.services.latest_metrics import get_latest_metrics
from app.services.net_utils import is_public_range, resolve_panel_ip
from app.services.node_ips import collect_node_ips
from app.services.node_capabilities import Capability, server_allows
from app.services.node_sync_queue import KIND_BLOCKLIST, enqueue
from app.services.ping_block import PingBlockScope, load_scope
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import Server, BlocklistRule, BlocklistSource, PanelSettings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 600
UPDATE_INTERVAL = 86400  # 24 hours
CACHE_TTL = 300  # 5 minutes cache for fetched lists
MAX_LIST_BYTES = 256 * 1024 * 1024  # потолок размера скачиваемого списка (~15 млн IP)
NODE_MAX_IPSET_ENTRIES = 1_000_000  # maxelem ipset на нодах — больше нода физически не примет
SYNC_BASE_TIMEOUT = 20.0
ALLOW_SYNC_TIMEOUT = 20.0
# Жёсткий потолок на ноду поверх таймаутов httpx: повисшее SOCKS-рукопожатие
# их не соблюдает и держало бы рассылку белого списка бесконечно.
# Три запроса: белый список in, out и настройка ping
ALLOW_PUSH_BUDGET = 3 * ALLOW_SYNC_TIMEOUT + 5
SYNC_TIMEOUT_IPS_PER_SEC = 40_000  # +1 сек к таймауту синка на каждые 40k IP
SYNC_BUDGET_SLACK = 10.0  # запас бюджета ноды поверх суммы таймаутов её запросов
# Сколько живут собранные общие списки: ожившие ноды синхронизируются по одной,
# и при массовом восстановлении каждая пересобирала бы их заново
SHARED_LISTS_TTL_SECONDS = 60
# Страховочная сверка белого списка: IP ноды по DNS-имени, адреса на её
# интерфейсах и IP панели меняются без единой правки в панели
ALLOWLIST_CHECK_INTERVAL = 300
ALLOWLIST_PUSH_DEBOUNCE = 3.0  # пачка правок серверов (массовое удаление, деплой) — одна рассылка
ALLOWLIST_START_DELAY = 60  # после рестарта last_seen протухший — весь парк сочли бы офлайн
DIRECTIONS = ("in", "out")
QUEUED_MESSAGE = "Сервер офлайн — синхронизация отложена до восстановления"
DENIED_MESSAGE = "Нода закрыла блокировку IP в своих настройках"
ALLOW_FAILED_MESSAGE = "Нода не приняла белый список"
UNCHANGED_MESSAGE = "Список не менялся — нода уже его приняла"

DEFAULT_SOURCES = [
    {
        "name": "AntiScanner",
        "url": "https://raw.githubusercontent.com/shadow-netlab/traffic-guard-lists/refs/heads/main/public/antiscanner.list",
        "is_default": True,
        "direction": "in"
    },
    {
        "name": "Government Networks",
        "url": "https://raw.githubusercontent.com/shadow-netlab/traffic-guard-lists/refs/heads/main/public/government_networks.list",
        "is_default": True,
        "direction": "in"
    }
]


@dataclass(frozen=True)
class AllowPolicy:
    """Что нода пропускает всегда: белый список по направлениям и закрытый
    для всех остальных ping. Рассылаются и сверяются по хэшу вместе."""
    ips: dict[str, list[str]]
    ping_scope: PingBlockScope
    # Кому ping закрыт на момент сборки: хэш по ним, а не по настройке —
    # перенос сервера в выбранную папку настройку не меняет, а ноду менять надо
    ping_blocked_ids: frozenset[int]

    def blocks_ping(self, server: Server) -> bool:
        return self.ping_scope.covers(server.id, server.folder)

    @cached_property
    def list_digests(self) -> dict[str, str]:
        """Хэш белого списка каждого направления — по нему видно, приняла ли
        нода именно этот список."""
        return {
            direction: hashlib.sha256("\n".join(ips).encode()).hexdigest()
            for direction, ips in self.ips.items()
        }

    def digest(self) -> str:
        payload = {"ips": self.ips, "ping_blocked": sorted(self.ping_blocked_ids)}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass
class DeliveredLists:
    """Хэши списков, которые нода приняла последними, по направлениям.

    Блок-лист и белый список порознь: белый рассылается и сам по себе.
    Направления нет — что на ноде лежит, неизвестно, и список уйдёт заново."""
    block: dict[str, str] = field(default_factory=dict)
    allow: dict[str, str] = field(default_factory=dict)

    def complete(self) -> bool:
        return all(
            direction in part
            for part in (self.block, self.allow)
            for direction in DIRECTIONS
        )


class BlocklistSourceUnavailableError(Exception):
    """Источник блок-листа не скачался, и прежней копии его содержимого нет.

    Синк заменяет набор на ноде целиком, поэтому «пропустить недоступный
    источник» означало бы разблокировать все его адреса на всём парке —
    и отчитаться при этом успехом.
    """

    def __init__(self, source_name: str, url: str, reason: str):
        self.source_name = source_name
        self.url = url
        self.reason = reason
        super().__init__(f"Blocklist source '{source_name}' is unavailable: {reason}")


class BlocklistManager:
    # Fan-out по нодам ограничен семафорами, поэтому нагрузка на БД и общий HTTP-пул
    # остаётся постоянной независимо от числа серверов — сервера синкаются волнами.
    DB_CONCURRENCY = 10    # max parallel DB sessions during fan-out sync
    HTTP_CONCURRENCY = 50  # max parallel servers syncing over HTTP at once

    def __init__(self):
        self._running = False
        self._update_task: Optional[asyncio.Task] = None
        self._cache: dict[str, tuple[float, list[str]]] = {}
        # Последнее удачное содержимое каждого источника — переживает истечение
        # кэша и служит подменой на время сбоя скачивания. Чистится по факту
        # состава включённых источников, иначе список отключённого висел бы в
        # памяти навсегда — на миллионах адресов это сотни мегабайт.
        self._last_good: dict[str, list[str]] = {}
        self._last_sync: Optional[dict] = None
        self._sync_in_progress = False
        self._db_sem = asyncio.Semaphore(self.DB_CONCURRENCY)
        self._http_sem = asyncio.Semaphore(self.HTTP_CONCURRENCY)
        self._sync_lock = asyncio.Lock()
        # Отдельные JSON-тела нужны только нодам со своими правилами; при списках
        # в миллионы IP каждое тело ~70 МБ — больше двух одновременно не собираем
        self._extra_body_sem = asyncio.Semaphore(2)
        # Свой семафор: рассылка белого списка не ждёт, пока полный синк
        # на миллионах адресов отпустит общие слоты
        self._allow_http_sem = asyncio.Semaphore(self.HTTP_CONCURRENCY)
        self._allowlist_task: Optional[asyncio.Task] = None
        self._allowlist_requested = asyncio.Event()
        # Хэш белого списка, разосланного на весь парк; None — разослать заново
        self._allowlist_pushed_hash: Optional[str] = None
        self._allowlist_inflight_hash: Optional[str] = None
        # Растёт, когда синк блок-листа доставил части нод белый список,
        # отличный от разосланного на весь парк
        self._allowlist_epoch = 0
        self._allowlist_listeners: list[Callable[[], None]] = []
        # Что приняла каждая нода, по server_id. Записи нет — что на ноде
        # лежит, неизвестно: новая нода, сбой доставки, рестарт панели
        self._delivered: defaultdict[int, DeliveredLists] = defaultdict(DeliveredLists)
        # Синки блок-листа идут одной ноде строго по очереди: вперемешку они
        # оставили бы на ноде список одного, а в памяти — хэш другого
        self._node_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Собранные общие списки, таймер их сброса и сборка, идущая прямо сейчас
        self._shared_cache: Optional[dict] = None
        self._shared_expiry: Optional[asyncio.TimerHandle] = None
        self._shared_build: Optional[asyncio.Task] = None
        # Парку причитается полный синк: источник сменился либо прошлый синк
        # сорвался целиком (недоступен источник, сбой БД), и до нод это ещё
        # не дошло — суточный цикл досинхронизирует
        self._fleet_sync_owed = False
    
    def _validate_ip_cidr(self, ip: str) -> bool:
        ip = ip.strip()
        if not ip:
            return False
        try:
            if '/' in ip:
                ipaddress.ip_network(ip, strict=False)
            else:
                ipaddress.ip_address(ip)
            return True
        except ValueError:
            return False
    
    def _normalize_ip(self, ip: str) -> str:
        ip = ip.strip()
        try:
            if '/' in ip:
                network = ipaddress.ip_network(ip, strict=False)
                if network.version == 4 and network.prefixlen == 32:
                    return str(network.network_address)
                return str(network)
            else:
                return str(ipaddress.ip_address(ip))
        except ValueError:
            return ip
    
    def deduplicate_ips(self, ips: list[str]) -> list[str]:
        seen = set()
        result = []
        for ip in ips:
            normalized = self._normalize_ip(ip)
            if normalized and normalized not in seen and self._validate_ip_cidr(normalized):
                seen.add(normalized)
                result.append(normalized)
        return result
    
    def parse_list_content(self, content: str) -> list[str]:
        """Извлечь блокируемые IP/CIDR из текста списка.

        Приватные/служебные диапазоны (bogons в firehol и подобных списках)
        отбрасываются: DROP по ним убивает loopback и docker-сети нод."""
        ips = []
        skipped_non_public = 0
        for line in content.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '#' in line:
                line = line.split('#')[0].strip()
            if not line:
                continue
            if not self._validate_ip_cidr(line):
                continue
            normalized = self._normalize_ip(line)
            if not is_public_range(normalized):
                skipped_non_public += 1
                continue
            ips.append(normalized)
        if skipped_non_public:
            logger.warning(f"Blocklist source: dropped {skipped_non_public} non-public range(s)")
        return ips
    
    def _get_cached(self, url: str) -> Optional[list[str]]:
        if url in self._cache:
            timestamp, ips = self._cache[url]
            if time.monotonic() - timestamp < CACHE_TTL:
                return ips
            del self._cache[url]
        return None

    def _set_cache(self, url: str, ips: list[str]):
        # Протухшие записи выкидываются сразу: список отключённого источника
        # может весить сотни МБ и иначе висел бы в памяти навсегда
        now = time.monotonic()
        expired = [u for u, (ts, _) in self._cache.items() if now - ts >= CACHE_TTL]
        for u in expired:
            del self._cache[u]
        self._cache[url] = (now, ips)

    async def fetch_github_list(
        self, url: str, timeout: float = 30.0, use_cache: bool = True
    ) -> tuple[bool, list[str], str]:
        if use_cache:
            cached = self._get_cached(url)
            if cached is not None:
                return True, cached, ""
        try:
            client = get_external_client()
            chunks: list[bytes] = []
            total = 0
            async with client.stream("GET", url, timeout=timeout) as response:
                if response.status_code != 200:
                    return False, [], f"HTTP {response.status_code}"
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_LIST_BYTES:
                        return False, [], f"List exceeds {MAX_LIST_BYTES // (1024 * 1024)} MB limit"
                    chunks.append(chunk)
            content = b"".join(chunks).decode("utf-8", errors="replace")
            chunks.clear()
            # Парсинг больших списков (100k+ строк) — CPU-bound, в поток,
            # иначе event loop замирает на десятки секунд
            ips = await asyncio.to_thread(self.parse_list_content, content)
            self._set_cache(url, ips)
            self._last_good[url] = ips
            return True, ips, ""
        except httpx.TimeoutException:
            return False, [], "Timeout"
        except httpx.RequestError as e:
            return False, [], f"Request error: {str(e)}"
        except Exception as e:
            logger.error(f"Failed to fetch {url}: {e}")
            return False, [], str(e)

    def calculate_hash(self, ips: list[str]) -> str:
        # Хэш по элементам вместо '\n'.join: склейка миллионов строк давала
        # лишние ~120 МБ транзиентной памяти на каждый refresh
        digest = hashlib.sha256()
        for ip in sorted(set(ips)):
            digest.update(ip.encode())
            digest.update(b'\n')
        return digest.hexdigest()
    
    async def get_setting(self, key: str, db: AsyncSession) -> Optional[str]:
        result = await db.execute(
            select(PanelSettings).where(PanelSettings.key == key)
        )
        setting = result.scalar_one_or_none()
        return setting.value if setting else None
    
    async def get_blocklist_settings(self, db: AsyncSession) -> dict:
        settings = {}
        auto_update = await self.get_setting("blocklist_auto_update_enabled", db)
        settings["auto_update_enabled"] = auto_update != "false" if auto_update else True
        interval = await self.get_setting("blocklist_auto_update_interval", db)
        settings["auto_update_interval"] = int(interval) if interval else UPDATE_INTERVAL
        return settings
    
    async def get_global_rules(
        self, db: AsyncSession, direction: str = "in", list_type: str = "block"
    ) -> list[str]:
        # Порядок строк входит в хэш тела синка: без ORDER BY тот же список
        # в другом порядке считался бы новым и ушёл бы на весь парк заново
        result = await db.execute(
            select(BlocklistRule).where(
                and_(
                    BlocklistRule.server_id.is_(None),
                    BlocklistRule.is_permanent == True,
                    BlocklistRule.direction == direction,
                    BlocklistRule.list_type == list_type
                )
            ).order_by(BlocklistRule.id)
        )
        rules = result.scalars().all()
        return [r.ip_cidr for r in rules]

    async def build_allow_policy(self) -> AllowPolicy:
        """Белый список по направлениям: ручные allow-правила + авто (IP панели и все
        адреса нод); во входящих — ещё и адреса из списков по URL.

        IP панели и нод всегда в allowlist — ACCEPT стоит первым в цепочке,
        поэтому управляющий трафик не попадёт под DROP даже при плохом блок-листе."""
        async with async_session() as db:
            servers = (await db.execute(
                select(Server.id, Server.url, Server.folder, Server.dedicated_cert)
                .where(Server.is_active == True)  # noqa: E712
            )).all()
            manual = {
                direction: await self.get_global_rules(db, direction, list_type="allow")
                for direction in DIRECTIONS
            }
            ping_scope = await load_scope(db)

        # Адреса интерфейсов — из последних ответов нод в памяти: колонка
        # servers.last_metrics обновляется редко. Словари общие, только читаем
        metrics = get_latest_metrics().get_many(row.id for row in servers)
        # Резолв — после закрытия сессии: медленный DNS не держит коннект пула
        auto = await collect_node_ips(
            (row.url, metrics.get(row.id), row.dedicated_cert) for row in servers
        )
        panel_ip = await resolve_panel_ip()
        if panel_ip:
            auto.add(panel_ip)
        # Списки по URL — про тех, кто приходит к нодам (CDN): только входящие
        listed = {"in": await get_allowlist_sources().collect()}

        ips = {
            direction: self.deduplicate_ips(sorted(auto.union(manual[direction], listed.get(direction, ()))))
            for direction in DIRECTIONS
        }
        blocked = frozenset(row.id for row in servers if ping_scope.covers(row.id, row.folder))
        return AllowPolicy(ips=ips, ping_scope=ping_scope, ping_blocked_ids=blocked)
    
    async def get_server_rules(self, server_id: int, db: AsyncSession, direction: str = "in") -> list[str]:
        result = await db.execute(
            select(BlocklistRule).where(
                and_(
                    BlocklistRule.server_id == server_id,
                    BlocklistRule.is_permanent == True,
                    BlocklistRule.direction == direction,
                    BlocklistRule.list_type == "block"
                )
            )
        )
        rules = result.scalars().all()
        return [r.ip_cidr for r in rules]
    
    async def get_auto_list_ips(self, db: AsyncSession, direction: str = "in") -> list[str]:
        # Строка источника переписывается при каждом обновлении и переезжает
        # в таблице — без ORDER BY источники менялись бы местами, а с ними
        # и порядок адресов в теле синка (см. get_global_rules)
        result = await db.execute(
            select(BlocklistSource).where(
                and_(
                    BlocklistSource.enabled == True,
                    BlocklistSource.direction == direction
                )
            ).order_by(BlocklistSource.id)
        )
        sources = result.scalars().all()
        all_ips = []
        for source in sources:
            success, ips, error = await self.fetch_github_list(source.url)
            if not success:
                # Прежняя копия лучше, чем усечённый список: она хотя бы
                # соответствует реальности вчерашнего дня.
                ips = self._last_good.get(source.url)
                if ips is None:
                    raise BlocklistSourceUnavailableError(source.name, source.url, error)
                logger.warning(
                    f"Source '{source.name}' unreachable ({error}) — "
                    f"using last known good copy of {len(ips)} entries"
                )
            if len(ips) > NODE_MAX_IPSET_ENTRIES:
                logger.warning(
                    f"Source '{source.name}' skipped: {len(ips)} IPs exceeds "
                    f"node ipset limit ({NODE_MAX_IPSET_ENTRIES})"
                )
                continue
            all_ips.extend(ips)
        return all_ips

    async def _prune_last_good(self, db: AsyncSession) -> None:
        """Оставить в подменном хранилище только включённые сейчас источники."""
        result = await db.execute(
            select(BlocklistSource.url).where(BlocklistSource.enabled == True)
        )
        enabled = {row[0] for row in result.all()}
        for url in [u for u in self._last_good if u not in enabled]:
            del self._last_good[url]
    
    @staticmethod
    def _merge_deduplicated(manual_ips: list[str], auto_ips: list[str]) -> tuple[list[str], set[str], int]:
        """Слить уже нормализованные списки без повторной нормализации.

        deduplicate_ips прогонял все записи через _normalize_ip заново и создавал
        вторую копию каждой строки — на списке в 4 млн IP это ~500 МБ лишней памяти.

        Итог обрезается по NODE_MAX_IPSET_ENTRIES: ipset на нодах создан с этим
        maxelem, сверх лимита restore падает и синк разваливается целиком.
        Ручные правила идут первыми и под обрезку не попадают."""
        block: list[str] = []
        block_set: set[str] = set()
        overflow = 0
        for ips in (manual_ips, auto_ips):
            for ip in ips:
                if ip in block_set:
                    continue
                if len(block) >= NODE_MAX_IPSET_ENTRIES:
                    overflow += 1
                    continue
                block_set.add(ip)
                block.append(ip)
        return block, block_set, overflow

    @staticmethod
    def _build_sync_body(ips: list[str], direction: str) -> bytes:
        return json.dumps(
            {"ips": ips, "permanent": True, "direction": direction},
            separators=(",", ":"),
        ).encode()

    async def build_shared_lists(self) -> dict:
        """Общие для всех серверов списки (глобальные правила + авто-источники + allow).

        Считается ОДИН раз на прогон синка: дедупликация 100k+ записей отдельно
        для каждого сервера (42 сервера × 2 направления) съедала минуты CPU и
        вешала event loop панели. Тяжёлый дедуп — в потоке.

        JSON-тело запроса тоже собирается один раз на направление и переиспользуется
        всеми нодами без своих правил: сериализация списка на каждую ноду держала
        в памяти десятки тел по ~70 МБ одновременно — OOM панели на списке в 4 млн IP."""
        policy = await self.build_allow_policy()
        shared = {"allow_policy": policy}
        async with async_session() as db:
            await self._prune_last_good(db)
            for direction in DIRECTIONS:
                global_ips = await self.get_global_rules(db, direction)
                auto_ips = await self.get_auto_list_ips(db, direction)
                # auto_ips уже нормализованы и отфильтрованы при парсинге; ручные
                # правила (в т.ч. старые записи в БД) чистятся от приватных диапазонов
                manual_ips = [
                    ip for ip in self.deduplicate_ips(global_ips)
                    if is_public_range(ip)
                ]
                block, block_set, overflow = await asyncio.to_thread(
                    self._merge_deduplicated, manual_ips, auto_ips
                )
                if overflow:
                    logger.warning(
                        f"Blocklist '{direction}': {overflow} entries dropped — "
                        f"node ipset limit is {NODE_MAX_IPSET_ENTRIES}"
                    )
                body = await asyncio.to_thread(self._build_sync_body, block, direction)
                shared[direction] = {
                    "block": block,
                    "block_set": block_set,
                    "count": len(block),
                    "body": body,
                    "digest": (await asyncio.to_thread(hashlib.sha256, body)).hexdigest(),
                    "allow": policy.ips[direction],
                    "allow_digest": policy.list_digests[direction],
                }
        return shared
    
    async def _shared_lists(self) -> dict:
        """Общие списки из кэша; сборку, которая уже идёт, все ждут вместе.

        Синк одной ноды иначе пересобирал бы их под себя: при массовом
        восстановлении это тысячи сборок на сотни тысяч адресов."""
        if self._shared_cache is not None:
            return self._shared_cache
        if self._shared_build is None:
            self._shared_build = asyncio.create_task(self._build_and_keep_shared())
        # Отмена одного из ждущих не должна оборвать сборку остальным
        return await asyncio.shield(self._shared_build)

    async def _build_and_keep_shared(self) -> dict:
        this_build = asyncio.current_task()
        try:
            shared = await self.build_shared_lists()
            # Сброшенная по ходу сборка могла прочитать старые правила — не храним
            if self._shared_build is this_build:
                self._shared_cache = shared
                # Списки на миллионы адресов — сотни мегабайт: по истечении срока
                # их надо отпустить, а не держать до следующего синка
                self._shared_expiry = asyncio.get_running_loop().call_later(
                    SHARED_LISTS_TTL_SECONDS, self.invalidate_shared_lists
                )
            return shared
        finally:
            if self._shared_build is this_build:
                self._shared_build = None

    def invalidate_shared_lists(self) -> None:
        """Забыть собранные общие списки: сменились правила, источники или белый список.

        Идущую сборку новые вызовы тоже не ждут — она началась до правки."""
        self._shared_cache = None
        self._shared_build = None
        if self._shared_expiry is not None:
            self._shared_expiry.cancel()
            self._shared_expiry = None

    @staticmethod
    def _sync_timeout(ip_count: int) -> float:
        """Таймаут синка растёт со списком: ноде нужно время распарсить и применить."""
        return SYNC_BASE_TIMEOUT + ip_count / SYNC_TIMEOUT_IPS_PER_SEC

    async def sync_to_node(
        self,
        server: Server,
        body: bytes,
        ip_count: int,
    ) -> tuple[bool, str, dict]:
        """Отправить готовое JSON-тело блок-листа на ноду.

        Тело сериализуется заранее (build_shared_lists) и переиспользуется
        всеми нодами — httpx с json= собирал бы свою копию на каждый запрос."""
        try:
            client = get_node_client(server)
            response = await client.post(
                f"{server.url}/api/ipset/sync",
                headers={**node_auth_headers(server), "Content-Type": "application/json"},
                content=body,
                timeout=self._sync_timeout(ip_count),
            )
            if response.status_code == 200:
                data = response.json()
                return True, data.get("message", "Synced"), data
            else:
                return False, f"HTTP {response.status_code}", {}
        except httpx.TimeoutException:
            return False, "Timeout", {}
        except httpx.RequestError as e:
            return False, f"Request error: {str(e)}", {}
        except Exception as e:
            logger.error(f"Failed to sync to {server.name}: {e}")
            return False, str(e), {}

    async def sync_allow_to_node(
        self,
        server: Server,
        ips: list[str],
        direction: str = "in",
        timeout: float = ALLOW_SYNC_TIMEOUT
    ) -> tuple[bool, str, dict]:
        """Синхронизировать белый список на ноду. На старой ноде (нет эндпоинта) — graceful."""
        try:
            client = get_node_client(server)
            response = await client.post(
                f"{server.url}/api/ipset/allowlist/sync",
                headers=node_auth_headers(server),
                json={"ips": ips, "direction": direction},
                timeout=timeout,
            )
            if response.status_code == 200:
                data = response.json()
                return True, data.get("message", "Synced"), data
            if response.status_code == 404:
                # нода старой версии без allowlist — не считаем фатальной ошибкой
                return True, "Allowlist not supported by node", {}
            return False, f"HTTP {response.status_code}", {}
        except httpx.TimeoutException:
            return False, "Timeout", {}
        except httpx.RequestError as e:
            return False, f"Request error: {str(e)}", {}
        except Exception as e:
            logger.error(f"Failed to sync allowlist to {server.name}: {e}")
            return False, str(e), {}

    async def sync_ping_to_node(self, server: Server, block: bool) -> tuple[bool, str]:
        """Закрыть или открыть ping на ноде.

        Агент старше MIN_NODE_VERSION_PING_BLOCK эндпоинта не знает — это не
        сбой: повтор из очереди ответил бы тем же 404."""
        try:
            response = await get_node_client(server).post(
                f"{server.url}/api/ipset/ping-block",
                headers=node_auth_headers(server),
                json={"enabled": block},
                timeout=ALLOW_SYNC_TIMEOUT,
            )
        except httpx.TimeoutException:
            return False, "Timeout"
        except httpx.RequestError as e:
            return False, f"Request error: {e}"
        if response.status_code == 404:
            return True, "Ping block not supported by node"
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"
        return True, "Ping blocked" if block else "Ping allowed"

    async def _load_extras(self, server: Server, shared: dict) -> dict[str, list[str]]:
        """Свои правила ноды, которых нет в общем списке.

        Короткая сессия БД под семафором освобождается до медленных HTTP-синков,
        иначе пул PostgreSQL исчерпывается при fan-out."""
        extras = {}
        async with self._db_sem:
            async with async_session() as db:
                for direction in DIRECTIONS:
                    server_rules = await self.get_server_rules(server.id, db, direction)
                    extras[direction] = [
                        ip for ip in self.deduplicate_ips(server_rules)
                        if is_public_range(ip) and ip not in shared[direction]["block_set"]
                    ]
        return extras

    async def _sync_one_server(self, server: Server, shared: dict) -> dict:
        """Sync both directions for a single server.

        `shared` — предвычисленные общие списки и JSON-тела из build_shared_lists();
        нода без своих правил получает общее тело как есть — ни одной копии списка.
        Бюджет времени отсчитывается только на саму доставку: чтение правил и
        очередь за слотом под отдельное тело в него не входят.
        """
        try:
            extras = await self._load_extras(server, shared)
        except Exception as e:
            logger.error(f"Failed to load blocklist IPs for {server.name}: {e}")
            return self._failed_result(server, str(e))

        known = self._delivered[server.id]
        block_digests = {
            direction: self._block_digest(shared[direction]["digest"], extras[direction])
            for direction in DIRECTIONS
        }
        # Отдельное тело на миллионах адресов весит десятки мегабайт: слот
        # держится до конца отправки, чтобы такие тела не копились по числу нод
        needs_own_body = any(
            extras[direction] and known.block.get(direction) != block_digests[direction]
            for direction in DIRECTIONS
        )
        body_slot = self._extra_body_sem if needs_own_body else contextlib.nullcontext()
        try:
            async with body_slot:
                return await asyncio.wait_for(
                    self._deliver(server, shared, extras, block_digests, known),
                    timeout=self._sync_budget(shared),
                )
        except asyncio.TimeoutError:
            return self._failed_result(server, "Timeout")

    @staticmethod
    def _block_digest(shared_digest: str, extra: list[str]) -> str:
        """Хэш блок-листа ноды: общий список плюс её собственные правила.
        Свои правила сортируются — база отдаёт их без порядка."""
        if not extra:
            return shared_digest
        return hashlib.sha256("\n".join([shared_digest, *sorted(extra)]).encode()).hexdigest()

    @staticmethod
    async def _send_if_changed(
        delivered: dict[str, str],
        direction: str,
        digest: str,
        send: Callable[[], Awaitable[tuple[bool, str, dict]]],
    ) -> tuple[bool, str, dict]:
        """Отправить ноде список, если она ещё не подтвердила именно его."""
        if delivered.get(direction) == digest:
            return True, UNCHANGED_MESSAGE, {}
        # Пока нода не подтвердила доставку, что на ней лежит — неизвестно:
        # сбой, таймаут и отмена посреди отправки оставляют список без хэша,
        # и в следующий раз он уйдёт заново
        delivered.pop(direction, None)
        accepted, message, data = await send()
        if accepted:
            delivered[direction] = digest
        return accepted, message, data

    async def _send_block(
        self, server: Server, lists: dict, extra: list[str], direction: str
    ) -> tuple[bool, str, dict]:
        ip_count = lists["count"] + len(extra)
        if not extra:
            return await self.sync_to_node(server, lists["body"], ip_count)
        # У ноды свои правила — общее тело ей не подходит, собирается своё
        body = await asyncio.to_thread(self._build_sync_body, lists["block"] + extra, direction)
        return await self.sync_to_node(server, body, ip_count)

    async def _deliver(
        self,
        server: Server,
        shared: dict,
        extras: dict[str, list[str]],
        block_digests: dict[str, str],
        known: DeliveredLists,
    ) -> dict:
        """Отправить ноде списки, которых на ней ещё нет, и настройку ping."""
        server_result = {
            "server_id": server.id,
            "server_name": server.name,
            "success": True,
            "in": {},
            "out": {},
        }

        for direction in DIRECTIONS:
            lists = shared[direction]
            extra = extras[direction]
            allow_ips = lists["allow"]
            try:
                success, message, data = await self._send_if_changed(
                    known.block, direction, block_digests[direction],
                    partial(self._send_block, server, lists, extra, direction),
                )
                allow_ok, allow_msg, allow_data = await self._send_if_changed(
                    known.allow, direction, lists["allow_digest"],
                    partial(self.sync_allow_to_node, server, allow_ips, direction=direction),
                )

                server_result[direction] = {
                    "success": success and allow_ok,
                    "message": message,
                    "ip_count": lists["count"] + len(extra),
                    "added": data.get("added", 0),
                    "removed": data.get("removed", 0),
                    "allow": {
                        "success": allow_ok,
                        "message": allow_msg,
                        "ip_count": len(allow_ips),
                        "added": allow_data.get("added", 0),
                        "removed": allow_data.get("removed", 0),
                    },
                }
                if not (success and allow_ok):
                    server_result["success"] = False
            except Exception as e:
                logger.error(f"Failed to sync {direction} to {server.name}: {e}")
                server_result[direction] = {
                    "success": False,
                    "message": str(e),
                    "ip_count": 0,
                }
                server_result["success"] = False

        # Правило ping ссылается на белый список — после него
        block_ping = shared["allow_policy"].blocks_ping(server)
        ping_ok, ping_msg = await self.sync_ping_to_node(server, block_ping)
        server_result["ping"] = {"success": ping_ok, "message": ping_msg, "blocked": block_ping}
        if not ping_ok:
            server_result["success"] = False
        return server_result

    def _sync_budget(self, shared: dict) -> float:
        """Потолок времени на одну ноду. Масштабируется под размер списков:
        фиксированные 30с обрубали синк крупных блок-листов на середине."""
        return SYNC_BUDGET_SLACK + ALLOW_SYNC_TIMEOUT + sum(
            self._sync_timeout(shared[direction]["count"]) + ALLOW_SYNC_TIMEOUT
            for direction in DIRECTIONS
        )

    async def _sync_one_server_safe(self, server: Server, shared: dict) -> dict:
        """Sync one server — never raises."""
        try:
            # Слот и очередь к ноде занимаются до отсчёта бюджета: при 2000 нодах
            # на 50 слотов хвост ждёт дольше самой доставки и получал ложный Timeout
            async with self._http_sem, self._node_locks[server.id]:
                return await self._sync_one_server(server, shared)
        except Exception as e:
            return self._failed_result(server, str(e))

    @staticmethod
    def _failed_result(server: Server, message: str) -> dict:
        failed = {"success": False, "message": message, "ip_count": 0}
        return {
            "server_id": server.id,
            "server_name": server.name,
            "success": False,
            "in": dict(failed),
            "out": dict(failed),
        }

    def _store_sync_result(self, results: dict, error: Optional[str] = None):
        self._last_sync = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "servers": results,
            "in_progress": False,
            "error": error,
        }
        self._sync_in_progress = False

    def get_sync_status(self) -> dict:
        if self._sync_in_progress:
            return {"in_progress": True, "timestamp": None, "servers": {}, "error": None}
        if self._last_sync:
            return self._last_sync
        return {"in_progress": False, "timestamp": None, "servers": {}, "error": None}

    @staticmethod
    def _queued_result(server: Server) -> dict:
        """Результат для офлайн-ноды: список ей не отправлен, долг записан в очередь."""
        skipped = {"success": False, "message": QUEUED_MESSAGE, "ip_count": 0}
        return {
            "server_id": server.id,
            "server_name": server.name,
            "success": False,
            "queued": True,
            "in": dict(skipped),
            "out": dict(skipped),
        }

    @staticmethod
    def _denied_result(server: Server) -> dict:
        """Результат для ноды, закрывшей ipset: в очередь такой долг не идёт."""
        skipped = {"success": False, "message": DENIED_MESSAGE, "ip_count": 0}
        return {
            "server_id": server.id,
            "server_name": server.name,
            "success": False,
            "denied": True,
            "in": dict(skipped),
            "out": dict(skipped),
        }

    @staticmethod
    def _split_denied(servers: list[Server]) -> tuple[list[Server], list[Server]]:
        allowed, denied = [], []
        for server in servers:
            (allowed if server_allows(server, Capability.IPSET, write=True) else denied).append(server)
        return allowed, denied

    async def _queue_offline(self, servers: list[Server]) -> dict:
        """Отложить синк офлайн-нодам: HTTP к ним всё равно упрётся в таймаут,
        а очередь доставит список сама, как только они вернутся в сеть."""
        if not servers:
            return {}
        # За время простоя ноду могли переустановить или откатить из снимка
        self._forget_delivered(s.id for s in servers)
        await enqueue([s.id for s in servers], KIND_BLOCKLIST, QUEUED_MESSAGE)
        return {s.id: self._queued_result(s) for s in servers}

    def _forget_delivered(self, server_ids: Iterable[int]) -> None:
        """Что лежит на этих нодах, больше не известно — следующая доставка
        отправит им списки целиком.

        Запись выбрасывается, а не чистится: доставка, идущая ноде прямо
        сейчас, допишет хэши в выброшенную, и они не засчитаются."""
        for server_id in server_ids:
            self._delivered.pop(server_id, None)

    async def sync_all_nodes(self) -> dict:
        """Sync blocklists to all active nodes in parallel (both directions).

        Под замком: параллельные полные синки (ручной + автообновление) не
        дублируют работу, второй вызов дождётся первого и прогонит свежие данные.
        Нода получает только те списки, которых на ней ещё нет."""
        # Сюда приходят сразу после правки правил и источников — списки,
        # собранные до неё, этой правки не содержат
        self.invalidate_shared_lists()
        async with self._sync_lock:
            self._sync_in_progress = True
            # Долг перед парком закрывается до сборки списков: источник,
            # сменившийся уже по ходу синка, поднимет его снова
            self._fleet_sync_owed = False
            results = {}
            error = None
            try:
                async with async_session() as db:
                    result = await db.execute(
                        select(Server).where(Server.is_active == True)
                    )
                    servers = result.scalars().all()

                if not servers:
                    self._store_sync_result({})
                    return {}

                # Закрывшие раздел отсекаются до подсчёта: сборка общих
                # списков на миллионах адресов стоит слишком дорого, чтобы
                # делать её ради нод, которые их не примут
                servers, denied = self._split_denied(list(servers))
                results.update({s.id: self._denied_result(s) for s in denied})

                online = [s for s in servers if is_server_online(s)]
                results.update(await self._queue_offline(
                    [s for s in servers if not is_server_online(s)]
                ))

                if online:
                    shared = await self._shared_lists()
                    tasks = [self._sync_one_server_safe(s, shared) for s in online]
                    done = await asyncio.gather(*tasks)

                    for sr in done:
                        results[sr["server_id"]] = sr
                    self._note_allow_sent(shared["allow_policy"].digest())

                    # Живая нода тоже могла не принять список (таймаут, ошибка ноды) —
                    # без очереди эта потеря так и осталась бы незамеченной.
                    await self._enqueue_failed(done)
            except BlocklistSourceUnavailableError as e:
                # Ноды не трогаем вовсе: неполный список снял бы блокировки.
                error = str(e)
                logger.error(f"sync_all_nodes cancelled: {error}")
            except Exception as e:
                error = str(e)
                logger.error(f"sync_all_nodes failed: {e}")
            finally:
                if error is not None:
                    self._fleet_sync_owed = True
                self._store_sync_result(results, error)
            return results

    async def _enqueue_failed(self, results: list[dict]) -> None:
        failed = [
            sr["server_id"] for sr in results
            if not sr.get("success") and not sr.get("denied")
        ]
        if failed:
            await enqueue(failed, KIND_BLOCKLIST, "Нода не приняла блок-лист")

    async def sync_nodes_by_ids(self, server_ids: list[int]) -> dict[int, Optional[str]]:
        """Досинхронизировать перечисленные ноды (исполнитель очереди отложенных синков).

        Общие списки собираются один раз на весь набор: на миллионах адресов тело
        синка весит десятки мегабайт, и пересборка под каждую ноду была бы дороже
        самой отправки. Возвращает по ноде None при успехе или текст ошибки.
        """
        async with async_session() as db:
            servers = list((await db.execute(
                select(Server).where(
                    Server.id.in_(server_ids),
                    Server.is_active == True  # noqa: E712
                )
            )).scalars().all())

        # Удалённый или выключенный сервер — долг закрыт, ждать его нечего.
        results: dict[int, Optional[str]] = {
            sid: None for sid in server_ids if sid not in {s.id for s in servers}
        }
        if not servers:
            return results

        # Долг ноды, закрывшей раздел, закрываем: повтор дал бы тот же отказ
        servers, denied = self._split_denied(servers)
        results.update({s.id: None for s in denied})
        if not servers:
            return results

        try:
            shared = await self._shared_lists()
        except BlocklistSourceUnavailableError as e:
            # Неполный список снял бы блокировки — ждём восстановления источника.
            return {**results, **{s.id: str(e) for s in servers}}

        done = await asyncio.gather(*[self._sync_one_server_safe(s, shared) for s in servers])
        self._note_allow_sent(shared["allow_policy"].digest())
        for sr in done:
            results[sr["server_id"]] = None if sr.get("success") else self._first_error(sr)
        self._merge_sync_results({sr["server_id"]: sr for sr in done})
        return results

    @staticmethod
    def _first_error(server_result: dict) -> str:
        for part in ("in", "out", "ping"):
            message = server_result.get(part, {}).get("message")
            if message and not server_result[part].get("success", True):
                return message
        return "Sync failed"

    def _merge_sync_results(self, results: dict) -> None:
        """Влить результаты частичного синка в общий статус, не сбрасывая флаг
        параллельно идущего полного синка."""
        servers = self._last_sync.get("servers", {}) if self._last_sync else {}
        servers.update(results)
        self._last_sync = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "servers": servers,
            "in_progress": False,
            "error": None,
        }

    async def sync_single_node_by_id(self, server_id: int) -> dict:
        """Sync one server by ID (both directions). Returns per-server result."""
        self._sync_in_progress = True
        # Сюда приходят ожившие, новые и перенастроенные ноды: что на них лежит,
        # неизвестно, поэтому списки уходят целиком, даже если не менялись
        self._forget_delivered([server_id])
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(Server).where(Server.id == server_id)
                )
                server = result.scalar_one_or_none()

            if not server:
                self._sync_in_progress = False
                return {}

            if not server_allows(server, Capability.IPSET, write=True):
                sr = self._denied_result(server)
                prev = self._last_sync.get("servers", {}) if self._last_sync else {}
                prev[server_id] = sr
                self._store_sync_result(prev)
                return sr

            if not is_server_online(server):
                sr = self._queued_result(server)
                await enqueue([server.id], KIND_BLOCKLIST, QUEUED_MESSAGE)
                prev = self._last_sync.get("servers", {}) if self._last_sync else {}
                prev[server_id] = sr
                self._store_sync_result(prev)
                return sr

            shared = await self._shared_lists()
            sr = await self._sync_one_server_safe(server, shared)
            self._note_allow_sent(shared["allow_policy"].digest())
            if not sr.get("success"):
                await enqueue([server_id], KIND_BLOCKLIST, "Нода не приняла блок-лист")
            prev = self._last_sync.get("servers", {}) if self._last_sync else {}
            prev[sr["server_id"]] = sr
            self._store_sync_result(prev)
            return sr
        except BlocklistSourceUnavailableError as e:
            logger.error(f"sync_single_node_by_id cancelled: {e}")
            prev = self._last_sync.get("servers", {}) if self._last_sync else {}
            self._store_sync_result(prev, str(e))
            return {}
        except Exception as e:
            logger.error(f"sync_single_node_by_id failed: {e}")
            self._sync_in_progress = False
            return {}

    def request_allowlist_push(self) -> None:
        """Попросить разослать белый список: изменился состав серверов или allow-правил.

        Не ждёт рассылки. Правки, пришедшие пачкой, схлопываются в одну
        рассылку, а список, который не изменился, не рассылается вовсе."""
        # Белый список входит в общие списки полного синка
        self.invalidate_shared_lists()
        self._allowlist_requested.set()

    def _note_allow_sent(self, digest: str) -> None:
        """Синк блок-листа доставил своим нодам белый список из своего снимка.

        Снимок, не совпавший с разосланным на весь парк, мог устареть за время
        долгого синка и лечь на ноду поверх свежего — тогда разослать заново."""
        # Пока идёт рассылка, сверяемся с ней: прежняя политика, которую она
        # заменяет, — уже устаревший снимок
        current = self._allowlist_inflight_hash or self._allowlist_pushed_hash
        if digest == current:
            return
        self._allowlist_pushed_hash = None
        self._allowlist_epoch += 1
        self._allowlist_requested.set()

    def add_allowlist_listener(self, listener: Callable[[], None]) -> None:
        """Сообщать о смене белого списка: его входящую часть не ограничивает
        аварийный режим анти-DDoS, и набор на нодах должен обновиться следом."""
        if listener not in self._allowlist_listeners:
            self._allowlist_listeners.append(listener)

    async def push_allowlist_if_changed(self) -> None:
        epoch = self._allowlist_epoch
        policy = await self.build_allow_policy()
        digest = policy.digest()
        if digest == self._allowlist_pushed_hash:
            return
        for listener in self._allowlist_listeners:
            listener()
        # Адрес ноды мог смениться без правок в панели — в общих списках
        # полного синка остался бы прежний белый список
        self.invalidate_shared_lists()
        self._allowlist_inflight_hash = digest
        try:
            await self._push_allowlist(policy)
        finally:
            self._allowlist_inflight_hash = None
        if epoch == self._allowlist_epoch:
            self._allowlist_pushed_hash = digest

    async def _push_allowlist(self, policy: AllowPolicy) -> None:
        """Разослать на все активные ноды только белый список и настройку ping.

        Белый список — сотни адресов, блок-лист — до миллиона: гонять весь
        блок-лист по парку ради одного нового IP ноды незачем."""
        async with async_session() as db:
            servers = list((await db.execute(
                select(Server).where(Server.is_active == True)  # noqa: E712
            )).scalars().all())
        servers, _ = self._split_denied(servers)
        online = [s for s in servers if is_server_online(s)]
        offline = [s.id for s in servers if not is_server_online(s)]

        # Долг офлайн-ноды закрывает полный синк блок-листа: он соберёт
        # белый список, актуальный на момент её возвращения
        if offline:
            self._forget_delivered(offline)
            await enqueue(offline, KIND_BLOCKLIST, QUEUED_MESSAGE)

        done = await asyncio.gather(*(self._push_allow_one(s, policy) for s in online))
        failed = [server_id for server_id, ok in done if not ok]
        if failed:
            await enqueue(failed, KIND_BLOCKLIST, ALLOW_FAILED_MESSAGE)

        logger.info(
            f"Allowlist pushed (in={len(policy.ips['in'])}, out={len(policy.ips['out'])}, "
            f"ping blocked on {len(policy.ping_blocked_ids)}): "
            f"{len(online) - len(failed)} ok, {len(failed)} failed, {len(offline)} queued"
        )

    async def _push_allow_one(self, server: Server, policy: AllowPolicy) -> tuple[int, bool]:
        # Очередь к ноде (_node_locks) рассылка не занимает: ждать, пока нода
        # применит блок-лист, ей незачем, а снимок полного синка, лёгший
        # поверх свежего списка, закрывает _note_allow_sent
        async with self._allow_http_sem:
            known = self._delivered[server.id]
            # Пока нода не подтвердила новый белый список, что на ней лежит —
            # неизвестно: после сбоя долг из очереди отправит его заново
            known.allow.clear()
            try:
                ok = await asyncio.wait_for(
                    self._send_allow(server, policy), timeout=ALLOW_PUSH_BUDGET
                )
            except asyncio.TimeoutError:
                logger.warning(f"Allowlist push to {server.name} timed out")
                ok = False
            if ok:
                known.allow.update(policy.list_digests)
        return server.id, ok

    async def _send_allow(self, server: Server, policy: AllowPolicy) -> bool:
        for direction in DIRECTIONS:
            ok, message, _ = await self.sync_allow_to_node(
                server, policy.ips[direction], direction=direction
            )
            if not ok:
                logger.warning(f"Allowlist {direction} to {server.name} failed: {message}")
                return False
        # Правило ping ссылается на белый список — после него
        ok, message = await self.sync_ping_to_node(server, policy.blocks_ping(server))
        if not ok:
            logger.warning(f"Ping block to {server.name} failed: {message}")
        return ok

    async def _allowlist_loop(self) -> None:
        await asyncio.sleep(ALLOWLIST_START_DELAY)
        while self._running:
            try:
                await asyncio.wait_for(
                    self._allowlist_requested.wait(), timeout=ALLOWLIST_CHECK_INTERVAL
                )
                await asyncio.sleep(ALLOWLIST_PUSH_DEBOUNCE)
            except asyncio.TimeoutError:
                pass
            self._allowlist_requested.clear()
            try:
                await self.push_allowlist_if_changed()
            except Exception as e:
                logger.error(f"Allowlist push failed: {e}")

    def _source_changed(self) -> None:
        """Источник в синке стал другим: собранные списки устарели, а парку
        причитается полный синк — даже если тот, кто скачал источник, до синка
        не дойдёт (обновление источников оборвалось на следующем)."""
        self.invalidate_shared_lists()
        self._fleet_sync_owed = True

    async def refresh_source(self, source_id: int) -> tuple[bool, str, int, bool]:
        async with async_session() as db:
            result = await db.execute(
                select(BlocklistSource).where(BlocklistSource.id == source_id)
            )
            source = result.scalar_one_or_none()
            if not source:
                return False, "Source not found", 0, False
            
            success, ips, error = await self.fetch_github_list(source.url, use_cache=False)
            # Источник, не помещавшийся в лимит ноды, в синк не шёл, а его хэш
            # всё это время не обновлялся — о возврате в синк хэш не скажет
            was_excluded = (source.ip_count or 0) > NODE_MAX_IPSET_ENTRIES
            if success and len(ips) > NODE_MAX_IPSET_ENTRIES:
                source.ip_count = len(ips)
                source.last_updated = datetime.now(timezone.utc)
                source.error_message = (
                    f"{len(ips)} IPs exceeds node limit of {NODE_MAX_IPSET_ENTRIES:,} — excluded from sync"
                )
                await db.commit()
                if not was_excluded:
                    self._source_changed()
                return False, source.error_message, len(ips), False
            if success:
                # Хэш миллионов записей — CPU-bound, в поток
                new_hash = await asyncio.to_thread(self.calculate_hash, ips)
                changed = was_excluded or source.last_hash != new_hash
                source.last_hash = new_hash
                source.last_updated = datetime.now(timezone.utc)
                source.ip_count = len(ips)
                source.error_message = None
                await db.commit()
                if changed:
                    self._source_changed()
                    return True, f"Updated: {len(ips)} IPs (changed)", len(ips), True
                return True, f"Checked: {len(ips)} IPs (no changes)", len(ips), False
            else:
                source.error_message = error
                await db.commit()
                return False, error, 0, False
    
    async def refresh_all_sources(self) -> tuple[dict, bool]:
        results = {}
        any_changed = False
        
        async with async_session() as db:
            result = await db.execute(
                select(BlocklistSource).where(BlocklistSource.enabled == True)
            )
            sources = result.scalars().all()
            for source in sources:
                success, message, ip_count, changed = await self.refresh_source(source.id)
                results[source.id] = {
                    "name": source.name,
                    "success": success,
                    "message": message,
                    "ip_count": ip_count,
                    "changed": changed
                }
                if changed:
                    any_changed = True
        
        return results, any_changed
    
    async def init_default_sources(self):
        async with async_session() as db:
            for source_data in DEFAULT_SOURCES:
                result = await db.execute(
                    select(BlocklistSource).where(BlocklistSource.url == source_data["url"])
                )
                existing = result.scalar_one_or_none()
                if not existing:
                    source = BlocklistSource(
                        name=source_data["name"],
                        url=source_data["url"],
                        enabled=True,
                        is_default=source_data.get("is_default", False),
                        direction=source_data.get("direction", "in"),
                    )
                    db.add(source)
                    logger.info(f"Added default source: {source_data['name']}")
            await db.commit()
    
    async def _refresh_and_sync(self) -> None:
        """Проход автообновления: скачать источники и обойти парк, если нодам
        есть что досылать. Обход без повода на 2000 нодах — тысячи запросов
        ради списков, которые они уже приняли."""
        logger.info("Starting auto-update of blocklist sources")
        results, sources_changed = await self.refresh_all_sources()

        for r in results.values():
            if r.get("changed"):
                logger.info(f"Source '{r['name']}' changed: {r['ip_count']} IPs")
            elif r.get("success"):
                logger.debug(f"Source '{r['name']}' unchanged: {r['ip_count']} IPs")
            else:
                logger.warning(f"Source '{r['name']}' failed: {r['message']}")

        if not (sources_changed or self._fleet_sync_owed or await self._online_node_awaits_lists()):
            logger.info("Blocklist sources unchanged, nodes already have the lists — sync skipped")
            return

        # refresh_all_sources уже положил свежие списки в кэш —
        # прежний clear_cache() заставлял скачивать миллионы IP второй раз
        logger.info("Syncing blocklists to all nodes")
        await self.sync_all_nodes()

    async def _online_node_awaits_lists(self) -> bool:
        """Есть ли живая нода, про которую неизвестно, что на ней лежит: новая,
        не принявшая прошлую доставку, либо панель перезапускалась."""
        async with async_session() as db:
            servers = (await db.execute(
                select(Server.id, Server.last_seen, Server.node_capabilities)
                .where(Server.is_active == True)  # noqa: E712
            )).all()
        return any(
            server.id not in self._delivered or not self._delivered[server.id].complete()
            for server in servers
            if server_allows(server, Capability.IPSET, write=True) and is_server_online(server)
        )

    async def _update_loop(self):
        await asyncio.sleep(60)
        
        while self._running:
            try:
                async with async_session() as db:
                    settings = await self.get_blocklist_settings(db)
                
                if not settings.get("auto_update_enabled", True):
                    await asyncio.sleep(3600)
                    continue
                
                interval = settings.get("auto_update_interval", UPDATE_INTERVAL)

                await self._refresh_and_sync()

                await asyncio.sleep(interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in blocklist update loop: {e}")
                await asyncio.sleep(3600)
    
    async def start(self):
        if self._running:
            return
        self._running = True
        await self.init_default_sources()
        self._update_task = asyncio.create_task(self._update_loop())
        self._allowlist_task = asyncio.create_task(self._allowlist_loop())
        logger.info("BlocklistManager started")

    async def stop(self):
        self._running = False
        for task in (self._update_task, self._allowlist_task):
            if not task:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("BlocklistManager stopped")


_manager: Optional[BlocklistManager] = None


def get_blocklist_manager() -> BlocklistManager:
    global _manager
    if _manager is None:
        _manager = BlocklistManager()
    return _manager
