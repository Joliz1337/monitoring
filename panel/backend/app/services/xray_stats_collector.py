"""
Xray stats collector for Remnawave integration.

Optimized version: stores cumulative counters instead of time-series data.
- XrayVisitStats: (server, destination, email) -> total_count (incremented)
- XrayHourlyStats: (server, hour) -> counts (for timeline charts)
"""

import asyncio
import json
import logging
import re
import socket
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError

from app.database import async_session
from app.models import (
    Server, RemnawaveSettings, RemnawaveNode, RemnawaveInfrastructureAddress,
    XrayVisitStats, XrayHourlyStats, RemnawaveUserCache, XrayUserIpStats,
    XrayIpDestinationStats
)
from app.services.remnawave_api import get_remnawave_api, RemnawaveAPIError

# Retry settings for database operations
DB_RETRY_ATTEMPTS = 5
DB_RETRY_DELAY = 0.5  # Initial delay in seconds
DB_RETRY_MAX_DELAY = 5.0  # Max delay between retries

logger = logging.getLogger(__name__)

# DNS cache for infrastructure addresses (address -> (resolved_ips, timestamp))
_dns_cache: dict[str, tuple[set[str], datetime]] = {}
_DNS_CACHE_TTL = timedelta(hours=1)

# Lock for serializing database writes (SQLite doesn't handle concurrent writes well)
_db_write_lock = asyncio.Lock()


async def retry_on_db_locked(coro_func, *args, **kwargs):
    """Retry database operation on lock errors with exponential backoff."""
    delay = DB_RETRY_DELAY
    last_error = None
    
    for attempt in range(DB_RETRY_ATTEMPTS):
        try:
            return await coro_func(*args, **kwargs)
        except OperationalError as e:
            if "database is locked" in str(e).lower():
                last_error = e
                if attempt < DB_RETRY_ATTEMPTS - 1:
                    logger.warning(f"Database locked, retry {attempt + 1}/{DB_RETRY_ATTEMPTS} in {delay:.1f}s")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, DB_RETRY_MAX_DELAY)
                else:
                    logger.error(f"Database locked after {DB_RETRY_ATTEMPTS} attempts")
                    raise
            else:
                raise
    
    if last_error:
        raise last_error


def extract_host_from_url(url: str) -> str:
    """Extract hostname or IP from URL."""
    try:
        parsed = urlparse(url)
        return parsed.hostname or ""
    except Exception:
        return ""


def is_valid_ip(address: str) -> bool:
    """Check if address is a valid IPv4 or IPv6 address."""
    try:
        socket.inet_pton(socket.AF_INET, address)
        return True
    except socket.error:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, address)
        return True
    except socket.error:
        pass
    return False


