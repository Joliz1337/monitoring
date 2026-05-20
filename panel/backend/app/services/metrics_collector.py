"""
Background metrics collector for the panel.
Polls all servers every N seconds and stores metrics in local DB.
Calculates network/disk speeds based on byte differences.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select, delete, update, bindparam
from app.services.http_client import get_node_client, node_auth_headers
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import Server, ServerCache, MetricsSnapshot, AggregatedMetrics, PanelSettings
from sqlalchemy import func as sql_func
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)

# Default intervals (used if settings not in DB)
DEFAULT_METRICS_INTERVAL = 10  # seconds (recommended: 10-15s)
DEFAULT_HAPROXY_INTERVAL = 300  # seconds (5 minutes - HAProxy/Traffic data changes rarely)


class ErrorTypes:
    TIMEOUT = "Connection timeout"
    CONNECTION_REFUSED = "Connection refused"
    SSL_ERROR = "SSL certificate error"
    AUTH_ERROR = "Authentication failed"
    SERVER_ERROR = "Server error"
    UNKNOWN = "Unknown error"


class ServerMetricsState:
    """Tracks previous values for speed calculation per server"""
    
    def __init__(self):
        self.prev_net_rx: int = 0
        self.prev_net_tx: int = 0
        self.prev_disk_read: int = 0
        self.prev_disk_write: int = 0
        self.prev_time: float = 0
        self.initialized: bool = False


class MetricsCollector:
    """Collects metrics from all servers and stores in panel DB"""
    
    XRAY_CHECK_INTERVAL = 120
    ACTIVE_REFRESH_INTERVAL = 5
    ACTIVITY_TTL = 60
    
    HTTP_CONCURRENCY = 50  # max parallel HTTP requests to nodes
    DB_CONCURRENCY = 10    # max parallel DB sessions
    CLEANUP_INTERVAL = 300 # cleanup every 5 minutes, not every cycle

    CB_FAILURE_THRESHOLD = 3   # после стольких подряд неудач нода уходит в skip
    CB_SKIP_CYCLES = 3         # пропускать N циклов перед повторной попыткой
    
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._aggregation_task: Optional[asyncio.Task] = None
        self._haproxy_task: Optional[asyncio.Task] = None
        self._settings_task: Optional[asyncio.Task] = None
        self._xray_check_task: Optional[asyncio.Task] = None
        self._active_cache_task: Optional[asyncio.Task] = None
        self._server_states: dict[int, ServerMetricsState] = {}
        self._collect_interval = DEFAULT_METRICS_INTERVAL
        self._haproxy_interval = DEFAULT_HAPROXY_INTERVAL
        self._traffic_period_days = 30
        
        self._active_servers: dict[int, float] = {}
        
        self._raw_retention_hours = 24
        self._hourly_retention_days = 30
        self._daily_retention_days = 365
        
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        self._last_hourly_aggregation: datetime = now_utc - timedelta(hours=2)
        self._last_daily_aggregation: datetime = now_utc - timedelta(days=2)
        self._last_cleanup: float = 0
        
        self._http_sem = asyncio.Semaphore(self.HTTP_CONCURRENCY)
        self._db_sem = asyncio.Semaphore(self.DB_CONCURRENCY)

        self._node_failures: dict[int, int] = {}
        self._node_skip_cycles: dict[int, int] = {}
    
    def notify_activity(self, server_id: int):
        """Mark server as having client activity — triggers fast HAProxy cache refresh (every 5s)"""
        self._active_servers[server_id] = time.time()
    
    async def _load_settings(self):
        """Load collector intervals from database settings"""
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(PanelSettings).where(
                        PanelSettings.key.in_([
                            'metrics_collect_interval', 'haproxy_collect_interval', 'traffic_period'
                        ])
                    )
                )
                settings = {s.key: s.value for s in result.scalars().all()}
                
                if 'metrics_collect_interval' in settings:
                    new_interval = int(settings['metrics_collect_interval'])
                    if 5 <= new_interval <= 300:
                        if new_interval != self._collect_interval:
                            logger.info(f"Metrics interval changed: {self._collect_interval}s -> {new_interval}s")
                            self._collect_interval = new_interval
                
                if 'haproxy_collect_interval' in settings:
                    new_interval = int(settings['haproxy_collect_interval'])
                    if 30 <= new_interval <= 600:
                        if new_interval != self._haproxy_interval:
                            logger.info(f"HAProxy interval changed: {self._haproxy_interval}s -> {new_interval}s")
                            self._haproxy_interval = new_interval
                
                if 'traffic_period' in settings:
                    new_period = int(settings['traffic_period'])
                    if 1 <= new_period <= 365:
                        self._traffic_period_days = new_period
        except Exception as e:
            logger.debug(f"Failed to load collector settings: {e}")
    
    async def _settings_loop(self):
        """Background loop to reload settings periodically"""
        while self._running:
            try:
                await asyncio.sleep(30)  # Check settings every 30 seconds
                await self._load_settings()
            except Exception as e:
                logger.debug(f"Settings reload error: {e}")
    
    async def start(self):
        """Start background collection"""
        if self._running:
            return
        
        # Load settings before starting
        await self._load_settings()
        
        self._running = True
        self._task = asyncio.create_task(self._collection_loop())
        self._aggregation_task = asyncio.create_task(self._aggregation_loop())
        self._haproxy_task = asyncio.create_task(self._haproxy_cache_loop())
        self._settings_task = asyncio.create_task(self._settings_loop())
        self._xray_check_task = asyncio.create_task(self._xray_check_loop())
        self._active_cache_task = asyncio.create_task(self._active_haproxy_cache_loop())
        logger.info(f"Metrics collector started (interval: {self._collect_interval}s, haproxy: {self._haproxy_interval}s)")
    
    async def stop(self):
        """Stop background collection"""
        self._running = False
        for task in [self._task, self._aggregation_task, self._haproxy_task, self._settings_task, self._xray_check_task, self._active_cache_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("Metrics collector stopped")
    
    async def _collection_loop(self):
        """Main collection loop"""
        while self._running:
            try:
                await self._collect_all_servers()
            except Exception as e:
                logger.error(f"Collection error: {e}")
            
            await asyncio.sleep(self._collect_interval)
    
    async def _collect_all_servers(self):
        """Collect metrics from all active servers with bounded concurrency."""
        async with async_session() as db:
            result = await db.execute(
                select(Server).where(Server.is_active == True)
            )
            servers = result.scalars().all()
        
        # Phase 1: fetch metrics from all servers (HTTP-bound, limited by semaphore)
        results = await asyncio.gather(
            *[self._fetch_server_metrics(server) for server in servers],
            return_exceptions=True
        )
        
        # Phase 2: batch write all results in a single DB session
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        snapshots = []
        server_updates = []
        
        for server, result in zip(servers, results):
            if isinstance(result, Exception):
                continue
            
            metrics, error_info = result
            if metrics:
                snapshot_data = self._build_snapshot(server.id, metrics, now_utc)
                if snapshot_data:
                    snapshots.append(snapshot_data)
                server_updates.append({
                    "id": server.id,
                    "last_seen": now_utc,
                    "last_error": None,
                    "error_code": None,
                    "last_metrics": json.dumps(metrics)
                })
            elif error_info:
                server_updates.append({
                    "id": server.id,
                    "last_error": error_info["message"],
                    "error_code": error_info["code"]
                })
        
        if snapshots or server_updates:
            success_updates = [u for u in server_updates if "last_seen" in u]
            error_updates = [u for u in server_updates if "last_seen" not in u]

            async with async_session() as db:
                if snapshots:
                    await db.execute(MetricsSnapshot.__table__.insert(), snapshots)

                # Bulk UPDATE через connection() — обходит ORM bulk-by-PK путь,
                # который требует id внутри values; нам нужен WHERE с bindparam.
                if success_updates or error_updates:
                    conn = await db.connection()

                if success_updates:
                    success_params = [
                        {
                            "sid": upd["id"],
                            "last_seen": upd["last_seen"],
                            "last_error": upd["last_error"],
                            "error_code": upd["error_code"],
                            "last_metrics": upd["last_metrics"],
                        }
                        for upd in success_updates
                    ]
                    await conn.execute(
                        update(Server)
                        .where(Server.id == bindparam("sid"))
                        .values(
                            last_seen=bindparam("last_seen"),
                            last_error=bindparam("last_error"),
                            error_code=bindparam("error_code"),
                            last_metrics=bindparam("last_metrics"),
                        ),
                        success_params,
                    )

                if error_updates:
                    error_params = [
                        {
                            "sid": upd["id"],
                            "last_error": upd["last_error"],
                            "error_code": upd["error_code"],
                        }
                        for upd in error_updates
                    ]
                    await conn.execute(
                        update(Server)
                        .where(Server.id == bindparam("sid"))
                        .values(
                            last_error=bindparam("last_error"),
                            error_code=bindparam("error_code"),
                        ),
                        error_params,
                    )

                await db.commit()
        
        # Cleanup throttled — only every CLEANUP_INTERVAL seconds
        now = time.time()
        if now - self._last_cleanup >= self.CLEANUP_INTERVAL:
            async with async_session() as db:
                await self._cleanup_old_data(db)
            self._last_cleanup = now
    
    async def _fetch_server_metrics(self, server: Server) -> tuple[Optional[dict], Optional[dict]]:
        """Fetch metrics from a single server with HTTP semaphore + circuit breaker."""
        # Circuit breaker: пропускаем нескольких циклов для сбоящих нод
        skip_left = self._node_skip_cycles.get(server.id, 0)
        if skip_left > 0:
            self._node_skip_cycles[server.id] = skip_left - 1
            return None, {"message": ErrorTypes.TIMEOUT, "code": 504}

        async with self._http_sem:
            result = await self._fetch_metrics(server)

        metrics, error_info = result
        if metrics is not None:
            if self._node_failures.pop(server.id, 0):
                self._node_skip_cycles.pop(server.id, None)
        elif error_info is not None:
            failures = self._node_failures.get(server.id, 0) + 1
            self._node_failures[server.id] = failures
            if failures >= self.CB_FAILURE_THRESHOLD:
                self._node_skip_cycles[server.id] = self.CB_SKIP_CYCLES
        return result

    async def _fetch_metrics(self, server: Server) -> tuple[Optional[dict], Optional[dict]]:
        """Fetch current metrics from server API. Returns (metrics, error_info)"""
        url = f"{server.url}/api/metrics"

        try:
            client = get_node_client(server)
            # Таймаут берётся из клиента (_NODE_TIMEOUT = 2/5/2/2) — без лишних аллокаций.
            response = await client.get(url, headers=node_auth_headers(server))
            if response.status_code == 200:
                return response.json(), None
            elif response.status_code == 401 or response.status_code == 403:
                return None, {"message": ErrorTypes.AUTH_ERROR, "code": response.status_code}
            else:
                return None, {"message": f"{ErrorTypes.SERVER_ERROR}: HTTP {response.status_code}", "code": response.status_code}
        except httpx.TimeoutException:
            logger.debug(f"Timeout connecting to {server.name}")
            return None, {"message": ErrorTypes.TIMEOUT, "code": 504}
        except httpx.ConnectError as e:
            error_str = str(e).lower()
            if "refused" in error_str:
                return None, {"message": ErrorTypes.CONNECTION_REFUSED, "code": 502}
            elif "ssl" in error_str or "certificate" in error_str:
                return None, {"message": ErrorTypes.SSL_ERROR, "code": 495}
            return None, {"message": f"{ErrorTypes.CONNECTION_REFUSED}: {str(e)[:100]}", "code": 502}
        except Exception as e:
            logger.debug(f"Request to {server.name} failed: {e}")
            return None, {"message": f"{ErrorTypes.UNKNOWN}: {str(e)[:100]}", "code": 500}
    
    def _build_snapshot(self, server_id: int, metrics: dict, now_utc: datetime) -> Optional[dict]:
        """Build a snapshot dict for batch insert. Returns None if data invalid."""
        current_time = time.time()
        
        if server_id not in self._server_states:
            self._server_states[server_id] = ServerMetricsState()
        
        state = self._server_states[server_id]
        
        cpu = metrics.get("cpu", {})
        memory = metrics.get("memory", {}).get("ram", {})
        swap = metrics.get("memory", {}).get("swap", {})
        network = metrics.get("network", {}).get("total", {})
        disk = metrics.get("disk", {})
        processes = metrics.get("processes", {})
        system = metrics.get("system", {})
        
        net_rx = network.get("rx_bytes", 0)
        net_tx = network.get("tx_bytes", 0)
        disk_read = sum(d.get("read_bytes", 0) for d in disk.get("io", {}).values())
        disk_write = sum(d.get("write_bytes", 0) for d in disk.get("io", {}).values())
        
        net_rx_speed = 0.0
        net_tx_speed = 0.0
        disk_read_speed = 0.0
        disk_write_speed = 0.0
        
        if state.initialized and state.prev_time > 0:
            dt = current_time - state.prev_time
            if dt > 0.5:
                rx_diff = net_rx - state.prev_net_rx
                tx_diff = net_tx - state.prev_net_tx
                if rx_diff >= 0:
                    net_rx_speed = rx_diff / dt
                if tx_diff >= 0:
                    net_tx_speed = tx_diff / dt
                
                read_diff = disk_read - state.prev_disk_read
                write_diff = disk_write - state.prev_disk_write
                if read_diff >= 0:
                    disk_read_speed = read_diff / dt
                if write_diff >= 0:
                    disk_write_speed = write_diff / dt
        
        state.prev_net_rx = net_rx
        state.prev_net_tx = net_tx
        state.prev_disk_read = disk_read
        state.prev_disk_write = disk_write
        state.prev_time = current_time
        state.initialized = True
        
        disk_percent = 0.0
        partitions = disk.get("partitions", [])
        if partitions:
            disk_percent = partitions[0].get("percent", 0)
        
        connections = system.get("connections", {})
        connections_count = connections.get("established", 0) + connections.get("listen", 0)
        connections_detailed = system.get("connections_detailed", {})
        tcp_states = connections_detailed.get("tcp", {})
        per_cpu = cpu.get("per_cpu_percent", [])
        
        return {
            "server_id": server_id,
            "timestamp": now_utc,
            "cpu_usage": cpu.get("usage_percent", 0),
            "load_avg_1": cpu.get("load_avg_1", 0),
            "load_avg_5": cpu.get("load_avg_5", 0),
            "load_avg_15": cpu.get("load_avg_15", 0),
            "memory_total": memory.get("total", 0),
            "memory_used": memory.get("used", 0),
            "memory_available": memory.get("available", 0),
            "memory_percent": memory.get("percent", 0),
            "swap_used": swap.get("used", 0),
            "swap_percent": swap.get("percent", 0),
            "net_rx_bytes_per_sec": net_rx_speed,
            "net_tx_bytes_per_sec": net_tx_speed,
            "net_rx_bytes": net_rx,
            "net_tx_bytes": net_tx,
            "disk_percent": disk_percent,
            "disk_read_bytes_per_sec": disk_read_speed,
            "disk_write_bytes_per_sec": disk_write_speed,
            "process_count": processes.get("total", 0),
            "connections_count": connections_count,
            "tcp_established": tcp_states.get("established"),
            "tcp_listen": tcp_states.get("listen"),
            "tcp_time_wait": tcp_states.get("time_wait"),
            "tcp_close_wait": tcp_states.get("close_wait"),
            "tcp_syn_sent": tcp_states.get("syn_sent"),
            "tcp_syn_recv": tcp_states.get("syn_recv") if tcp_states.get("syn_recv") is not None else tcp_states.get("syn_received"),
            "tcp_fin_wait": tcp_states.get("fin_wait"),
            "per_cpu_percent": json.dumps(per_cpu) if per_cpu else None,
        }
    
    async def _cleanup_old_data(self, db: AsyncSession):
        """Remove data older than retention periods.
        
        Note: We use naive UTC datetime for consistent comparisons across platforms.
        """
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        
        # Cleanup raw data (24 hours)
        raw_cutoff = now_utc - timedelta(hours=self._raw_retention_hours)
        await db.execute(
            delete(MetricsSnapshot).where(MetricsSnapshot.timestamp < raw_cutoff)
        )
        
        # Cleanup hourly aggregated data (30 days)
        hourly_cutoff = now_utc - timedelta(days=self._hourly_retention_days)
        await db.execute(
            delete(AggregatedMetrics).where(
                AggregatedMetrics.period_type == 'hour',
                AggregatedMetrics.timestamp < hourly_cutoff
            )
        )
        
        # Cleanup daily aggregated data (365 days)
        daily_cutoff = now_utc - timedelta(days=self._daily_retention_days)
        await db.execute(
            delete(AggregatedMetrics).where(
                AggregatedMetrics.period_type == 'day',
                AggregatedMetrics.timestamp < daily_cutoff
            )
        )
        
        await db.commit()
    
    async def _aggregation_loop(self):
        """Background loop for data aggregation"""
        while self._running:
            try:
                await asyncio.sleep(60)  # Check every minute
                
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                
                # Hourly aggregation (run at the start of each hour)
                if now - self._last_hourly_aggregation >= timedelta(hours=1):
                    async with async_session() as db:
                        await self._aggregate_hourly(db)
                    self._last_hourly_aggregation = now.replace(minute=0, second=0, microsecond=0)
                
                # Daily aggregation (run once per day at midnight)
                if now - self._last_daily_aggregation >= timedelta(days=1):
                    async with async_session() as db:
                        await self._aggregate_daily(db)
                    self._last_daily_aggregation = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    
            except Exception as e:
                logger.error(f"Aggregation error: {e}")
    
    async def _haproxy_cache_loop(self):
        """Background loop for caching HAProxy and Traffic data"""
        while self._running:
            try:
                await asyncio.sleep(self._haproxy_interval)
                await self._cache_haproxy_traffic_data()
            except Exception as e:
                logger.error(f"HAProxy/Traffic cache error: {e}")
    
    async def _active_haproxy_cache_loop(self):
        """Fast refresh loop (every 5s) for servers with recent client activity.
        Only fetches HAProxy data (no traffic) to keep it lightweight."""
        while self._running:
            try:
                await asyncio.sleep(self.ACTIVE_REFRESH_INTERVAL)
                
                now = time.time()
                expired = [sid for sid, ts in self._active_servers.items()
                           if now - ts > self.ACTIVITY_TTL]
                for sid in expired:
                    del self._active_servers[sid]
                
                if not self._active_servers:
                    continue
                
                active_ids = list(self._active_servers.keys())
                async with async_session() as db:
                    result = await db.execute(
                        select(Server).where(Server.id.in_(active_ids), Server.is_active == True)
                    )
                    servers = result.scalars().all()
                
                if servers:
                    tasks = [self._cache_server_haproxy_only(s) for s in servers]
                    await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as e:
                logger.error(f"Active HAProxy cache error: {e}")
    
    async def _cache_server_haproxy_only(self, server: Server):
        """Lightweight HAProxy-only cache refresh (no traffic data)"""
        try:
            async with self._http_sem:
                client = get_node_client(server)
                headers = node_auth_headers(server)
                haproxy_data = {}

                for endpoint, key in [
                    ("/api/haproxy/status", "status"),
                    ("/api/haproxy/rules", "rules"),
                    ("/api/haproxy/certs/all", "certs"),
                    ("/api/haproxy/firewall/rules", "firewall"),
                ]:
                    try:
                        res = await client.get(f"{server.url}{endpoint}", headers=headers, timeout=10.0)
                        if res.status_code == 200:
                            haproxy_data[key] = res.json()
                    except Exception:
                        pass

                if not haproxy_data:
                    return
            
            # Load existing cache from server_cache table
            async with self._db_sem:
                async with async_session() as db:
                    result = await db.execute(
                        select(ServerCache).where(ServerCache.server_id == server.id)
                    )
                    cache_row = result.scalar_one_or_none()
                    
                    existing = {}
                    if cache_row and cache_row.last_haproxy_data:
                        try:
                            existing = json.loads(cache_row.last_haproxy_data)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    existing.update(haproxy_data)
                    existing["cached_at"] = datetime.now(timezone.utc).isoformat()
                    
                    haproxy_json = json.dumps(existing)
                    stmt = pg_insert(ServerCache).values(
                        server_id=server.id,
                        last_haproxy_data=haproxy_json
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=['server_id'],
                        set_={'last_haproxy_data': haproxy_json}
                    )
                    await db.execute(stmt)
                    await db.commit()
        except Exception as e:
            logger.debug(f"Failed to fast-refresh HAProxy for {server.name}: {e}")
    
    async def _cache_haproxy_traffic_data(self):
        """Cache HAProxy status/rules/certs and Traffic summary for all servers"""
        async with async_session() as db:
            result = await db.execute(
                select(Server).where(Server.is_active == True)
            )
            servers = result.scalars().all()
        
        # Each server uses its own session
        tasks = [self._cache_server_haproxy_traffic(server) for server in servers]
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _cache_server_haproxy_traffic(self, server: Server):
        """Cache HAProxy and Traffic data for a single server into server_cache table."""
        try:
            haproxy_data = {}
            traffic_data = {}
            
            async with self._http_sem:
                client = get_node_client(server)
                headers = node_auth_headers(server)

                for endpoint, key in [
                    ("/api/haproxy/status", "status"),
                    ("/api/haproxy/rules", "rules"),
                    ("/api/haproxy/certs/all", "certs"),
                    ("/api/haproxy/firewall/rules", "firewall"),
                ]:
                    try:
                        res = await client.get(f"{server.url}{endpoint}", headers=headers, timeout=15.0)
                        if res.status_code == 200:
                            haproxy_data[key] = res.json()
                    except Exception:
                        pass

                for endpoint, key, params in [
                    ("/api/traffic/summary", "summary", {"days": self._traffic_period_days}),
                    ("/api/traffic/ports/tracked", "tracked_ports", {}),
                    ("/api/traffic/hourly", "hourly", {"hours": 24}),
                    ("/api/traffic/daily", "daily", {"days": self._traffic_period_days}),
                    ("/api/traffic/monthly", "monthly", {"months": 12}),
                ]:
                    try:
                        res = await client.get(
                            f"{server.url}{endpoint}", headers=headers, params=params, timeout=15.0,
                        )
                        if res.status_code == 200:
                            traffic_data[key] = res.json()
                    except Exception:
                        pass
            
            if not haproxy_data and not traffic_data:
                return
            
            async with self._db_sem:
                async with async_session() as db:
                    result = await db.execute(
                        select(ServerCache).where(ServerCache.server_id == server.id)
                    )
                    cache_row = result.scalar_one_or_none()
                    
                    haproxy_json = None
                    traffic_json = None
                    
                    if haproxy_data:
                        existing = {}
                        if cache_row and cache_row.last_haproxy_data:
                            try:
                                existing = json.loads(cache_row.last_haproxy_data)
                            except (json.JSONDecodeError, TypeError):
                                pass
                        existing.update(haproxy_data)
                        existing["cached_at"] = datetime.now(timezone.utc).isoformat()
                        haproxy_json = json.dumps(existing)
                    
                    if traffic_data:
                        existing = {}
                        if cache_row and cache_row.last_traffic_data:
                            try:
                                existing = json.loads(cache_row.last_traffic_data)
                            except (json.JSONDecodeError, TypeError):
                                pass
                        existing.update(traffic_data)
                        existing["cached_at"] = datetime.now(timezone.utc).isoformat()
                        traffic_json = json.dumps(existing)
                    
                    values = {"server_id": server.id}
                    set_clause = {}
                    if haproxy_json:
                        values["last_haproxy_data"] = haproxy_json
                        set_clause["last_haproxy_data"] = haproxy_json
                    if traffic_json:
                        values["last_traffic_data"] = traffic_json
                        set_clause["last_traffic_data"] = traffic_json
                    
                    stmt = pg_insert(ServerCache).values(**values)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=['server_id'],
                        set_=set_clause
                    )
                    await db.execute(stmt)
                    await db.commit()
                    
        except Exception as e:
            logger.debug(f"Failed to cache HAProxy/Traffic for {server.name}: {e}")
    
    async def _xray_check_loop(self):
        """Periodically check which servers have a running remnanode container."""
        await asyncio.sleep(10)  # Initial delay to let servers come online
        while self._running:
            try:
                await self._check_xray_on_all_servers()
            except Exception as e:
                logger.error(f"Xray check error: {e}")
            await asyncio.sleep(self.XRAY_CHECK_INTERVAL)
    
    async def _check_xray_on_all_servers(self):
        """Check xray availability on every active server, batch update has_xray_node."""
        async with async_session() as db:
            result = await db.execute(
                select(Server).where(Server.is_active == True)
            )
            servers = result.scalars().all()
        
        if not servers:
            return
        
        async def _probe(server: Server) -> tuple[int, bool]:
            async with self._http_sem:
                try:
                    client = get_node_client(server)
                    resp = await client.get(
                        f"{server.url}/api/remnawave/status",
                        headers=node_auth_headers(server),
                        timeout=12.0,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        return server.id, bool(data.get("available", False))
                except Exception:
                    pass
            return server.id, False
        
        results = await asyncio.gather(*[_probe(s) for s in servers], return_exceptions=True)
        
        # Batch update changed servers in one session
        changes = []
        for server, r in zip(servers, results):
            if isinstance(r, tuple):
                sid, available = r
                if available != server.has_xray_node:
                    changes.append((sid, available))
        
        if changes:
            try:
                async with async_session() as db:
                    for sid, new_val in changes:
                        await db.execute(
                            update(Server).where(Server.id == sid).values(has_xray_node=new_val)
                        )
                    await db.commit()
                for sid, new_val in changes:
                    logger.info(f"Server {sid}: has_xray_node = {new_val}")
            except Exception as e:
                logger.warning(f"Failed to batch update has_xray_node: {e}")
    
    async def _aggregate_hourly(self, db: AsyncSession):
        """Aggregate raw metrics to hourly summaries — single SQL for all servers."""
        from sqlalchemy import text
        
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        hour_end = now.replace(minute=0, second=0, microsecond=0)
        hour_start = hour_end - timedelta(hours=1)
        
        await db.execute(text("""
            INSERT INTO aggregated_metrics (
                server_id, timestamp, period_type,
                avg_cpu, max_cpu, avg_load,
                avg_memory_percent, max_memory_percent, avg_disk_percent,
                total_rx_bytes, total_tx_bytes, avg_rx_speed, avg_tx_speed,
                avg_disk_read_speed, avg_disk_write_speed,
                avg_tcp_established, avg_tcp_listen, avg_tcp_time_wait,
                avg_tcp_close_wait, avg_tcp_syn_sent, avg_tcp_syn_recv,
                avg_tcp_fin_wait, data_points
            )
            SELECT
                server_id, :hour_start, 'hour',
                AVG(cpu_usage), MAX(cpu_usage), AVG(load_avg_1),
                AVG(memory_percent), MAX(memory_percent), AVG(disk_percent),
                COALESCE(SUM(net_rx_bytes_per_sec * 10), 0)::BIGINT,
                COALESCE(SUM(net_tx_bytes_per_sec * 10), 0)::BIGINT,
                AVG(net_rx_bytes_per_sec), AVG(net_tx_bytes_per_sec),
                AVG(disk_read_bytes_per_sec), AVG(disk_write_bytes_per_sec),
                AVG(tcp_established), AVG(tcp_listen), AVG(tcp_time_wait),
                AVG(tcp_close_wait), AVG(tcp_syn_sent), AVG(tcp_syn_recv),
                AVG(tcp_fin_wait), COUNT(*)
            FROM metrics_snapshots
            WHERE timestamp >= :hour_start AND timestamp < :hour_end
            GROUP BY server_id
            ON CONFLICT DO NOTHING
        """), {"hour_start": hour_start, "hour_end": hour_end})
        
        await db.commit()
        logger.info(f"Hourly aggregation completed for {hour_start}")
    
    async def _aggregate_daily(self, db: AsyncSession):
        """Aggregate hourly metrics to daily summaries — single SQL for all servers."""
        from sqlalchemy import text
        
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start = day_end - timedelta(days=1)
        
        await db.execute(text("""
            INSERT INTO aggregated_metrics (
                server_id, timestamp, period_type,
                avg_cpu, max_cpu, avg_load,
                avg_memory_percent, max_memory_percent, avg_disk_percent,
                total_rx_bytes, total_tx_bytes, avg_rx_speed, avg_tx_speed,
                avg_disk_read_speed, avg_disk_write_speed,
                avg_tcp_established, avg_tcp_listen, avg_tcp_time_wait,
                avg_tcp_close_wait, avg_tcp_syn_sent, avg_tcp_syn_recv,
                avg_tcp_fin_wait, data_points
            )
            SELECT
                server_id, :day_start, 'day',
                AVG(avg_cpu), MAX(max_cpu), AVG(avg_load),
                AVG(avg_memory_percent), MAX(max_memory_percent), AVG(avg_disk_percent),
                COALESCE(SUM(total_rx_bytes), 0), COALESCE(SUM(total_tx_bytes), 0),
                AVG(avg_rx_speed), AVG(avg_tx_speed),
                AVG(avg_disk_read_speed), AVG(avg_disk_write_speed),
                AVG(avg_tcp_established), AVG(avg_tcp_listen), AVG(avg_tcp_time_wait),
                AVG(avg_tcp_close_wait), AVG(avg_tcp_syn_sent), AVG(avg_tcp_syn_recv),
                AVG(avg_tcp_fin_wait), SUM(data_points)
            FROM aggregated_metrics
            WHERE period_type = 'hour' AND timestamp >= :day_start AND timestamp < :day_end
            GROUP BY server_id
            ON CONFLICT DO NOTHING
        """), {"day_start": day_start, "day_end": day_end})
        
        await db.commit()
        logger.info(f"Daily aggregation completed for {day_start}")


# Singleton instance
_collector: Optional[MetricsCollector] = None


def get_collector() -> MetricsCollector:
    """Get or create metrics collector instance"""
    global _collector
    if _collector is None:
        _collector = MetricsCollector()
    return _collector


async def start_collector():
    """Start the metrics collector"""
    collector = get_collector()
    await collector.start()


async def stop_collector():
    """Stop the metrics collector"""
    collector = get_collector()
    await collector.stop()
