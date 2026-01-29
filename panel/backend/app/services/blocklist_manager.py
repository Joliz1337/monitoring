"""Blocklist manager for IP/CIDR blocking

Handles:
- GitHub list fetching and parsing
- Deduplication and validation
- Syncing to nodes via API
"""

import asyncio
import hashlib
import ipaddress
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import Server, BlocklistRule, BlocklistSource, PanelSettings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300
UPDATE_INTERVAL = 86400  # 24 hours

# Default GitHub lists
DEFAULT_SOURCES = [
    {
        "name": "AntiScanner",
        "url": "https://raw.githubusercontent.com/shadow-netlab/traffic-guard-lists/refs/heads/main/public/antiscanner.list",
        "is_default": True
    },
    {
        "name": "Government Networks",
        "url": "https://raw.githubusercontent.com/shadow-netlab/traffic-guard-lists/refs/heads/main/public/government_networks.list",
        "is_default": True
    }
]


class BlocklistManager:
    """Manager for IP/CIDR blocklists"""
    
    def __init__(self):
        self._running = False
        self._update_task: Optional[asyncio.Task] = None
    
    def _validate_ip_cidr(self, ip: str) -> bool:
        """Validate IP address or CIDR notation"""
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
        """Normalize IP/CIDR format"""
        ip = ip.strip()
        
        try:
            if '/' in ip:
                network = ipaddress.ip_network(ip, strict=False)
                # For IPv4, remove /32 suffix
                if network.version == 4 and network.prefixlen == 32:
                    return str(network.network_address)
                return str(network)
            else:
                return str(ipaddress.ip_address(ip))
        except ValueError:
            return ip
    
    def deduplicate_ips(self, ips: list[str]) -> list[str]:
        """Remove duplicates and normalize IPs"""
        seen = set()
        result = []
        
        for ip in ips:
            normalized = self._normalize_ip(ip)
            if normalized and normalized not in seen and self._validate_ip_cidr(normalized):
                seen.add(normalized)
                result.append(normalized)
        
        return result
    
    def parse_list_content(self, content: str) -> list[str]:
        """Parse blocklist content, removing comments and empty lines"""
        ips = []
        
        for line in content.split('\n'):
            line = line.strip()
            
            # Skip empty lines
            if not line:
                continue
            
            # Skip comments
            if line.startswith('#'):
                continue
            
            # Remove inline comments
            if '#' in line:
                line = line.split('#')[0].strip()
            
            if not line:
                continue
            
            # Validate and normalize
            if self._validate_ip_cidr(line):
                ips.append(self._normalize_ip(line))
        
        return ips
    
    async def fetch_github_list(self, url: str, timeout: float = 30.0) -> tuple[bool, list[str], str]:
        """Fetch and parse GitHub blocklist
        
        Returns: (success, ips, error_message)
        """
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.get(url)
                
                if response.status_code != 200:
                    return False, [], f"HTTP {response.status_code}"
                
                content = response.text
                ips = self.parse_list_content(content)
                
                return True, ips, ""
                
        except httpx.TimeoutException:
            return False, [], "Timeout"
        except httpx.RequestError as e:
            return False, [], f"Request error: {str(e)}"
        except Exception as e:
            logger.error(f"Failed to fetch {url}: {e}")
            return False, [], str(e)
    
    def calculate_hash(self, ips: list[str]) -> str:
        """Calculate SHA256 hash of sorted IP list"""
        sorted_ips = sorted(set(ips))
        content = '\n'.join(sorted_ips)
        return hashlib.sha256(content.encode()).hexdigest()
    
    async def check_list_updated(self, source: BlocklistSource) -> tuple[bool, list[str]]:
        """Check if GitHub list has been updated
        
        Returns: (has_changed, new_ips)
        """
        success, ips, error = await self.fetch_github_list(source.url)
        
        if not success:
            logger.warning(f"Failed to fetch {source.name}: {error}")
            return False, []
        
        new_hash = self.calculate_hash(ips)
        
        if source.last_hash and source.last_hash == new_hash:
            return False, ips  # Not changed but return IPs for sync
        
        return True, ips
    
    async def get_setting(self, key: str, db: AsyncSession) -> Optional[str]:
        """Get panel setting value"""
        result = await db.execute(
            select(PanelSettings).where(PanelSettings.key == key)
        )
        setting = result.scalar_one_or_none()
        return setting.value if setting else None
    
    async def get_blocklist_settings(self, db: AsyncSession) -> dict:
        """Get blocklist-related settings"""
        settings = {}
        
        timeout = await self.get_setting("blocklist_temp_timeout", db)
        settings["temp_timeout"] = int(timeout) if timeout else DEFAULT_TIMEOUT
        
        auto_update = await self.get_setting("blocklist_auto_update_enabled", db)
        settings["auto_update_enabled"] = auto_update != "false" if auto_update else True
        
        interval = await self.get_setting("blocklist_auto_update_interval", db)
        settings["auto_update_interval"] = int(interval) if interval else UPDATE_INTERVAL
        
        return settings
    
    async def get_global_rules(self, db: AsyncSession) -> list[str]:
        """Get all global blocklist rules (server_id is NULL)"""
        result = await db.execute(
            select(BlocklistRule).where(
                and_(
                    BlocklistRule.server_id.is_(None),
                    BlocklistRule.is_permanent == True
                )
            )
        )
        rules = result.scalars().all()
        return [r.ip_cidr for r in rules]
    
    async def get_server_rules(self, server_id: int, db: AsyncSession) -> list[str]:
        """Get blocklist rules for specific server"""
        result = await db.execute(
            select(BlocklistRule).where(
                and_(
                    BlocklistRule.server_id == server_id,
                    BlocklistRule.is_permanent == True
                )
            )
        )
        rules = result.scalars().all()
        return [r.ip_cidr for r in rules]
    
    async def get_auto_list_ips(self, db: AsyncSession) -> list[str]:
        """Get IPs from enabled auto-lists (BlocklistSource)"""
        result = await db.execute(
            select(BlocklistSource).where(BlocklistSource.enabled == True)
        )
        sources = result.scalars().all()
        
        all_ips = []
        for source in sources:
            # Fetch current IPs from each source
            success, ips, error = await self.fetch_github_list(source.url)
            if success:
                all_ips.extend(ips)
        
        return all_ips
    
    async def get_combined_ips_for_server(self, server_id: int, db: AsyncSession) -> list[str]:
        """Get combined and deduplicated IPs for a server"""
        # Global manual rules
        global_ips = await self.get_global_rules(db)
        
        # Server-specific rules
        server_ips = await self.get_server_rules(server_id, db)
        
        # Auto-list IPs
        auto_ips = await self.get_auto_list_ips(db)
        
        # Combine and deduplicate
        all_ips = global_ips + server_ips + auto_ips
        return self.deduplicate_ips(all_ips)
    
    async def sync_to_node(
        self, 
        server: Server, 
        ips: list[str], 
        permanent: bool = True,
        timeout: float = 60.0
    ) -> tuple[bool, str, dict]:
        """Send IP list to node via API
        
        Returns: (success, message, result)
        """
        try:
            async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
                response = await client.post(
                    f"{server.url}/api/ipset/sync",
                    headers={"X-API-Key": server.api_key},
                    json={"ips": ips, "permanent": permanent}
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
    
    async def sync_all_nodes(self) -> dict:
        """Sync blocklists to all active nodes
        
        Returns: dict with results per server
        """
        results = {}
        
        async with async_session() as db:
            # Get all active servers
            result = await db.execute(
                select(Server).where(Server.is_active == True)
            )
            servers = result.scalars().all()
            
            for server in servers:
                try:
                    # Get combined IPs for this server
                    ips = await self.get_combined_ips_for_server(server.id, db)
                    
                    # Sync to node
                    success, message, data = await self.sync_to_node(server, ips)
                    
                    results[server.id] = {
                        "server_name": server.name,
                        "success": success,
                        "message": message,
                        "ip_count": len(ips),
                        "added": data.get("added", 0),
                        "removed": data.get("removed", 0)
                    }
                    
                except Exception as e:
                    logger.error(f"Failed to sync to {server.name}: {e}")
                    results[server.id] = {
                        "server_name": server.name,
                        "success": False,
                        "message": str(e),
                        "ip_count": 0
                    }
        
        return results
    
    async def refresh_source(self, source_id: int) -> tuple[bool, str, int]:
        """Refresh single source from GitHub
        
        Returns: (success, message, ip_count)
        """
        async with async_session() as db:
            result = await db.execute(
                select(BlocklistSource).where(BlocklistSource.id == source_id)
            )
            source = result.scalar_one_or_none()
            
            if not source:
                return False, "Source not found", 0
            
            success, ips, error = await self.fetch_github_list(source.url)
            
            if success:
                new_hash = self.calculate_hash(ips)
                source.last_hash = new_hash
                source.last_updated = datetime.now(timezone.utc)
                source.ip_count = len(ips)
                source.error_message = None
                await db.commit()
                
                return True, f"Updated: {len(ips)} IPs", len(ips)
            else:
                source.error_message = error
                await db.commit()
                return False, error, 0
    
    async def refresh_all_sources(self) -> dict:
        """Refresh all enabled sources"""
        results = {}
        
        async with async_session() as db:
            result = await db.execute(
                select(BlocklistSource).where(BlocklistSource.enabled == True)
            )
            sources = result.scalars().all()
            
            for source in sources:
                success, message, ip_count = await self.refresh_source(source.id)
                results[source.id] = {
                    "name": source.name,
                    "success": success,
                    "message": message,
                    "ip_count": ip_count
                }
        
        return results
    
    async def init_default_sources(self):
        """Initialize default blocklist sources if not exist"""
        async with async_session() as db:
            for source_data in DEFAULT_SOURCES:
                # Check if already exists
                result = await db.execute(
                    select(BlocklistSource).where(BlocklistSource.url == source_data["url"])
                )
                existing = result.scalar_one_or_none()
                
                if not existing:
                    source = BlocklistSource(
                        name=source_data["name"],
                        url=source_data["url"],
                        enabled=True,
                        is_default=source_data.get("is_default", False)
                    )
                    db.add(source)
                    logger.info(f"Added default source: {source_data['name']}")
            
            await db.commit()
    
    async def _update_loop(self):
        """Background loop for auto-updating lists"""
        while self._running:
            try:
                async with async_session() as db:
                    settings = await self.get_blocklist_settings(db)
                
                if not settings.get("auto_update_enabled", True):
                    await asyncio.sleep(3600)  # Check again in 1 hour
                    continue
                
                interval = settings.get("auto_update_interval", UPDATE_INTERVAL)
                
                # Refresh all sources
                logger.info("Starting auto-update of blocklist sources")
                results = await self.refresh_all_sources()
                
                # Check if any source was updated
                any_updated = any(r.get("success") for r in results.values())
                
                if any_updated:
                    # Sync to all nodes
                    logger.info("Syncing updated blocklists to nodes")
                    await self.sync_all_nodes()
                
                await asyncio.sleep(interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in blocklist update loop: {e}")
                await asyncio.sleep(3600)  # Wait 1 hour on error
    
    async def start(self):
        """Start background update task"""
        if self._running:
            return
        
        self._running = True
        
        # Initialize default sources
        await self.init_default_sources()
        
        # Start update loop
        self._update_task = asyncio.create_task(self._update_loop())
        logger.info("BlocklistManager started")
    
    async def stop(self):
        """Stop background update task"""
        self._running = False
        
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
        
        logger.info("BlocklistManager stopped")


# Singleton instance
_manager: Optional[BlocklistManager] = None


def get_blocklist_manager() -> BlocklistManager:
    """Get or create BlocklistManager instance"""
    global _manager
    if _manager is None:
        _manager = BlocklistManager()
    return _manager
