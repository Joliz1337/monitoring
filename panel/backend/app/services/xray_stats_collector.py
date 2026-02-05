"""
Xray stats collector for Remnawave integration.

Optimized version with PostgreSQL batch upsert (ON CONFLICT):
- XrayVisitStats: (server, destination, email) -> total_count (incremented)
- XrayHourlyStats: (server, hour) -> counts (for timeline charts)

Performance:
- Batch INSERT ... ON CONFLICT instead of per-row SELECT + UPDATE
- 10-100x faster writes compared to SQLite version
- Concurrent writes supported (PostgreSQL)
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
from sqlalchemy import select, delete, update, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.models import (
    Server, RemnawaveSettings, RemnawaveNode, RemnawaveInfrastructureAddress,
    RemnawaveExcludedDestination, XrayVisitStats, XrayHourlyStats, RemnawaveUserCache,
    XrayUserIpStats, XrayIpDestinationStats, XrayDestination
)
from app.services.remnawave_api import get_remnawave_api, RemnawaveAPIError

logger = logging.getLogger(__name__)

# DNS cache for infrastructure addresses (address -> (resolved_ips, timestamp))
_dns_cache: dict[str, tuple[set[str], datetime]] = {}
_DNS_CACHE_TTL = timedelta(hours=1)

# Batch size for upserts
UPSERT_BATCH_SIZE = 5000
# User cache has 25 fields, PostgreSQL limit is ~32767 params, so max 1300 users per batch
USER_CACHE_BATCH_SIZE = 500


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
    """Resolve domain to all IP addresses (A and AAAA records)."""
    if is_valid_ip(domain):
        return {domain}
    
    ips = set()
    try:
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
    
    if use_cache and address in _dns_cache:
        cached_ips, cached_time = _dns_cache[address]
        if now - cached_time < _DNS_CACHE_TTL:
            return cached_ips
    
    loop = asyncio.get_event_loop()
    ips = await loop.run_in_executor(None, resolve_domain_to_ips, address)
    
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
        self._time_since_last_collect = 0
        
        self._last_collect_time: Optional[datetime] = None
        self._collecting = False
        
        self._last_user_cache_update: Optional[datetime] = None
        self._user_cache_updating = False
        
        # Cache for ignored users (refreshed on each collection)
        self._ignored_user_ids: set[int] = set()
    
    async def _get_ignored_user_ids(self) -> set[int]:
        """Get set of ignored user IDs from settings."""
        async with async_session() as db:
            result = await db.execute(select(RemnawaveSettings).limit(1))
            settings = result.scalar_one_or_none()
            
            if not settings or not settings.ignored_user_ids:
                return set()
            
            try:
                data = json.loads(settings.ignored_user_ids)
                if isinstance(data, list):
                    return {int(x) for x in data if isinstance(x, (int, str)) and str(x).isdigit()}
                return set()
            except (json.JSONDecodeError, ValueError):
                return set()
    
    async def _get_excluded_destinations(self) -> set[str]:
        """Get set of excluded destinations from database."""
        async with async_session() as db:
            result = await db.execute(select(RemnawaveExcludedDestination.destination))
            return {row[0] for row in result.fetchall()}
    
    async def start(self):
        """Start background collection."""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._collection_loop())
        self._user_cache_task = asyncio.create_task(self._user_cache_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Xray stats collector started (PostgreSQL batch mode)")
    
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
                settings = await self._get_settings()
                
                if settings:
                    new_interval = settings.collection_interval or 60
                    if new_interval != self._collection_interval:
                        logger.info(f"Collection interval changed: {self._collection_interval}s -> {new_interval}s")
                        self._collection_interval = new_interval
                        if self._time_since_last_collect >= new_interval:
                            self._time_since_last_collect = new_interval
                
                if settings and settings.enabled and self._time_since_last_collect >= self._collection_interval:
                    await self._collect_from_all_nodes()
                    self._time_since_last_collect = 0
                
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
        """Update node status."""
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
        except Exception as e:
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
        """Get set of all infrastructure IP addresses."""
        infrastructure_ips = set()
        
        node_ips = await self._get_node_ips()
        infrastructure_ips.update(node_ips)
        
        async with async_session() as db:
            result = await db.execute(select(RemnawaveInfrastructureAddress))
            addresses = result.scalars().all()
            
            for addr in addresses:
                resolved = await resolve_infrastructure_address(addr.address)
                infrastructure_ips.update(resolved)
                
                resolved_json = json.dumps(sorted(resolved)) if resolved else None
                if resolved_json != addr.resolved_ips:
                    addr.resolved_ips = resolved_json
                    addr.last_resolved = datetime.now(timezone.utc).replace(tzinfo=None)
            
            await db.commit()
        
        return infrastructure_ips
    
    async def _get_or_create_destination_ids(self, db: AsyncSession, destinations: set[str], now: datetime) -> dict[str, int]:
        """Get or create destination IDs for a set of destinations.
        
        Returns a mapping from destination string to destination_id.
        Uses batch upsert for efficiency.
        """
        if not destinations:
            return {}
        
        dest_list = list(destinations)
        dest_to_id: dict[str, int] = {}
        
        # Batch upsert destinations
        for i in range(0, len(dest_list), UPSERT_BATCH_SIZE):
            batch = dest_list[i:i + UPSERT_BATCH_SIZE]
            
            values = [
                {
                    "destination": dest,
                    "first_seen": now,
                    "hit_count": 0
                }
                for dest in batch
            ]
            
            stmt = pg_insert(XrayDestination).values(values)
            stmt = stmt.on_conflict_do_update(
                index_elements=['destination'],
                set_={'hit_count': XrayDestination.hit_count + 1}
            )
            await db.execute(stmt)
        
        # Fetch all destination IDs
        result = await db.execute(
            select(XrayDestination.id, XrayDestination.destination)
            .where(XrayDestination.destination.in_(dest_list))
        )
        for row in result.fetchall():
            dest_to_id[row[1]] = row[0]
        
        return dest_to_id

    async def _save_stats(self, server_id: int, stats: list[dict], ip_stats: list[dict] = None, ip_destination_stats: list[dict] = None):
        """Save collected stats to DB using PostgreSQL batch upsert."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        
        if ip_stats is None:
            ip_stats = []
        if ip_destination_stats is None:
            ip_destination_stats = []
        
        # Get ignored user IDs (users excluded from all stats)
        ignored_user_ids = await self._get_ignored_user_ids()
        if ignored_user_ids:
            logger.debug(f"Filtering out {len(ignored_user_ids)} ignored users from stats")
        
        # Get excluded destinations (sites excluded from statistics)
        excluded_destinations = await self._get_excluded_destinations()
        if excluded_destinations:
            logger.debug(f"Filtering out {len(excluded_destinations)} excluded destinations from stats")
        
        # Collect all unique destinations first
        all_destinations: set[str] = set()
        
        # Aggregate stats for batch processing
        visit_updates: dict[tuple[str, int], int] = {}
        total_count = 0
        unique_users = set()
        unique_destinations = set()
        
        for stat in stats:
            destination = stat.get("destination", "")
            email = stat.get("email", 0)
            count = stat.get("count", 0)
            
            if not destination or not email or not count:
                continue
            
            # Skip ignored users
            if email in ignored_user_ids:
                continue
            
            # Skip excluded destinations
            if destination in excluded_destinations:
                continue
            
            key = (destination, email)
            visit_updates[key] = visit_updates.get(key, 0) + count
            total_count += count
            unique_users.add(email)
            unique_destinations.add(destination)
            all_destinations.add(destination)
        
        # Get infrastructure IPs
        infrastructure_ips = await self._get_infrastructure_ips()
        
        # Aggregate IP stats with infrastructure flag
        ip_updates: dict[tuple[int, str], tuple[int, bool]] = {}
        for ip_stat in ip_stats:
            email = ip_stat.get("email", 0)
            source_ip = ip_stat.get("source_ip", "")
            count = ip_stat.get("count", 0)
            
            if not email or not source_ip or not count:
                continue
            
            # Skip ignored users
            if email in ignored_user_ids:
                continue
            
            is_infra = source_ip in infrastructure_ips
            key = (email, source_ip)
            if key in ip_updates:
                existing_count, existing_infra = ip_updates[key]
                ip_updates[key] = (existing_count + count, is_infra)
            else:
                ip_updates[key] = (count, is_infra)
        
        # Aggregate IP-destination stats (skip infrastructure IPs, ignored users, and excluded destinations)
        ip_dest_updates: dict[tuple[int, str, str], int] = {}
        for ip_dest_stat in ip_destination_stats:
            email = ip_dest_stat.get("email", 0)
            source_ip = ip_dest_stat.get("source_ip", "")
            destination = ip_dest_stat.get("destination", "")
            count = ip_dest_stat.get("count", 0)
            
            if not email or not source_ip or not destination or not count:
                continue
            
            # Skip ignored users
            if email in ignored_user_ids:
                continue
            
            # Skip excluded destinations
            if destination in excluded_destinations:
                continue
            
            if source_ip in infrastructure_ips:
                continue
            
            key = (email, source_ip, destination)
            ip_dest_updates[key] = ip_dest_updates.get(key, 0) + count
            all_destinations.add(destination)
        
        if not visit_updates and not ip_updates and not ip_dest_updates:
            return
        
        async with async_session() as db:
            # Get or create destination IDs for all destinations
            dest_to_id = await self._get_or_create_destination_ids(db, all_destinations, now)
            
            # Batch upsert visit stats
            if visit_updates:
                await self._batch_upsert_visits(db, server_id, visit_updates, dest_to_id, now)
            
            # Batch upsert IP stats
            if ip_updates:
                await self._batch_upsert_ip_stats(db, server_id, ip_updates, now)
            
            # Batch upsert IP-destination stats
            if ip_dest_updates:
                await self._batch_upsert_ip_dest_stats(db, server_id, ip_dest_updates, dest_to_id, now)
            
            # Upsert hourly stats
            await self._upsert_hourly_stats(db, server_id, hour_start, total_count, len(unique_users), len(unique_destinations))
            
            await db.commit()
        
        logger.debug(f"Saved {len(visit_updates)} visit entries, {len(ip_updates)} IP entries, {len(ip_dest_updates)} IP-dest entries via batch upsert")
    
    async def _batch_upsert_visits(self, db: AsyncSession, server_id: int, updates: dict, dest_to_id: dict[str, int], now: datetime):
        """Batch upsert visit stats using PostgreSQL ON CONFLICT."""
        items = list(updates.items())
        
        for i in range(0, len(items), UPSERT_BATCH_SIZE):
            batch = items[i:i + UPSERT_BATCH_SIZE]
            
            values = []
            for (dest, email), count in batch:
                dest_id = dest_to_id.get(dest)
                if dest_id is None:
                    continue
                values.append({
                    "server_id": server_id,
                    "destination_id": dest_id,
                    "email": email,
                    "visit_count": count,
                    "first_seen": now,
                    "last_seen": now
                })
            
            if not values:
                continue
            
            stmt = pg_insert(XrayVisitStats).values(values)
            stmt = stmt.on_conflict_do_update(
                constraint='uq_xray_stats_unique_v2',
                set_={
                    'visit_count': XrayVisitStats.visit_count + stmt.excluded.visit_count,
                    'last_seen': stmt.excluded.last_seen
                }
            )
            await db.execute(stmt)
    
    async def _batch_upsert_ip_stats(self, db: AsyncSession, server_id: int, updates: dict, now: datetime):
        """Batch upsert IP stats using PostgreSQL ON CONFLICT."""
        items = list(updates.items())
        
        for i in range(0, len(items), UPSERT_BATCH_SIZE):
            batch = items[i:i + UPSERT_BATCH_SIZE]
            
            values = [
                {
                    "server_id": server_id,
                    "email": email,
                    "source_ip": source_ip,
                    "connection_count": count,
                    "is_infrastructure": is_infra,
                    "first_seen": now,
                    "last_seen": now
                }
                for (email, source_ip), (count, is_infra) in batch
            ]
            
            stmt = pg_insert(XrayUserIpStats).values(values)
            stmt = stmt.on_conflict_do_update(
                constraint='uq_user_ip_stats_unique',
                set_={
                    'connection_count': XrayUserIpStats.connection_count + stmt.excluded.connection_count,
                    'is_infrastructure': stmt.excluded.is_infrastructure,
                    'last_seen': stmt.excluded.last_seen
                }
            )
            await db.execute(stmt)
    
    async def _batch_upsert_ip_dest_stats(self, db: AsyncSession, server_id: int, updates: dict, dest_to_id: dict[str, int], now: datetime):
        """Batch upsert IP-destination stats using PostgreSQL ON CONFLICT."""
        items = list(updates.items())
        
        for i in range(0, len(items), UPSERT_BATCH_SIZE):
            batch = items[i:i + UPSERT_BATCH_SIZE]
            
            values = []
            for (email, source_ip, destination), count in batch:
                dest_id = dest_to_id.get(destination)
                if dest_id is None:
                    continue
                values.append({
                    "server_id": server_id,
                    "email": email,
                    "source_ip": source_ip,
                    "destination_id": dest_id,
                    "connection_count": count,
                    "first_seen": now,
                    "last_seen": now
                })
            
            if not values:
                continue
            
            stmt = pg_insert(XrayIpDestinationStats).values(values)
            stmt = stmt.on_conflict_do_update(
                constraint='uq_ip_dest_stats_unique_v2',
                set_={
                    'connection_count': XrayIpDestinationStats.connection_count + stmt.excluded.connection_count,
                    'last_seen': stmt.excluded.last_seen
                }
            )
            await db.execute(stmt)
    
    async def _upsert_hourly_stats(self, db: AsyncSession, server_id: int, hour_start: datetime, total_count: int, unique_users: int, unique_destinations: int):
        """Upsert hourly stats using PostgreSQL ON CONFLICT."""
        stmt = pg_insert(XrayHourlyStats).values(
            server_id=server_id,
            hour=hour_start,
            visit_count=total_count,
            unique_users=unique_users,
            unique_destinations=unique_destinations
        )
        stmt = stmt.on_conflict_do_update(
            constraint='uq_xray_hourly_unique',
            set_={
                'visit_count': XrayHourlyStats.visit_count + stmt.excluded.visit_count,
                'unique_users': stmt.excluded.unique_users,
                'unique_destinations': stmt.excluded.unique_destinations
            }
        )
        await db.execute(stmt)
    
    async def _user_cache_loop(self):
        """Background loop for caching Remnawave users."""
        await asyncio.sleep(30)
        
        while self._running:
            try:
                settings = await self._get_settings()
                # Only auto-update if collection is enabled
                if settings and settings.enabled:
                    await self._update_user_cache()
                await asyncio.sleep(self._user_cache_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"User cache error: {e}")
                await asyncio.sleep(300)
    
    async def _update_user_cache(self) -> dict:
        """Update cached Remnawave users with full info.
        
        Returns:
            dict with update result (success, count, error)
        """
        settings = await self._get_settings()
        
        if not settings or not settings.api_url or not settings.api_token:
            return {"success": False, "error": "API not configured", "count": 0}
        
        if self._user_cache_updating:
            return {"success": False, "error": "Update already in progress", "count": 0}
        
        self._user_cache_updating = True
        api = get_remnawave_api(settings.api_url, settings.api_token, settings.cookie_secret)
        
        try:
            users = await api.get_all_users_paginated(size=50)
            
            if not users:
                return {"success": True, "count": 0, "error": None}
            
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            
            async with async_session() as db:
                await self._batch_upsert_user_cache(db, users, now)
                await db.commit()
            
            self._last_user_cache_update = now
            logger.info(f"Updated user cache: {len(users)} users")
            return {"success": True, "count": len(users), "error": None}
            
        except RemnawaveAPIError as e:
            logger.warning(f"Failed to update user cache: {e.message}")
            return {"success": False, "error": e.message, "count": 0}
        finally:
            self._user_cache_updating = False
            await api.close()
    
    async def refresh_user_cache_now(self) -> dict:
        """Force immediate user cache refresh.
        
        Returns:
            dict with refresh result
        """
        return await self._update_user_cache()
    
    def get_user_cache_status(self) -> dict:
        """Get user cache status."""
        return {
            "last_update": self._last_user_cache_update.isoformat() if self._last_user_cache_update else None,
            "updating": self._user_cache_updating,
            "update_interval": self._user_cache_interval
        }
    
    async def _batch_upsert_user_cache(self, db: AsyncSession, users: list[dict], now: datetime):
        """Batch upsert user cache using PostgreSQL ON CONFLICT."""
        values = []
        
        for user in users:
            user_id = user.get("id")
            if not user_id:
                continue
            
            user_traffic = user.get("userTraffic") or {}
            
            values.append({
                "email": user_id,
                "uuid": user.get("uuid"),
                "short_uuid": user.get("shortUuid"),
                "username": user.get("username"),
                "telegram_id": user.get("telegramId"),
                "status": user.get("status"),
                "expire_at": self._parse_datetime(user.get("expireAt")),
                "subscription_url": user.get("subscriptionUrl"),
                "sub_revoked_at": self._parse_datetime(user.get("subRevokedAt")),
                "sub_last_user_agent": user.get("subLastUserAgent"),
                "sub_last_opened_at": self._parse_datetime(user.get("subLastOpenedAt")),
                "traffic_limit_bytes": user.get("trafficLimitBytes"),
                "traffic_limit_strategy": user.get("trafficLimitStrategy"),
                "last_traffic_reset_at": self._parse_datetime(user.get("lastTrafficResetAt")),
                "used_traffic_bytes": user_traffic.get("usedTrafficBytes"),
                "lifetime_used_traffic_bytes": user_traffic.get("lifetimeUsedTrafficBytes"),
                "online_at": self._parse_datetime(user_traffic.get("onlineAt")),
                "first_connected_at": self._parse_datetime(user_traffic.get("firstConnectedAt")),
                "last_connected_node_uuid": user_traffic.get("lastConnectedNodeUuid"),
                "hwid_device_limit": user.get("hwidDeviceLimit"),
                "user_email": user.get("email"),
                "description": user.get("description"),
                "tag": user.get("tag"),
                "created_at": self._parse_datetime(user.get("createdAt")),
                "updated_at": now
            })
        
        if not values:
            return
        
        for i in range(0, len(values), USER_CACHE_BATCH_SIZE):
            batch = values[i:i + USER_CACHE_BATCH_SIZE]
            
            stmt = pg_insert(RemnawaveUserCache).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=['email'],
                set_={
                    'uuid': stmt.excluded.uuid,
                    'short_uuid': stmt.excluded.short_uuid,
                    'username': stmt.excluded.username,
                    'telegram_id': stmt.excluded.telegram_id,
                    'status': stmt.excluded.status,
                    'expire_at': stmt.excluded.expire_at,
                    'subscription_url': stmt.excluded.subscription_url,
                    'sub_revoked_at': stmt.excluded.sub_revoked_at,
                    'sub_last_user_agent': stmt.excluded.sub_last_user_agent,
                    'sub_last_opened_at': stmt.excluded.sub_last_opened_at,
                    'traffic_limit_bytes': stmt.excluded.traffic_limit_bytes,
                    'traffic_limit_strategy': stmt.excluded.traffic_limit_strategy,
                    'last_traffic_reset_at': stmt.excluded.last_traffic_reset_at,
                    'used_traffic_bytes': stmt.excluded.used_traffic_bytes,
                    'lifetime_used_traffic_bytes': stmt.excluded.lifetime_used_traffic_bytes,
                    'online_at': stmt.excluded.online_at,
                    'first_connected_at': stmt.excluded.first_connected_at,
                    'last_connected_node_uuid': stmt.excluded.last_connected_node_uuid,
                    'hwid_device_limit': stmt.excluded.hwid_device_limit,
                    'user_email': stmt.excluded.user_email,
                    'description': stmt.excluded.description,
                    'tag': stmt.excluded.tag,
                    'created_at': stmt.excluded.created_at,
                    'updated_at': stmt.excluded.updated_at
                }
            )
            await db.execute(stmt)
    
    def _parse_datetime(self, value: str | None) -> datetime | None:
        """Parse ISO datetime string to datetime object."""
        if not value:
            return None
        try:
            if value.endswith('Z'):
                value = value[:-1] + '+00:00'
            dt = datetime.fromisoformat(value)
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
        """Remove old stats and stale user cache based on configurable retention settings."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        try:
            # Get retention settings from DB
            settings = await self._get_settings()
            visit_retention = settings.visit_stats_retention_days if settings and settings.visit_stats_retention_days else 365
            ip_retention = settings.ip_stats_retention_days if settings and settings.ip_stats_retention_days else 90
            ip_dest_retention = settings.ip_destination_retention_days if settings and settings.ip_destination_retention_days else 90
            hourly_retention = settings.hourly_stats_retention_days if settings and settings.hourly_stats_retention_days else 365
            
            async with async_session() as db:
                # Remove hourly data older than retention period
                hourly_cutoff = now - timedelta(days=hourly_retention)
                await db.execute(
                    delete(XrayHourlyStats).where(
                        XrayHourlyStats.hour < hourly_cutoff
                    )
                )
                
                # Remove visit stats older than retention (based on last_seen)
                visit_cutoff = now - timedelta(days=visit_retention)
                result = await db.execute(
                    delete(XrayVisitStats).where(
                        XrayVisitStats.last_seen < visit_cutoff
                    )
                )
                deleted_visits = result.rowcount
                
                # Remove IP stats older than retention
                ip_cutoff = now - timedelta(days=ip_retention)
                ip_result = await db.execute(
                    delete(XrayUserIpStats).where(
                        XrayUserIpStats.last_seen < ip_cutoff
                    )
                )
                deleted_ips = ip_result.rowcount
                
                # Remove IP-destination stats older than retention
                ip_dest_cutoff = now - timedelta(days=ip_dest_retention)
                ip_dest_result = await db.execute(
                    delete(XrayIpDestinationStats).where(
                        XrayIpDestinationStats.last_seen < ip_dest_cutoff
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
                
                # Clean up orphaned destinations (not referenced by any stats)
                # This is done periodically to free space from old destinations
                orphan_result = await db.execute(
                    text("""
                        DELETE FROM xray_destinations 
                        WHERE id NOT IN (SELECT DISTINCT destination_id FROM xray_visit_stats)
                        AND id NOT IN (SELECT DISTINCT destination_id FROM xray_ip_destination_stats)
                    """)
                )
                deleted_orphans = orphan_result.rowcount
                
                await db.commit()
            
            if deleted_visits > 0 or deleted_ips > 0 or deleted_ip_dests > 0 or deleted_orphans > 0:
                logger.info(f"Xray stats cleanup: {deleted_visits} visit records, {deleted_ips} IP records, {deleted_ip_dests} IP-dest records, {deleted_orphans} orphan destinations removed")
            else:
                logger.info("Xray stats cleanup completed")
                
        except Exception as e:
            logger.error(f"Database error in cleanup: {e}")


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
