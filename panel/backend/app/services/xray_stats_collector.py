"""
Xray stats collector for Remnawave integration.

Simplified: single table xray_stats (email, source_ip, host) -> count.
One batch upsert per collection cycle, no normalization tables.
"""

import asyncio
import json
import logging
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
    RemnawaveExcludedDestination, XrayStats, XrayHourlyStats, RemnawaveUserCache
)
from app.services.remnawave_api import get_remnawave_api, RemnawaveAPIError

logger = logging.getLogger(__name__)

UPSERT_BATCH_SIZE = 500
USER_CACHE_BATCH_SIZE = 500

# DNS cache for infrastructure addresses
_dns_cache: dict[str, tuple[set[str], datetime]] = {}
_DNS_CACHE_TTL = timedelta(hours=1)


def _extract_host(destination: str) -> str:
    """Extract host from destination, stripping :port suffix."""
    parts = destination.rsplit(':', 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return destination


def extract_host_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.hostname or ""
    except Exception:
        return ""


def is_valid_ip(address: str) -> bool:
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
    if is_valid_ip(domain):
        return {domain}
    ips = set()
    try:
        results = socket.getaddrinfo(domain, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for result in results:
            ips.add(result[4][0])
    except Exception:
        pass
    return ips


async def resolve_infrastructure_address(address: str, use_cache: bool = True) -> set[str]:
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


async def rebuild_summaries():
    """Rebuild pre-computed summary tables from xray_stats."""
    try:
        async with async_session() as db:
            # 1. Global summary
            await db.execute(text("""
                INSERT INTO xray_global_summary (id, total_visits, unique_users, unique_destinations, last_updated)
                SELECT 1,
                    COALESCE(SUM(count), 0),
                    COUNT(DISTINCT email),
                    COUNT(DISTINCT host),
                    NOW()
                FROM xray_stats
                ON CONFLICT (id) DO UPDATE SET
                    total_visits = EXCLUDED.total_visits,
                    unique_users = EXCLUDED.unique_users,
                    unique_destinations = EXCLUDED.unique_destinations,
                    last_updated = EXCLUDED.last_updated
            """))
            
            # 2. Destination summary
            await db.execute(text("DELETE FROM xray_destination_summary"))
            await db.execute(text("""
                INSERT INTO xray_destination_summary (host, total_visits, unique_users, last_seen)
                SELECT host, SUM(count), COUNT(DISTINCT email), MAX(last_seen)
                FROM xray_stats
                GROUP BY host
            """))
            
            # 3. User summary (infrastructure IPs computed here)
            # MIN_ASN_VISIT_COUNT = 1000 — only IPs with >= 1000 visits count as active client IPs
            await db.execute(text("DELETE FROM xray_user_summary"))
            await db.execute(text("""
                INSERT INTO xray_user_summary (email, total_visits, unique_sites, unique_client_ips, infrastructure_ips, first_seen, last_seen)
                SELECT email, SUM(count), COUNT(DISTINCT host), 0, 0,
                       MIN(first_seen), MAX(last_seen)
                FROM xray_stats
                GROUP BY email
            """))
            
            # Update active IP counts (only IPs with >= 1000 total visits count as active client IPs)
            infra_ips = await _get_infrastructure_ips_sql(db)
            if infra_ips:
                placeholders = ",".join(f"'{ip}'" for ip in infra_ips)
                await db.execute(text(f"""
                    UPDATE xray_user_summary us SET
                        unique_client_ips = COALESCE(sub.client_ips, 0),
                        infrastructure_ips = COALESCE(sub.infra_ips, 0)
                    FROM (
                        SELECT email,
                            COUNT(CASE WHEN source_ip NOT IN ({placeholders}) AND ip_total >= 1000 THEN 1 END) as client_ips,
                            COUNT(CASE WHEN source_ip IN ({placeholders}) THEN 1 END) as infra_ips
                        FROM (
                            SELECT email, source_ip, SUM(count) as ip_total
                            FROM xray_stats
                            GROUP BY email, source_ip
                        ) ip_stats
                        GROUP BY email
                    ) sub
                    WHERE us.email = sub.email
                """))
            else:
                await db.execute(text("""
                    UPDATE xray_user_summary us SET
                        unique_client_ips = COALESCE(sub.active_ips, 0)
                    FROM (
                        SELECT email,
                            COUNT(CASE WHEN ip_total >= 1000 THEN 1 END) as active_ips
                        FROM (
                            SELECT email, source_ip, SUM(count) as ip_total
                            FROM xray_stats
                            GROUP BY email, source_ip
                        ) ip_stats
                        GROUP BY email
                    ) sub
                    WHERE us.email = sub.email
                """))
            
            await db.commit()
            logger.debug("Summary tables rebuilt")
    except Exception as e:
        logger.error(f"Failed to rebuild summary tables: {e}")


async def _get_infrastructure_ips_sql(db: AsyncSession) -> set[str]:
    """Get set of all infrastructure IP addresses for SQL queries."""
    infrastructure_ips = set()
    
    # From server URLs
    result = await db.execute(select(Server.url))
    for row in result.fetchall():
        if row[0]:
            host = extract_host_from_url(row[0])
            if host:
                infrastructure_ips.add(host)
    
    # From infrastructure addresses
    result = await db.execute(select(RemnawaveInfrastructureAddress))
    for addr in result.scalars().all():
        infrastructure_ips.add(addr.address)
        if addr.resolved_ips:
            try:
                resolved = json.loads(addr.resolved_ips)
                infrastructure_ips.update(resolved)
            except json.JSONDecodeError:
                pass
    
    return infrastructure_ips


class XrayStatsCollector:
    """Collects Xray visit stats from Remnawave nodes."""
    
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._user_cache_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        
        self._collection_interval = 300
        self._user_cache_interval = 1800
        self._time_since_last_collect = 0
        
        self._last_collect_time: Optional[datetime] = None
        self._collecting = False
        
        self._last_user_cache_update: Optional[datetime] = None
        self._user_cache_updating = False
        
        self._ignored_user_ids: set[int] = set()
        self._db_write_lock = asyncio.Lock()
    
    async def _get_ignored_user_ids(self) -> set[int]:
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
        async with async_session() as db:
            result = await db.execute(select(RemnawaveExcludedDestination.destination))
            return {row[0] for row in result.fetchall()}
    
    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._collection_loop())
        self._user_cache_task = asyncio.create_task(self._user_cache_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Xray stats collector started (single-table mode)")
    
    async def stop(self):
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
        async with async_session() as db:
            result = await db.execute(select(RemnawaveSettings).limit(1))
            return result.scalar_one_or_none()
    
    async def _collection_loop(self):
        self._time_since_last_collect = 0
        _SETTINGS_CHECK_INTERVAL = 15
        _settings_check_counter = 0
        _cached_enabled = False
        
        while self._running:
            try:
                if _settings_check_counter >= _SETTINGS_CHECK_INTERVAL:
                    settings = await self._get_settings()
                    _settings_check_counter = 0
                    if settings:
                        new_interval = settings.collection_interval or 60
                        if new_interval != self._collection_interval:
                            logger.info(f"Collection interval changed: {self._collection_interval}s -> {new_interval}s")
                            self._collection_interval = new_interval
                            if self._time_since_last_collect >= new_interval:
                                self._time_since_last_collect = new_interval
                        _cached_enabled = settings.enabled
                    else:
                        _cached_enabled = False
                
                if _cached_enabled and self._time_since_last_collect >= self._collection_interval:
                    await self._collect_from_all_nodes()
                    self._time_since_last_collect = 0
                    try:
                        await rebuild_summaries()
                        from app.routers.remnawave import warm_batch_cache
                        await warm_batch_cache()
                    except Exception as e:
                        logger.warning(f"Post-collection tasks failed: {e}")
                
                await asyncio.sleep(1)
                self._time_since_last_collect += 1
                _settings_check_counter += 1
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Collection error: {e}")
                await asyncio.sleep(5)
                self._time_since_last_collect += 5
                _settings_check_counter += 5
    
    async def _collect_from_all_nodes(self):
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
        settings = await self._get_settings()
        if not settings or not settings.enabled:
            return {"success": False, "error": "Collection is disabled", "collected_at": None, "nodes_count": 0}
        if self._collecting:
            return {"success": False, "error": "Collection already in progress", "collected_at": None, "nodes_count": 0}
        
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
                
                if stats:
                    await self._save_stats(stats)
                
                await self._update_node_status(node.id, error=None)
                logger.debug(f"Collected {len(stats)} stat entries from {server.name}")
                
        except Exception as e:
            error_msg = str(e)[:500]
            logger.debug(f"Failed to collect from {server.name}: {error_msg}")
            await self._update_node_status(node.id, error=error_msg)
    
    async def _update_node_status(self, node_id: int, error: str | None):
        for attempt in range(1, 4):
            try:
                async with async_session() as db:
                    if error is None:
                        await db.execute(
                            update(RemnawaveNode).where(RemnawaveNode.id == node_id)
                            .values(last_collected=datetime.now(timezone.utc).replace(tzinfo=None), last_error=None)
                        )
                    else:
                        await db.execute(
                            update(RemnawaveNode).where(RemnawaveNode.id == node_id)
                            .values(last_error=error)
                        )
                    await db.commit()
                return
            except Exception as e:
                if "deadlock" in str(e).lower() and attempt < 3:
                    await asyncio.sleep(0.3 * attempt)
                    continue
                logger.error(f"Failed to update node status: {e}")
                return
    
    async def _get_infrastructure_ips(self) -> set[str]:
        infrastructure_ips = set()
        
        async with async_session() as db:
            # From server URLs
            result = await db.execute(select(Server.url))
            for row in result.fetchall():
                if row[0]:
                    host = extract_host_from_url(row[0])
                    if host:
                        infrastructure_ips.add(host)
            
            # From infrastructure addresses
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
    
    async def _save_stats(self, stats: list[dict]):
        """Save stats to DB using single batch upsert into xray_stats.
        
        Uses asyncio.Lock to prevent concurrent writes that cause deadlocks
        when multiple nodes are collected in parallel.
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        
        ignored_user_ids = await self._get_ignored_user_ids()
        excluded_destinations = await self._get_excluded_destinations()
        
        updates: dict[tuple[int, str, str], int] = {}
        total_count = 0
        unique_users: set[int] = set()
        unique_hosts: set[str] = set()
        
        for stat in stats:
            email = stat.get("email", 0)
            source_ip = stat.get("source_ip", "")
            host = stat.get("host") or _extract_host(stat.get("destination", ""))
            count = stat.get("count", 0)
            
            if not email or not source_ip or not host or not count:
                continue
            if email in ignored_user_ids:
                continue
            if host in excluded_destinations:
                continue
            
            key = (email, source_ip, host)
            updates[key] = updates.get(key, 0) + count
            total_count += count
            unique_users.add(email)
            unique_hosts.add(host)
        
        if not updates:
            return
        
        async with self._db_write_lock:
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    async with async_session() as db:
                        items = list(updates.items())
                        for i in range(0, len(items), UPSERT_BATCH_SIZE):
                            batch = items[i:i + UPSERT_BATCH_SIZE]
                            values = [
                                {
                                    "email": email,
                                    "source_ip": source_ip,
                                    "host": host,
                                    "count": count,
                                    "first_seen": now,
                                    "last_seen": now
                                }
                                for (email, source_ip, host), count in batch
                            ]
                            
                            stmt = pg_insert(XrayStats).values(values)
                            stmt = stmt.on_conflict_do_update(
                                index_elements=['email', 'source_ip', 'host'],
                                set_={
                                    'count': XrayStats.count + stmt.excluded.count,
                                    'last_seen': stmt.excluded.last_seen
                                }
                            )
                            await db.execute(stmt)
                        
                        hourly_stmt = pg_insert(XrayHourlyStats).values(
                            server_id=0,
                            hour=hour_start,
                            visit_count=total_count,
                            unique_users=len(unique_users),
                            unique_destinations=len(unique_hosts)
                        )
                        hourly_stmt = hourly_stmt.on_conflict_do_update(
                            index_elements=['server_id', 'hour'],
                            set_={
                                'visit_count': XrayHourlyStats.visit_count + hourly_stmt.excluded.visit_count,
                                'unique_users': hourly_stmt.excluded.unique_users,
                                'unique_destinations': hourly_stmt.excluded.unique_destinations
                            }
                        )
                        await db.execute(hourly_stmt)
                        
                        await db.commit()
                    
                    logger.debug(f"Saved {len(updates)} unique entries via batch upsert")
                    return
                except Exception as e:
                    is_deadlock = "deadlock" in str(e).lower()
                    if is_deadlock and attempt < max_retries:
                        logger.warning(f"Deadlock in _save_stats (attempt {attempt}/{max_retries}), retrying...")
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    raise
    
    # === User cache ===
    
    async def _user_cache_loop(self):
        await asyncio.sleep(30)
        while self._running:
            try:
                settings = await self._get_settings()
                if settings and settings.enabled:
                    await self._update_user_cache()
                await asyncio.sleep(self._user_cache_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"User cache error: {e}")
                await asyncio.sleep(300)
    
    async def _update_user_cache(self) -> dict:
        settings = await self._get_settings()
        if not settings or not settings.api_url or not settings.api_token:
            return {"success": False, "error": "API not configured", "count": 0}
        if self._user_cache_updating:
            return {"success": False, "error": "Update already in progress", "count": 0}
        
        self._user_cache_updating = True
        max_retries = 2
        last_error = None
        
        for attempt in range(1, max_retries + 1):
            api = get_remnawave_api(settings.api_url, settings.api_token, settings.cookie_secret)
            try:
                users = await api.get_all_users_paginated(size=200, concurrency=5)
                
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                async with async_session() as db:
                    fetched_emails = set()
                    for user in users:
                        uid = user.get("id")
                        if uid:
                            fetched_emails.add(uid)
                    
                    if users:
                        await self._batch_upsert_user_cache(db, users, now)
                    
                    # Удаляем пользователей, которых больше нет в Remnawave
                    if fetched_emails:
                        await db.execute(
                            delete(RemnawaveUserCache).where(
                                RemnawaveUserCache.email.notin_(list(fetched_emails))
                            )
                        )
                    
                    await db.commit()
                
                self._last_user_cache_update = now
                logger.info(f"User cache synced: {len(users)} users (stale removed)")
                self._user_cache_updating = False
                return {"success": True, "count": len(users), "error": None}
                
            except RemnawaveAPIError as e:
                last_error = e.message
                logger.warning(f"User cache sync attempt {attempt}/{max_retries} failed: {e.message}")
            except Exception as e:
                last_error = str(e)
                logger.warning(f"User cache sync attempt {attempt}/{max_retries} error: {e}")
            finally:
                await api.close()
            
            if attempt < max_retries:
                await asyncio.sleep(5)
        
        self._user_cache_updating = False
        logger.warning(f"User cache sync failed after {max_retries} attempts, keeping old cache")
        return {"success": False, "error": last_error, "count": 0}
    
    async def refresh_user_cache_now(self) -> dict:
        return await self._update_user_cache()
    
    def get_user_cache_status(self) -> dict:
        return {
            "last_update": self._last_user_cache_update.isoformat() if self._last_user_cache_update else None,
            "updating": self._user_cache_updating,
            "update_interval": self._user_cache_interval
        }
    
    async def _batch_upsert_user_cache(self, db: AsyncSession, users: list[dict], now: datetime):
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
                set_={col: getattr(stmt.excluded, col) for col in [
                    'uuid', 'short_uuid', 'username', 'telegram_id', 'status',
                    'expire_at', 'subscription_url', 'sub_revoked_at', 'sub_last_user_agent',
                    'sub_last_opened_at', 'traffic_limit_bytes', 'traffic_limit_strategy',
                    'last_traffic_reset_at', 'used_traffic_bytes', 'lifetime_used_traffic_bytes',
                    'online_at', 'first_connected_at', 'last_connected_node_uuid',
                    'hwid_device_limit', 'user_email', 'description', 'tag',
                    'created_at', 'updated_at'
                ]}
            )
            await db.execute(stmt)
    
    def _parse_datetime(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            if value.endswith('Z'):
                value = value[:-1] + '+00:00'
            dt = datetime.fromisoformat(value)
            return dt.replace(tzinfo=None)
        except (ValueError, TypeError):
            return None
    
    # === Cleanup ===
    
    async def _cleanup_loop(self):
        while self._running:
            try:
                await asyncio.sleep(86400)
                await self._cleanup_old_data()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
    
    async def _cleanup_old_data(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        try:
            settings = await self._get_settings()
            stats_retention = settings.visit_stats_retention_days if settings and settings.visit_stats_retention_days else 365
            hourly_retention = settings.hourly_stats_retention_days if settings and settings.hourly_stats_retention_days else 365
            
            async with async_session() as db:
                # Remove old stats
                stats_cutoff = now - timedelta(days=stats_retention)
                stats_result = await db.execute(
                    delete(XrayStats).where(XrayStats.last_seen < stats_cutoff)
                )
                deleted_stats = stats_result.rowcount
                
                # Remove old hourly data
                hourly_cutoff = now - timedelta(days=hourly_retention)
                await db.execute(
                    delete(XrayHourlyStats).where(XrayHourlyStats.hour < hourly_cutoff)
                )
                
                # Remove stale user cache
                cache_cutoff = now - timedelta(days=7)
                await db.execute(
                    delete(RemnawaveUserCache).where(RemnawaveUserCache.updated_at < cache_cutoff)
                )
                
                await db.commit()
            
            if deleted_stats > 0:
                logger.info(f"Xray stats cleanup: {deleted_stats} rows removed")
                try:
                    async with async_session() as vacuum_db:
                        await vacuum_db.execute(text("VACUUM xray_stats, xray_hourly_stats"))
                    logger.info("VACUUM completed")
                except Exception as ve:
                    logger.debug(f"VACUUM failed (non-critical): {ve}")
            else:
                logger.info("Xray stats cleanup completed (nothing to delete)")
                
        except Exception as e:
            logger.error(f"Database error in cleanup: {e}")


# Singleton
_collector: Optional[XrayStatsCollector] = None


def get_xray_stats_collector() -> XrayStatsCollector:
    global _collector
    if _collector is None:
        _collector = XrayStatsCollector()
    return _collector


async def start_xray_stats_collector():
    collector = get_xray_stats_collector()
    await collector.start()


async def stop_xray_stats_collector():
    collector = get_xray_stats_collector()
    await collector.stop()