def resolve_domain_to_ips(domain: str) -> set[str]:
    """Resolve domain to all IP addresses (A and AAAA records).
    
    Returns set of IP addresses. If domain is already an IP, returns it as-is.
    """
    if is_valid_ip(domain):
        return {domain}
    
    ips = set()
    try:
        # Get all addresses (both IPv4 and IPv6)
        results = socket.getaddrinfo(domain, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for result in results:
            ip = result[4][0]
            ips.add(ip)
    except socket.gaierror as e:
        logger.debug(f"DNS resolution failed for {domain}: {e}")
    except Exception as e:
        logger.debug(f"Error resolving {domain}: {e}")
    
    return ips


async def resolve_infrastructure_address(address: str, use_cache: bool = True) -> set[str]:
    """Resolve infrastructure address (IP or domain) to IP addresses with caching."""
    global _dns_cache
    
    now = datetime.now(timezone.utc)
    
    # Check cache first
    if use_cache and address in _dns_cache:
        cached_ips, cached_time = _dns_cache[address]
        if now - cached_time < _DNS_CACHE_TTL:
            return cached_ips
    
    # Run DNS resolution in thread pool to avoid blocking
    loop = asyncio.get_event_loop()
    ips = await loop.run_in_executor(None, resolve_domain_to_ips, address)
    
    # Update cache
    if ips:
        _dns_cache[address] = (ips, now)
    
    return ips


class XrayStatsCollector:
    """Collects Xray visit stats from Remnawave nodes."""
    
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._user_cache_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        
        self._collection_interval = 300  # 5 minutes default
        self._user_cache_interval = 3600
        self._time_since_last_collect = 0  # Tracks seconds since last collection
        
        # Retention: hourly stats for timeline (365 days)
        self._hourly_retention_days = 365
        
        self._last_collect_time: Optional[datetime] = None
        self._collecting = False
    
    async def start(self):
        """Start background collection."""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._collection_loop())
        self._user_cache_task = asyncio.create_task(self._user_cache_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Xray stats collector started")
    
    async def stop(self):
        """Stop background collection."""
        self._running = False
        
        for task in [self._task, self._user_cache_task, self._cleanup_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        logger.info("Xray stats collector stopped")
    
    async def _get_settings(self) -> Optional[RemnawaveSettings]:
        """Get Remnawave settings from DB."""
        async with async_session() as db:
            result = await db.execute(select(RemnawaveSettings).limit(1))
            return result.scalar_one_or_none()
    
    async def _collection_loop(self):
        """Main collection loop with dynamic interval updates."""
        self._time_since_last_collect = 0
        
        while self._running:
            try:
                # Always fetch settings to get updated interval
                settings = await self._get_settings()
                
                if settings:
                    new_interval = settings.collection_interval or 60
                    if new_interval != self._collection_interval:
                        logger.info(f"Collection interval changed: {self._collection_interval}s -> {new_interval}s")
                        self._collection_interval = new_interval
                        # Reset timer if new interval is shorter than time already waited
                        if self._time_since_last_collect >= new_interval:
                            self._time_since_last_collect = new_interval
                
                # Check if it's time to collect
                if settings and settings.enabled and self._time_since_last_collect >= self._collection_interval:
                    await self._collect_from_all_nodes()
                    self._time_since_last_collect = 0
                
                # Sleep for 1 second and increment counter
                await asyncio.sleep(1)
                self._time_since_last_collect += 1
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Collection error: {e}")
                await asyncio.sleep(5)
                self._time_since_last_collect += 5
    
    async def _collect_from_all_nodes(self):
        """Collect stats from all enabled Remnawave nodes."""
        async with async_session() as db:
            result = await db.execute(
                select(RemnawaveNode, Server)
                .join(Server, RemnawaveNode.server_id == Server.id)
                .where(RemnawaveNode.enabled == True)
                .where(Server.is_active == True)
            )
            nodes = result.all()
        
        if not nodes:
            return
        
        self._collecting = True
        try:
            tasks = [self._collect_from_node(node, server) for node, server in nodes]
            await asyncio.gather(*tasks, return_exceptions=True)
            self._last_collect_time = datetime.now(timezone.utc).replace(tzinfo=None)
        finally:
            self._collecting = False
    
    async def collect_now(self) -> dict:
        """Force immediate collection from all nodes."""
        settings = await self._get_settings()
        
        if not settings or not settings.enabled:
            return {
                "success": False,
                "error": "Collection is disabled",
                "collected_at": None,
                "nodes_count": 0
            }
        
        if self._collecting:
            return {
                "success": False,
                "error": "Collection already in progress",
                "collected_at": None,
                "nodes_count": 0
            }
        
        async with async_session() as db:
            result = await db.execute(
                select(RemnawaveNode, Server)
                .join(Server, RemnawaveNode.server_id == Server.id)
                .where(RemnawaveNode.enabled == True)
                .where(Server.is_active == True)
            )
            nodes_count = len(result.all())
        
        await self._collect_from_all_nodes()
        
        # Reset timer after manual collection
        self._time_since_last_collect = 0
        
        return {
            "success": True,
            "collected_at": self._last_collect_time.isoformat() if self._last_collect_time else None,
            "nodes_count": nodes_count
        }
    
    def get_status(self) -> dict:
        """Get collector status with timing info."""
        next_collect_in = None
        if self._running:
            # Use tracked time counter for accurate countdown
            next_collect_in = max(0, self._collection_interval - self._time_since_last_collect)
        
        return {
            "running": self._running,
            "collecting": self._collecting,
            "collection_interval": self._collection_interval,
            "last_collect_time": self._last_collect_time.isoformat() if self._last_collect_time else None,
            "next_collect_in": next_collect_in
        }
    
    async def _collect_from_node(self, node: RemnawaveNode, server: Server):
        """Collect stats from a single node."""
        try:
            async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
                response = await client.post(
                    f"{server.url}/api/remnawave/stats/collect",
                    headers={"X-API-Key": server.api_key}
                )
                
                if response.status_code != 200:
                    raise Exception(f"HTTP {response.status_code}")
                
                data = response.json()
                stats = data.get("stats", [])
                ip_stats = data.get("ip_stats", [])
                ip_destination_stats = data.get("ip_destination_stats", [])
                
                if stats or ip_stats or ip_destination_stats:
                    await self._save_stats(server.id, stats, ip_stats, ip_destination_stats)
                
                await self._update_node_status(node.id, error=None)
                
                logger.debug(f"Collected {len(stats)} stat entries, {len(ip_stats)} IP entries from {server.name}")
                
        except Exception as e:
            error_msg = str(e)[:500]
            logger.debug(f"Failed to collect from {server.name}: {error_msg}")
            await self._update_node_status(node.id, error=error_msg)
    
    async def _update_node_status(self, node_id: int, error: str | None):
        """Update node status with retry on database lock."""
        async with _db_write_lock:
            for attempt in range(DB_RETRY_ATTEMPTS):
                try:
                    async with async_session() as db:
                        if error is None:
                            await db.execute(
                                update(RemnawaveNode)
                                .where(RemnawaveNode.id == node_id)
                                .values(
                                    last_collected=datetime.now(timezone.utc).replace(tzinfo=None),
                                    last_error=None
                                )
                            )
                        else:
                            await db.execute(
                                update(RemnawaveNode)
                                .where(RemnawaveNode.id == node_id)
                                .values(last_error=error)
                            )
                        await db.commit()
                        return
                except OperationalError as e:
                    if "database is locked" in str(e).lower() and attempt < DB_RETRY_ATTEMPTS - 1:
                        await asyncio.sleep(DB_RETRY_DELAY * (attempt + 1))
                    else:
                        logger.error(f"Failed to update node status: {e}")
    
    async def _get_node_ips(self) -> set[str]:
        """Get set of all server/node IP addresses (from server URLs)."""
        node_ips = set()
        async with async_session() as db:
            result = await db.execute(select(Server.url))
            for row in result.fetchall():
                url = row[0]
                if url:
                    host = extract_host_from_url(url)
                    if host:
                        node_ips.add(host)
        return node_ips
    
    async def _get_infrastructure_ips(self) -> set[str]:
        """Get set of all infrastructure IP addresses.
        
        Combines:
        - Server/node IPs (from server URLs)
        - Manually configured infrastructure addresses (with DNS resolution)
        
        Returns set of IP addresses that should be marked as infrastructure.
        """
        infrastructure_ips = set()
        
        # Add node IPs from server URLs
        node_ips = await self._get_node_ips()
        infrastructure_ips.update(node_ips)
        
        # Add manually configured infrastructure addresses
        async with async_session() as db:
            result = await db.execute(select(RemnawaveInfrastructureAddress))
            addresses = result.scalars().all()
            
            for addr in addresses:
                # Resolve address (uses cache)
                resolved = await resolve_infrastructure_address(addr.address)
                infrastructure_ips.update(resolved)
                
                # Update resolved_ips in DB if changed
                resolved_json = json.dumps(sorted(resolved)) if resolved else None
                if resolved_json != addr.resolved_ips:
                    addr.resolved_ips = resolved_json
                    addr.last_resolved = datetime.now(timezone.utc).replace(tzinfo=None)
            
            await db.commit()
        
        return infrastructure_ips
    
    async def _save_stats(self, server_id: int, stats: list[dict], ip_stats: list[dict] = None, ip_destination_stats: list[dict] = None):
        """Save collected stats to DB (increment counters)."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        
        if ip_stats is None:
            ip_stats = []
        if ip_destination_stats is None:
            ip_destination_stats = []
        
        # Aggregate stats for batch processing
        visit_updates: dict[tuple[str, int], int] = {}  # (destination, email) -> count
        total_count = 0
        unique_users = set()
        unique_destinations = set()
        
        for stat in stats:
            destination = stat.get("destination", "")
            email = stat.get("email", 0)
            count = stat.get("count", 0)
            
            if not destination or not email or not count:
                continue
            
            key = (destination, email)
            visit_updates[key] = visit_updates.get(key, 0) + count
            total_count += count
            unique_users.add(email)
            unique_destinations.add(destination)
        
        # Get infrastructure IPs (nodes, HAProxy servers, etc.)
        infrastructure_ips = await self._get_infrastructure_ips()
        
        # Aggregate IP stats (with infrastructure flag)
        # Key: (email, source_ip) -> (count, is_infrastructure)
        ip_updates: dict[tuple[int, str], tuple[int, bool]] = {}
        infra_ip_count = 0
        client_ip_count = 0
        for ip_stat in ip_stats:
            email = ip_stat.get("email", 0)
            source_ip = ip_stat.get("source_ip", "")
            count = ip_stat.get("count", 0)
            
            if not email or not source_ip or not count:
                continue
            
            is_infra = source_ip in infrastructure_ips
            if is_infra:
                infra_ip_count += count
            else:
                client_ip_count += count
            
            key = (email, source_ip)
            if key in ip_updates:
                existing_count, existing_infra = ip_updates[key]
                ip_updates[key] = (existing_count + count, is_infra)
            else:
                ip_updates[key] = (count, is_infra)
        
        # Aggregate IP-destination stats (skip infrastructure IPs for destinations)
        ip_dest_updates: dict[tuple[int, str, str], int] = {}  # (email, source_ip, destination) -> count
        for ip_dest_stat in ip_destination_stats:
            email = ip_dest_stat.get("email", 0)
            source_ip = ip_dest_stat.get("source_ip", "")
            destination = ip_dest_stat.get("destination", "")
            count = ip_dest_stat.get("count", 0)
            
            if not email or not source_ip or not destination or not count:
                continue
            
            # Skip IP-destination stats for infrastructure IPs (not useful for tracking)
            if source_ip in infrastructure_ips:
                continue
            
            key = (email, source_ip, destination)
            ip_dest_updates[key] = ip_dest_updates.get(key, 0) + count
        
        if infra_ip_count > 0:
            logger.debug(f"Recorded {infra_ip_count} connections from infrastructure IPs, {client_ip_count} from client IPs")
        
        if not visit_updates and not ip_updates and not ip_dest_updates:
            return
        
        # Use lock to serialize database writes (SQLite limitation)
        async with _db_write_lock:
            await self._save_stats_to_db(
                server_id, now, hour_start,
                visit_updates, ip_updates, ip_dest_updates,
                total_count, unique_users, unique_destinations
            )
        
        logger.debug(f"Saved {len(visit_updates)} stat entries, {len(ip_updates)} IP entries, {len(ip_dest_updates)} IP-destination entries, {total_count} total visits")
    
    async def _save_stats_to_db(
        self, server_id: int, now: datetime, hour_start: datetime,
        visit_updates: dict, ip_updates: dict, ip_dest_updates: dict,
        total_count: int, unique_users: set, unique_destinations: set
    ):
        """Internal method to save stats with retry logic."""
        delay = DB_RETRY_DELAY
        
        for attempt in range(DB_RETRY_ATTEMPTS):
            try:
                async with async_session() as db:
                    # Disable autoflush to prevent premature writes
                    with db.no_autoflush:
                        # Update visit counters (batch upsert)
                        for (destination, email), count in visit_updates.items():
                            existing = await db.execute(
                                select(XrayVisitStats).where(
                                    XrayVisitStats.server_id == server_id,
                                    XrayVisitStats.destination == destination,
                                    XrayVisitStats.email == email
                                )
                            )
                            row = existing.scalar_one_or_none()
                            
                            if row:
                                row.visit_count += count
                                row.last_seen = now
                            else:
                                db.add(XrayVisitStats(
                                    server_id=server_id,
                                    destination=destination,
                                    email=email,
                                    visit_count=count,
                                    first_seen=now,
                                    last_seen=now
                                ))
                        
                        # Update IP stats (upsert with is_infrastructure flag)
                        for (email, source_ip), (count, is_infra) in ip_updates.items():
                            existing = await db.execute(
                                select(XrayUserIpStats).where(
                                    XrayUserIpStats.server_id == server_id,
                                    XrayUserIpStats.email == email,
                                    XrayUserIpStats.source_ip == source_ip
                                )
                            )
                            row = existing.scalar_one_or_none()
                            
                            if row:
                                row.connection_count += count
                                row.last_seen = now
                                # Update infrastructure flag (may change if address was added/removed)
                                row.is_infrastructure = is_infra
                            else:
                                db.add(XrayUserIpStats(
                                    server_id=server_id,
                                    email=email,
                                    source_ip=source_ip,
                                    connection_count=count,
                                    is_infrastructure=is_infra,
                                    first_seen=now,
                                    last_seen=now
                                ))
                        
                        # Update IP-destination stats (upsert)
                        for (email, source_ip, destination), count in ip_dest_updates.items():
                            existing = await db.execute(
                                select(XrayIpDestinationStats).where(
                                    XrayIpDestinationStats.server_id == server_id,
                                    XrayIpDestinationStats.email == email,
                                    XrayIpDestinationStats.source_ip == source_ip,
                                    XrayIpDestinationStats.destination == destination
                                )
                            )
                            row = existing.scalar_one_or_none()
                            
                            if row:
                                row.connection_count += count
                                row.last_seen = now
                            else:
                                db.add(XrayIpDestinationStats(
                                    server_id=server_id,
                                    email=email,
                                    source_ip=source_ip,
                                    destination=destination,
                                    connection_count=count,
                                    first_seen=now,
                                    last_seen=now
                                ))
                        
                        # Update hourly stats for timeline
                        hourly_existing = await db.execute(
                            select(XrayHourlyStats).where(
                                XrayHourlyStats.server_id == server_id,
                                XrayHourlyStats.hour == hour_start
                            )
                        )
                        hourly_row = hourly_existing.scalar_one_or_none()
                        
                        if hourly_row:
                            hourly_row.visit_count += total_count
                            # Update unique counts (approximate - may count same user twice in hour)
                            hourly_row.unique_users = max(hourly_row.unique_users, len(unique_users))
                            hourly_row.unique_destinations = max(hourly_row.unique_destinations, len(unique_destinations))
                        else:
                            db.add(XrayHourlyStats(
                                server_id=server_id,
                                hour=hour_start,
                                visit_count=total_count,
                                unique_users=len(unique_users),
                                unique_destinations=len(unique_destinations)
                            ))
                    
                    # Commit outside no_autoflush block
                    await db.commit()
                    return  # Success, exit retry loop
                    
            except OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < DB_RETRY_ATTEMPTS - 1:
                    logger.warning(f"Database locked during save_stats, retry {attempt + 1}/{DB_RETRY_ATTEMPTS} in {delay:.1f}s")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, DB_RETRY_MAX_DELAY)
                else:
                    logger.error(f"Database error in save_stats: {e}")
                    raise
    
    async def _user_cache_loop(self):
        """Background loop for caching Remnawave users."""
        await asyncio.sleep(30)
        
        while self._running:
            try:
                await self._update_user_cache()
                await asyncio.sleep(self._user_cache_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"User cache error: {e}")
                await asyncio.sleep(300)
    
    async def _update_user_cache(self):
        """Update cached Remnawave users with full info."""
        settings = await self._get_settings()
        
        if not settings or not settings.enabled or not settings.api_url or not settings.api_token:
            return
        
        api = get_remnawave_api(settings.api_url, settings.api_token, settings.cookie_secret)
        
        try:
            users = await api.get_all_users_paginated(size=50)
            
            if not users:
                return
            
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            
            # Use lock and retry for database write
            async with _db_write_lock:
                await self._save_user_cache_to_db(users, now)
            
            logger.info(f"Updated user cache: {len(users)} users")
            
        except RemnawaveAPIError as e:
            logger.warning(f"Failed to update user cache: {e.message}")
        finally:
            await api.close()
    
    async def _save_user_cache_to_db(self, users: list[dict], now: datetime):
        """Save user cache to DB with retry on lock."""
        delay = DB_RETRY_DELAY
        
        for attempt in range(DB_RETRY_ATTEMPTS):
            try:
                async with async_session() as db:
                    with db.no_autoflush:
                        for user in users:
                            user_id = user.get("id")
                            if not user_id:
                                continue
                            
                            # Parse dates
                            expire_at = self._parse_datetime(user.get("expireAt"))
                            sub_revoked_at = self._parse_datetime(user.get("subRevokedAt"))
                            sub_last_opened_at = self._parse_datetime(user.get("subLastOpenedAt"))
                            last_traffic_reset_at = self._parse_datetime(user.get("lastTrafficResetAt"))
                            created_at = self._parse_datetime(user.get("createdAt"))
                            
                            # Parse userTraffic
                            user_traffic = user.get("userTraffic") or {}
                            online_at = self._parse_datetime(user_traffic.get("onlineAt"))
                            first_connected_at = self._parse_datetime(user_traffic.get("firstConnectedAt"))
                            
                            existing = await db.execute(
                                select(RemnawaveUserCache).where(
                                    RemnawaveUserCache.email == user_id
                                )
                            )
                            cache_entry = existing.scalar_one_or_none()
                            
                            if cache_entry:
                                cache_entry.uuid = user.get("uuid")
                                cache_entry.short_uuid = user.get("shortUuid")
                                cache_entry.username = user.get("username")
                                cache_entry.telegram_id = user.get("telegramId")
                                cache_entry.status = user.get("status")
                                # Subscription info
                                cache_entry.expire_at = expire_at
                                cache_entry.subscription_url = user.get("subscriptionUrl")
                                cache_entry.sub_revoked_at = sub_revoked_at
                                cache_entry.sub_last_user_agent = user.get("subLastUserAgent")
                                cache_entry.sub_last_opened_at = sub_last_opened_at
                                # Traffic limits
                                cache_entry.traffic_limit_bytes = user.get("trafficLimitBytes")
                                cache_entry.traffic_limit_strategy = user.get("trafficLimitStrategy")
                                cache_entry.last_traffic_reset_at = last_traffic_reset_at
                                # Traffic usage
                                cache_entry.used_traffic_bytes = user_traffic.get("usedTrafficBytes")
                                cache_entry.lifetime_used_traffic_bytes = user_traffic.get("lifetimeUsedTrafficBytes")
                                cache_entry.online_at = online_at
                                cache_entry.first_connected_at = first_connected_at
                                cache_entry.last_connected_node_uuid = user_traffic.get("lastConnectedNodeUuid")
                                # Device limit
                                cache_entry.hwid_device_limit = user.get("hwidDeviceLimit")
                                # Additional info
                                cache_entry.user_email = user.get("email")
                                cache_entry.description = user.get("description")
                                cache_entry.tag = user.get("tag")
                                cache_entry.created_at = created_at
                                cache_entry.updated_at = now
                            else:
                                db.add(RemnawaveUserCache(
                                    email=user_id,
                                    uuid=user.get("uuid"),
                                    short_uuid=user.get("shortUuid"),
                                    username=user.get("username"),
                                    telegram_id=user.get("telegramId"),
                                    status=user.get("status"),
                                    # Subscription info
                                    expire_at=expire_at,
                                    subscription_url=user.get("subscriptionUrl"),
                                    sub_revoked_at=sub_revoked_at,
                                    sub_last_user_agent=user.get("subLastUserAgent"),
                                    sub_last_opened_at=sub_last_opened_at,
                                    # Traffic limits
                                    traffic_limit_bytes=user.get("trafficLimitBytes"),
                                    traffic_limit_strategy=user.get("trafficLimitStrategy"),
                                    last_traffic_reset_at=last_traffic_reset_at,
                                    # Traffic usage
                                    used_traffic_bytes=user_traffic.get("usedTrafficBytes"),
                                    lifetime_used_traffic_bytes=user_traffic.get("lifetimeUsedTrafficBytes"),
                                    online_at=online_at,
                                    first_connected_at=first_connected_at,
                                    last_connected_node_uuid=user_traffic.get("lastConnectedNodeUuid"),
                                    # Device limit
                                    hwid_device_limit=user.get("hwidDeviceLimit"),
                                    # Additional info
                                    user_email=user.get("email"),
                                    description=user.get("description"),
                                    tag=user.get("tag"),
                                    created_at=created_at,
                                    updated_at=now
                                ))
                    
                    await db.commit()
                    return  # Success
                    
            except OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < DB_RETRY_ATTEMPTS - 1:
                    logger.warning(f"Database locked during user cache update, retry {attempt + 1}/{DB_RETRY_ATTEMPTS}")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, DB_RETRY_MAX_DELAY)
                else:
                    logger.error(f"Database error in user cache update: {e}")
                    raise
    
    def _parse_datetime(self, value: str | None) -> datetime | None:
        """Parse ISO datetime string to datetime object."""
        if not value:
            return None
        try:
            # Handle ISO format with timezone
            if value.endswith('Z'):
                value = value[:-1] + '+00:00'
            dt = datetime.fromisoformat(value)
            # Remove timezone info for SQLite compatibility
            return dt.replace(tzinfo=None)
        except (ValueError, TypeError):
            return None
    
    async def _cleanup_loop(self):
        """Background loop for cleaning old data."""
        while self._running:
            try:
                await asyncio.sleep(86400)  # Once per day
                await self._cleanup_old_data()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
    
    async def _cleanup_old_data(self):
        """Remove old stats and stale user cache."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        async with _db_write_lock:
            delay = DB_RETRY_DELAY
            
            for attempt in range(DB_RETRY_ATTEMPTS):
                try:
                    async with async_session() as db:
                        # Remove hourly data older than retention period (365 days)
                        hourly_cutoff = now - timedelta(days=self._hourly_retention_days)
                        await db.execute(
                            delete(XrayHourlyStats).where(
                                XrayHourlyStats.hour < hourly_cutoff
                            )
                        )
                        
                        # Remove visit stats older than 365 days (based on last_seen)
                        visit_cutoff = now - timedelta(days=365)
                        result = await db.execute(
                            delete(XrayVisitStats).where(
                                XrayVisitStats.last_seen < visit_cutoff
                            )
                        )
                        deleted_visits = result.rowcount
                        
                        # Remove IP stats older than 365 days (based on last_seen)
                        ip_result = await db.execute(
                            delete(XrayUserIpStats).where(
                                XrayUserIpStats.last_seen < visit_cutoff
                            )
                        )
                        deleted_ips = ip_result.rowcount
                        
                        # Remove IP-destination stats older than 365 days (based on last_seen)
                        ip_dest_result = await db.execute(
                            delete(XrayIpDestinationStats).where(
                                XrayIpDestinationStats.last_seen < visit_cutoff
                            )
                        )
                        deleted_ip_dests = ip_dest_result.rowcount
                        
                        # Remove stale user cache entries (not updated for 7 days)
                        cache_cutoff = now - timedelta(days=7)
                        await db.execute(
                            delete(RemnawaveUserCache).where(
                                RemnawaveUserCache.updated_at < cache_cutoff
                            )
                        )
                        
                        await db.commit()
                    
                    if deleted_visits > 0 or deleted_ips > 0 or deleted_ip_dests > 0:
                        logger.info(f"Xray stats cleanup: {deleted_visits} visit records, {deleted_ips} IP records, {deleted_ip_dests} IP-destination records removed (older than 365 days)")
                    else:
                        logger.info("Xray stats cleanup completed")
                    return  # Success
                    
                except OperationalError as e:
                    if "database is locked" in str(e).lower() and attempt < DB_RETRY_ATTEMPTS - 1:
                        logger.warning(f"Database locked during cleanup, retry {attempt + 1}/{DB_RETRY_ATTEMPTS}")
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, DB_RETRY_MAX_DELAY)
                    else:
                        logger.error(f"Database error in cleanup: {e}")
                        raise


# Singleton instance
_collector: Optional[XrayStatsCollector] = None


def get_xray_stats_collector() -> XrayStatsCollector:
    """Get or create Xray stats collector instance."""
    global _collector
    if _collector is None:
        _collector = XrayStatsCollector()
    return _collector


async def start_xray_stats_collector():
    """Start the Xray stats collector."""
    collector = get_xray_stats_collector()
    await collector.start()


async def stop_xray_stats_collector():
    """Stop the Xray stats collector."""
    collector = get_xray_stats_collector()
    await collector.stop()
