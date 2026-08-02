"""Blocklist manager for IP/CIDR blocking

Handles:
- GitHub list fetching and parsing
- Deduplication and validation
- Syncing to nodes via API (both incoming and outgoing directions)
"""

import asyncio
import hashlib
import ipaddress
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select, and_
from urllib.parse import urlparse

from app.services.http_client import get_node_client, get_external_client, node_auth_headers
from app.services.net_utils import is_public_range, resolve_panel_ip, host_to_ip
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
SYNC_TIMEOUT_IPS_PER_SEC = 40_000  # +1 сек к таймауту синка на каждые 40k IP

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


class BlocklistManager:
    # Fan-out по нодам ограничен семафорами, поэтому нагрузка на БД и общий HTTP-пул
    # остаётся постоянной независимо от числа серверов — сервера синкаются волнами.
    DB_CONCURRENCY = 10    # max parallel DB sessions during fan-out sync
    HTTP_CONCURRENCY = 50  # max parallel servers syncing over HTTP at once

    def __init__(self):
        self._running = False
        self._update_task: Optional[asyncio.Task] = None
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._last_sync: Optional[dict] = None
        self._sync_in_progress = False
        self._db_sem = asyncio.Semaphore(self.DB_CONCURRENCY)
        self._http_sem = asyncio.Semaphore(self.HTTP_CONCURRENCY)
        self._sync_lock = asyncio.Lock()
        # Отдельные JSON-тела нужны только нодам со своими правилами; при списках
        # в миллионы IP каждое тело ~70 МБ — больше двух одновременно не собираем
        self._extra_body_sem = asyncio.Semaphore(2)
    
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
        timeout = await self.get_setting("blocklist_temp_timeout", db)
        settings["temp_timeout"] = int(timeout) if timeout else DEFAULT_TIMEOUT
        auto_update = await self.get_setting("blocklist_auto_update_enabled", db)
        settings["auto_update_enabled"] = auto_update != "false" if auto_update else True
        interval = await self.get_setting("blocklist_auto_update_interval", db)
        settings["auto_update_interval"] = int(interval) if interval else UPDATE_INTERVAL
        return settings
    
    async def get_global_rules(
        self, db: AsyncSession, direction: str = "in", list_type: str = "block"
    ) -> list[str]:
        result = await db.execute(
            select(BlocklistRule).where(
                and_(
                    BlocklistRule.server_id.is_(None),
                    BlocklistRule.is_permanent == True,
                    BlocklistRule.direction == direction,
                    BlocklistRule.list_type == list_type
                )
            )
        )
        rules = result.scalars().all()
        return [r.ip_cidr for r in rules]

    async def get_allow_ips_global(self, db: AsyncSession, direction: str = "in") -> list[str]:
        """Белый список для синка: ручные allow-правила + авто (IP панели и всех нод).

        IP панели и нод всегда в allowlist — ACCEPT стоит первым в цепочке,
        поэтому управляющий трафик не попадёт под DROP даже при плохом блок-листе."""
        ips = set(await self.get_global_rules(db, direction, list_type="allow"))

        servers = (await db.execute(
            select(Server).where(Server.is_active == True)
        )).scalars().all()
        for srv in servers:
            ip = host_to_ip(urlparse(srv.url).hostname or "")
            if ip:
                ips.add(ip)

        panel_ip = resolve_panel_ip()
        if panel_ip:
            ips.add(panel_ip)

        return self.deduplicate_ips(sorted(ips))
    
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
        result = await db.execute(
            select(BlocklistSource).where(
                and_(
                    BlocklistSource.enabled == True,
                    BlocklistSource.direction == direction
                )
            )
        )
        sources = result.scalars().all()
        all_ips = []
        for source in sources:
            success, ips, error = await self.fetch_github_list(source.url)
            if not success:
                continue
            if len(ips) > NODE_MAX_IPSET_ENTRIES:
                logger.warning(
                    f"Source '{source.name}' skipped: {len(ips)} IPs exceeds "
                    f"node ipset limit ({NODE_MAX_IPSET_ENTRIES})"
                )
                continue
            all_ips.extend(ips)
        return all_ips
    
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
        shared = {}
        async with async_session() as db:
            for direction in ("in", "out"):
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
                    "allow": await self.get_allow_ips_global(db, direction),
                }
        return shared
    
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

    async def _sync_one_server(self, server: Server, shared: dict) -> dict:
        """Sync both directions for a single server.

        `shared` — предвычисленные общие списки и JSON-тела из build_shared_lists();
        нода без своих правил получает общее тело как есть — ни одной копии списка.
        Короткая сессия БД под семафором освобождается до медленных HTTP-синков,
        иначе пул PostgreSQL исчерпывается при fan-out.
        """
        server_result = {
            "server_id": server.id,
            "server_name": server.name,
            "success": True,
            "in": {},
            "out": {},
        }

        try:
            extras = {}
            async with self._db_sem:
                async with async_session() as db:
                    for direction in ("in", "out"):
                        server_rules = await self.get_server_rules(server.id, db, direction)
                        extras[direction] = [
                            ip for ip in self.deduplicate_ips(server_rules)
                            if is_public_range(ip) and ip not in shared[direction]["block_set"]
                        ]
        except Exception as e:
            logger.error(f"Failed to load blocklist IPs for {server.name}: {e}")
            for direction in ("in", "out"):
                server_result[direction] = {"success": False, "message": str(e), "ip_count": 0}
            server_result["success"] = False
            return server_result

        async with self._http_sem:
            for direction in ("in", "out"):
                try:
                    extra = extras[direction]
                    ip_count = shared[direction]["count"] + len(extra)

                    if extra:
                        # У ноды свои правила — собираем отдельное тело; семафор
                        # держит его до конца отправки, чтобы гигантские тела
                        # не копились в памяти по числу таких нод
                        async with self._extra_body_sem:
                            body = await asyncio.to_thread(
                                self._build_sync_body,
                                shared[direction]["block"] + extra,
                                direction,
                            )
                            success, message, data = await self.sync_to_node(
                                server, body, ip_count
                            )
                            del body
                    else:
                        success, message, data = await self.sync_to_node(
                            server, shared[direction]["body"], ip_count
                        )

                    allow_ips = shared[direction]["allow"]
                    allow_ok, allow_msg, allow_data = await self.sync_allow_to_node(
                        server, allow_ips, direction=direction
                    )

                    server_result[direction] = {
                        "success": success and allow_ok,
                        "message": message,
                        "ip_count": ip_count,
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
        return server_result

    async def _sync_one_server_safe(self, server: Server, shared: dict) -> dict:
        """Sync one server with a global timeout wrapper — never raises."""
        # Бюджет масштабируется под размер списков: фиксированные 30с обрубали
        # синк крупных блок-листов на середине
        budget = 10.0 + sum(
            self._sync_timeout(shared[d]["count"]) + ALLOW_SYNC_TIMEOUT
            for d in ("in", "out")
        )
        try:
            return await asyncio.wait_for(
                self._sync_one_server(server, shared), timeout=budget
            )
        except asyncio.TimeoutError:
            return {
                "server_id": server.id,
                "server_name": server.name,
                "success": False,
                "in": {"success": False, "message": "Timeout", "ip_count": 0},
                "out": {"success": False, "message": "Timeout", "ip_count": 0},
            }
        except Exception as e:
            return {
                "server_id": server.id,
                "server_name": server.name,
                "success": False,
                "in": {"success": False, "message": str(e), "ip_count": 0},
                "out": {"success": False, "message": str(e), "ip_count": 0},
            }

    def _store_sync_result(self, results: dict):
        self._last_sync = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "servers": results,
            "in_progress": False,
        }
        self._sync_in_progress = False

    def get_sync_status(self) -> dict:
        if self._sync_in_progress:
            return {"in_progress": True, "timestamp": None, "servers": {}}
        if self._last_sync:
            return self._last_sync
        return {"in_progress": False, "timestamp": None, "servers": {}}

    async def sync_all_nodes(self) -> dict:
        """Sync blocklists to all active nodes in parallel (both directions).

        Под замком: параллельные полные синки (ручной + автообновление) не
        дублируют работу, второй вызов дождётся первого и прогонит свежие данные."""
        async with self._sync_lock:
            self._sync_in_progress = True
            results = {}
            try:
                async with async_session() as db:
                    result = await db.execute(
                        select(Server).where(Server.is_active == True)
                    )
                    servers = result.scalars().all()

                if not servers:
                    self._store_sync_result({})
                    return {}

                shared = await self.build_shared_lists()
                tasks = [self._sync_one_server_safe(s, shared) for s in servers]
                done = await asyncio.gather(*tasks)

                for sr in done:
                    results[sr["server_id"]] = sr
            except Exception as e:
                logger.error(f"sync_all_nodes failed: {e}")
            finally:
                self._store_sync_result(results)
            return results

    async def sync_single_node_by_id(self, server_id: int) -> dict:
        """Sync one server by ID (both directions). Returns per-server result."""
        self._sync_in_progress = True
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(Server).where(Server.id == server_id)
                )
                server = result.scalar_one_or_none()

            if not server:
                self._sync_in_progress = False
                return {}

            shared = await self.build_shared_lists()
            sr = await self._sync_one_server_safe(server, shared)
            prev = self._last_sync.get("servers", {}) if self._last_sync else {}
            prev[sr["server_id"]] = sr
            self._store_sync_result(prev)
            return sr
        except Exception as e:
            logger.error(f"sync_single_node_by_id failed: {e}")
            self._sync_in_progress = False
            return {}
    
    async def refresh_source(self, source_id: int) -> tuple[bool, str, int, bool]:
        async with async_session() as db:
            result = await db.execute(
                select(BlocklistSource).where(BlocklistSource.id == source_id)
            )
            source = result.scalar_one_or_none()
            if not source:
                return False, "Source not found", 0, False
            
            success, ips, error = await self.fetch_github_list(source.url, use_cache=False)
            if success and len(ips) > NODE_MAX_IPSET_ENTRIES:
                source.ip_count = len(ips)
                source.last_updated = datetime.now(timezone.utc)
                source.error_message = (
                    f"{len(ips)} IPs exceeds node limit of {NODE_MAX_IPSET_ENTRIES:,} — excluded from sync"
                )
                await db.commit()
                return False, source.error_message, len(ips), False
            if success:
                # Хэш миллионов записей — CPU-bound, в поток
                new_hash = await asyncio.to_thread(self.calculate_hash, ips)
                changed = source.last_hash != new_hash
                source.last_hash = new_hash
                source.last_updated = datetime.now(timezone.utc)
                source.ip_count = len(ips)
                source.error_message = None
                await db.commit()
                if changed:
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
                
                logger.info("Starting auto-update of blocklist sources")
                results, _ = await self.refresh_all_sources()
                
                for source_id, r in results.items():
                    if r.get("changed"):
                        logger.info(f"Source '{r['name']}' changed: {r['ip_count']} IPs")
                    elif r.get("success"):
                        logger.debug(f"Source '{r['name']}' unchanged: {r['ip_count']} IPs")
                    else:
                        logger.warning(f"Source '{r['name']}' failed: {r['message']}")
                
                # refresh_all_sources уже положил свежие списки в кэш —
                # прежний clear_cache() заставлял скачивать миллионы IP второй раз
                logger.info("Syncing blocklists to all nodes")
                await self.sync_all_nodes()
                
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
        logger.info("BlocklistManager started")
    
    async def stop(self):
        self._running = False
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
        logger.info("BlocklistManager stopped")


_manager: Optional[BlocklistManager] = None


def get_blocklist_manager() -> BlocklistManager:
    global _manager
    if _manager is None:
        _manager = BlocklistManager()
    return _manager
