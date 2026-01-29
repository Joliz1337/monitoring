"""
Xray stats collector for Remnawave integration.

Periodically collects visit statistics from nodes with Remnawave,
stores in panel DB, aggregates hourly/daily, and caches Remnawave users.
"""

import asyncio
import logging
import socket
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select, delete, update, func as sql_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import (
    Server, RemnawaveSettings, RemnawaveNode,
    XrayVisitStats, RemnawaveUserCache
)
from app.services.remnawave_api import get_remnawave_api, RemnawaveAPIError

logger = logging.getLogger(__name__)


class XrayStatsCollector:
    """Collects Xray visit stats from Remnawave nodes."""
    
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._user_cache_task: Optional[asyncio.Task] = None
        self._aggregation_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        
        # Default intervals (will be updated from settings)
        self._collection_interval = 60  # seconds
        self._user_cache_interval = 3600  # 1 hour
        
        # Retention periods
        self._hourly_retention_days = 7
        self._daily_retention_days = 365
        
        # DNS cache for IP resolution
        self._dns_cache: dict[str, tuple[str, float]] = {}  # ip -> (domain, timestamp)
        self._dns_cache_ttl = 3600  # 1 hour
    
    async def start(self):
        """Start background collection."""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._collection_loop())
        self._user_cache_task = asyncio.create_task(self._user_cache_loop())
        self._aggregation_task = asyncio.create_task(self._aggregation_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Xray stats collector started")
    
    async def stop(self):
        """Stop background collection."""
        self._running = False
        
        for task in [self._task, self._user_cache_task, self._aggregation_task, self._cleanup_task]:
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
        """Main collection loop."""
        while self._running:
            try:
                settings = await self._get_settings()
                
                if settings and settings.enabled:
                    self._collection_interval = settings.collection_interval or 60
                    await self._collect_from_all_nodes()
                
                await asyncio.sleep(self._collection_interval)
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Collection error: {e}")
                await asyncio.sleep(60)
    
    async def _collect_from_all_nodes(self):
        """Collect stats from all enabled Remnawave nodes."""
        async with async_session() as db:
            # Get all enabled Remnawave nodes with their server info
            result = await db.execute(
                select(RemnawaveNode, Server)
                .join(Server, RemnawaveNode.server_id == Server.id)
                .where(RemnawaveNode.enabled == True)
                .where(Server.is_active == True)
            )
            nodes = result.all()
        
        if not nodes:
            return
        
        # Collect from each node concurrently
        tasks = [self._collect_from_node(node, server) for node, server in nodes]
        await asyncio.gather(*tasks, return_exceptions=True)
    
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
                
                if stats:
                    await self._save_stats(server.id, stats)
                
                # Update node status
                async with async_session() as db:
                    await db.execute(
                        update(RemnawaveNode)
                        .where(RemnawaveNode.id == node.id)
                        .values(
                            last_collected=datetime.now(timezone.utc).replace(tzinfo=None),
                            last_error=None
                        )
                    )
                    await db.commit()
                
                logger.debug(f"Collected {len(stats)} stat entries from {server.name}")
                
        except Exception as e:
            error_msg = str(e)[:500]
            logger.debug(f"Failed to collect from {server.name}: {error_msg}")
            
            async with async_session() as db:
                await db.execute(
                    update(RemnawaveNode)
                    .where(RemnawaveNode.id == node.id)
                    .values(last_error=error_msg)
                )
                await db.commit()
    
    async def _save_stats(self, server_id: int, stats: list[dict]):
        """Save collected stats to DB (hourly aggregation)."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        
        async with async_session() as db:
            for stat in stats:
                destination = stat.get("destination", "")
                email = stat.get("email", 0)
                count = stat.get("count", 0)
                
                if not destination or not email or not count:
                    continue
                
                # Try to resolve IP to domain
                domain = await self._resolve_domain(destination)
                
                # Upsert: increment count if exists, insert if not
                existing = await db.execute(
                    select(XrayVisitStats).where(
                        XrayVisitStats.server_id == server_id,
                        XrayVisitStats.period_start == hour_start,
                        XrayVisitStats.period_type == 'hour',
                        XrayVisitStats.destination == destination,
                        XrayVisitStats.email == email
                    )
                )
                row = existing.scalar_one_or_none()
                
                if row:
                    row.visit_count += count
                    if domain:
                        row.destination_domain = domain
                else:
                    db.add(XrayVisitStats(
                        server_id=server_id,
                        period_start=hour_start,
                        period_type='hour',
                        destination=destination,
                        destination_domain=domain,
                        email=email,
                        visit_count=count
                    ))
            
            await db.commit()
    
    async def _resolve_domain(self, destination: str) -> Optional[str]:
        """Resolve IP address to domain name (cached)."""
        # Extract host from destination (format: host:port)
        host = destination.rsplit(':', 1)[0] if ':' in destination else destination
        
        # Check if it's already a domain name (not an IP)
        try:
            socket.inet_aton(host)
        except socket.error:
            # Not an IP address, it's already a domain
            return None
        
        # Check cache
        now = datetime.now().timestamp()
        if host in self._dns_cache:
            domain, cached_time = self._dns_cache[host]
            if now - cached_time < self._dns_cache_ttl:
                return domain
        
        # Try reverse DNS lookup
        try:
            domain = socket.gethostbyaddr(host)[0]
            self._dns_cache[host] = (domain, now)
            return domain
        except socket.herror:
            # No reverse DNS
            self._dns_cache[host] = (None, now)
            return None
        except Exception:
            return None
    
    async def _user_cache_loop(self):
        """Background loop for caching Remnawave users."""
        # Initial delay to let things settle
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
        """Update cached Remnawave users."""
        settings = await self._get_settings()
        
        if not settings or not settings.enabled or not settings.api_url or not settings.api_token:
            return
        
        api = get_remnawave_api(settings.api_url, settings.api_token, settings.cookie_secret)
        
        try:
            users = await api.get_all_users_paginated(size=50)
            
            if not users:
                return
            
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            
            async with async_session() as db:
                for user in users:
                    user_id = user.get("id")  # This is the numeric ID (email in logs)
                    if not user_id:
                        continue
                    
                    # Upsert user cache
                    existing = await db.execute(
                        select(RemnawaveUserCache).where(
                            RemnawaveUserCache.email == user_id
                        )
                    )
                    cache_entry = existing.scalar_one_or_none()
                    
                    if cache_entry:
                        cache_entry.uuid = user.get("uuid")
                        cache_entry.username = user.get("username")
                        cache_entry.telegram_id = user.get("telegramId")
                        cache_entry.status = user.get("status")
                        cache_entry.updated_at = now
                    else:
                        db.add(RemnawaveUserCache(
                            email=user_id,
                            uuid=user.get("uuid"),
                            username=user.get("username"),
                            telegram_id=user.get("telegramId"),
                            status=user.get("status"),
                            updated_at=now
                        ))
                
                await db.commit()
            
            logger.info(f"Updated user cache: {len(users)} users")
            
        except RemnawaveAPIError as e:
            logger.warning(f"Failed to update user cache: {e.message}")
        finally:
            await api.close()
    
    async def _aggregation_loop(self):
        """Background loop for daily aggregation."""
        while self._running:
            try:
                await asyncio.sleep(3600)  # Check every hour
                
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                
                # Daily aggregation at midnight
                if now.hour == 0:
                    await self._aggregate_daily()
                    
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Aggregation error: {e}")
    
    async def _aggregate_daily(self):
        """Aggregate hourly stats to daily."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start = day_end - timedelta(days=1)
        
        async with async_session() as db:
            # Get hourly data for yesterday
            result = await db.execute(
                select(
                    XrayVisitStats.server_id,
                    XrayVisitStats.destination,
                    XrayVisitStats.destination_domain,
                    XrayVisitStats.email,
                    sql_func.sum(XrayVisitStats.visit_count).label('total_count')
                )
                .where(
                    XrayVisitStats.period_type == 'hour',
                    XrayVisitStats.period_start >= day_start,
                    XrayVisitStats.period_start < day_end
                )
                .group_by(
                    XrayVisitStats.server_id,
                    XrayVisitStats.destination,
                    XrayVisitStats.destination_domain,
                    XrayVisitStats.email
                )
            )
            
            rows = result.fetchall()
            
            for row in rows:
                # Check if daily record exists
                existing = await db.execute(
                    select(XrayVisitStats).where(
                        XrayVisitStats.server_id == row.server_id,
                        XrayVisitStats.period_start == day_start,
                        XrayVisitStats.period_type == 'day',
                        XrayVisitStats.destination == row.destination,
                        XrayVisitStats.email == row.email
                    )
                )
                
                if not existing.scalar_one_or_none():
                    db.add(XrayVisitStats(
                        server_id=row.server_id,
                        period_start=day_start,
                        period_type='day',
                        destination=row.destination,
                        destination_domain=row.destination_domain,
                        email=row.email,
                        visit_count=row.total_count or 0
                    ))
            
            await db.commit()
        
        logger.info(f"Daily aggregation completed for {day_start.date()}")
    
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
        """Remove old stats data."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        async with async_session() as db:
            # Remove hourly data older than retention period
            hourly_cutoff = now - timedelta(days=self._hourly_retention_days)
            await db.execute(
                delete(XrayVisitStats).where(
                    XrayVisitStats.period_type == 'hour',
                    XrayVisitStats.period_start < hourly_cutoff
                )
            )
            
            # Remove daily data older than retention period
            daily_cutoff = now - timedelta(days=self._daily_retention_days)
            await db.execute(
                delete(XrayVisitStats).where(
                    XrayVisitStats.period_type == 'day',
                    XrayVisitStats.period_start < daily_cutoff
                )
            )
            
            # Remove stale user cache entries (not updated for 7 days)
            cache_cutoff = now - timedelta(days=7)
            await db.execute(
                delete(RemnawaveUserCache).where(
                    RemnawaveUserCache.updated_at < cache_cutoff
                )
            )
            
            await db.commit()
        
        logger.info("Xray stats cleanup completed")


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
