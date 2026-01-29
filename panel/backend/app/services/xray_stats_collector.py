"""
Xray stats collector for Remnawave integration.

Optimized version: stores cumulative counters instead of time-series data.
- XrayVisitStats: (server, destination, email) -> total_count (incremented)
- XrayHourlyStats: (server, hour) -> counts (for timeline charts)
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database import async_session
from app.models import (
    Server, RemnawaveSettings, RemnawaveNode,
    XrayVisitStats, XrayHourlyStats, RemnawaveUserCache
)
from app.services.remnawave_api import get_remnawave_api, RemnawaveAPIError

logger = logging.getLogger(__name__)


class XrayStatsCollector:
    """Collects Xray visit stats from Remnawave nodes."""
    
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._user_cache_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        
        self._collection_interval = 60
        self._user_cache_interval = 3600
        
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
        
        return {
            "success": True,
            "collected_at": self._last_collect_time.isoformat() if self._last_collect_time else None,
            "nodes_count": nodes_count
        }
    
    def get_status(self) -> dict:
        """Get collector status with timing info."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        next_collect_in = None
        if self._running and self._last_collect_time:
            elapsed = (now - self._last_collect_time).total_seconds()
            next_collect_in = max(0, self._collection_interval - int(elapsed))
        elif self._running:
            next_collect_in = self._collection_interval
        
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
                
                if stats:
                    await self._save_stats(server.id, stats)
                
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
        """Save collected stats to DB (increment counters)."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        
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
        
        if not visit_updates:
            return
        
        async with async_session() as db:
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
            
            await db.commit()
        
        logger.debug(f"Saved {len(visit_updates)} stat entries, {total_count} total visits")
    
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
                    user_id = user.get("id")
                    if not user_id:
                        continue
                    
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
        """Remove old hourly stats and stale user cache."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        async with async_session() as db:
            # Remove hourly data older than retention period
            hourly_cutoff = now - timedelta(days=self._hourly_retention_days)
            await db.execute(
                delete(XrayHourlyStats).where(
                    XrayHourlyStats.hour < hourly_cutoff
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
