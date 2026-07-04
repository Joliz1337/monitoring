"""Blocklist manager for IP/CIDR blocking

Handles:
- GitHub list fetching and parsing
- Deduplication and validation
- Syncing to nodes via API (both incoming and outgoing directions)
"""

import asyncio
import hashlib
import ipaddress
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
        return None
    
    def _set_cache(self, url: str, ips: list[str]):
        self._cache[url] = (time.monotonic(), ips)
    
    def clear_cache(self):
        self._cache.clear()
    
    async def fetch_github_list(
        self, url: str, timeout: float = 30.0, use_cache: bool = True
    ) -> tuple[bool, list[str], str]:
        if use_cache:
            cached = self._get_cached(url)
            if cached is not None:
                return True, cached, ""
        try:
            client = get_external_client()
            response = await client.get(url, timeout=timeout)
            if response.status_code != 200:
                return False, [], f"HTTP {response.status_code}"
            content = response.text
            ips = self.parse_list_content(content)
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
        sorted_ips = sorted(set(ips))
        content = '\n'.join(sorted_ips)
        return hashlib.sha256(content.encode()).hexdigest()
    
    async def check_list_updated(self, source: BlocklistSource) -> tuple[bool, list[str]]:
        success, ips, error = await self.fetch_github_list(source.url)
        if not success:
            logger.warning(f"Failed to fetch {source.name}: {error}")
            return False, []
        new_hash = self.calculate_hash(ips)
        if source.last_hash and source.last_hash == new_hash:
            return False, ips
        return True, ips
    
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
            if success:
                all_ips.extend(ips)
        return all_ips
    
    async def get_combined_ips_for_server(
        self, server_id: int, db: AsyncSession, direction: str = "in"
    ) -> list[str]:
        global_ips = await self.get_global_rules(db, direction)
        server_ips = await self.get_server_rules(server_id, db, direction)
        auto_ips = await self.get_auto_list_ips(db, direction)
        # auto_ips уже отфильтрованы при парсинге; ручные правила (в т.ч. старые
        # записи в БД) дополнительно чистятся от приватных диапазонов здесь
        manual_ips = [
            ip for ip in global_ips + server_ips
            if is_public_range(self._normalize_ip(ip))
        ]
        all_ips = manual_ips + auto_ips
        return self.deduplicate_ips(all_ips)
    
    async def sync_to_node(
        self,
        server: Server,
        ips: list[str],
        permanent: bool = True,
        direction: str = "in",
        timeout: float = 20.0
    ) -> tuple[bool, str, dict]:
        try:
            client = get_node_client(server)
            response = await client.post(
                f"{server.url}/api/ipset/sync",
                headers=node_auth_headers(server),
                json={"ips": ips, "permanent": permanent, "direction": direction},
                timeout=timeout,
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
        timeout: float = 20.0
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

    async def _sync_one_server(self, server: Server) -> dict:
        """Sync both directions for a single server.

        IP-списки читаются из БД в короткой сессии под семафором, после чего
        соединение освобождается — медленные HTTP-синки к ноде идут уже без
        удержания коннекта, иначе пул PostgreSQL исчерпывается при fan-out.
        """
        server_result = {
            "server_id": server.id,
            "server_name": server.name,
            "success": True,
            "in": {},
            "out": {},
        }

        try:
            ip_lists = {}
            async with self._db_sem:
                async with async_session() as db:
                    for direction in ("in", "out"):
                        ip_lists[direction] = {
                            "block": await self.get_combined_ips_for_server(server.id, db, direction),
                            "allow": await self.get_allow_ips_global(db, direction),
                        }
        except Exception as e:
            logger.error(f"Failed to load blocklist IPs for {server.name}: {e}")
            for direction in ("in", "out"):
                server_result[direction] = {"success": False, "message": str(e), "ip_count": 0}
            server_result["success"] = False
            return server_result

        async with self._http_sem:
            for direction in ("in", "out"):
                try:
                    ips = ip_lists[direction]["block"]
                    success, message, data = await self.sync_to_node(
                        server, ips, direction=direction
                    )

                    allow_ips = ip_lists[direction]["allow"]
                    allow_ok, allow_msg, allow_data = await self.sync_allow_to_node(
                        server, allow_ips, direction=direction
                    )

                    server_result[direction] = {
                        "success": success and allow_ok,
                        "message": message,
                        "ip_count": len(ips),
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

    async def _sync_one_server_safe(self, server: Server) -> dict:
        """Sync one server with a global timeout wrapper — never raises."""
        try:
            return await asyncio.wait_for(
                self._sync_one_server(server), timeout=30.0
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
        """Sync blocklists to all active nodes in parallel (both directions)."""
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

            tasks = [self._sync_one_server_safe(s) for s in servers]
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

            sr = await self._sync_one_server_safe(server)
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
            if success:
                new_hash = self.calculate_hash(ips)
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
                results, any_changed = await self.refresh_all_sources()
                
                for source_id, r in results.items():
                    if r.get("changed"):
                        logger.info(f"Source '{r['name']}' changed: {r['ip_count']} IPs")
                    elif r.get("success"):
                        logger.debug(f"Source '{r['name']}' unchanged: {r['ip_count']} IPs")
                    else:
                        logger.warning(f"Source '{r['name']}' failed: {r['message']}")
                
                if any_changed:
                    logger.info("Sources changed, clearing cache before sync")
                    self.clear_cache()
                
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
