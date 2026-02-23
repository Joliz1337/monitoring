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
from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import Server, MetricsSnapshot, AggregatedMetrics, PanelSettings
from sqlalchemy import func as sql_func

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
    
    XRAY_CHECK_INTERVAL = 120  # Check xray availability every 2 minutes
    
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._aggregation_task: Optional[asyncio.Task] = None
        self._haproxy_task: Optional[asyncio.Task] = None
        self._settings_task: Optional[asyncio.Task] = None
        self._xray_check_task: Optional[asyncio.Task] = None
        self._server_states: dict[int, ServerMetricsState] = {}
        self._collect_interval = DEFAULT_METRICS_INTERVAL
        self._haproxy_interval = DEFAULT_HAPROXY_INTERVAL
        self._traffic_period_days = 30
        
        # Retention periods
        self._raw_retention_hours = 24  # keep raw data 24 hours
        self._hourly_retention_days = 30  # keep hourly data 30 days  
        self._daily_retention_days = 365  # keep daily data 365 days
        
        # Track last aggregation times (naive UTC for consistency)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        self._last_hourly_aggregation: datetime = now_utc - timedelta(hours=2)
        self._last_daily_aggregation: datetime = now_utc - timedelta(days=2)
    
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
        logger.info(f"Metrics collector started (interval: {self._collect_interval}s, haproxy: {self._haproxy_interval}s)")
    
    async def stop(self):
        """Stop background collection"""
        self._running = False
        for task in [self._task, self._aggregation_task, self._haproxy_task, self._settings_task, self._xray_check_task]:
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
        """Collect metrics from all active servers"""
        async with async_session() as db:
            # Get all active servers
            result = await db.execute(
                select(Server).where(Server.is_active == True)
            )
            servers = result.scalars().all()
        
        # Collect from each server concurrently (each with its own session)
        tasks = [self._collect_server(server) for server in servers]
        await asyncio.gather(*tasks, return_exceptions=True)
        
        # Cleanup old data periodically
        async with async_session() as db:
            await self._cleanup_old_data(db)
    
    async def _collect_server(self, server: Server):
        """Collect metrics from a single server (uses own DB session).
        
        Retries on deadlock to handle concurrent writes from xray check loop.
        """
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                metrics, error_info = await self._fetch_metrics(server)
                
                async with async_session() as db:
                    if metrics:
                        await self._save_metrics(server.id, metrics, db)
                        await db.execute(
                            update(Server).where(Server.id == server.id).values(
                                last_seen=datetime.now(timezone.utc).replace(tzinfo=None),
                                last_error=None,
                                error_code=None,
                                last_metrics=json.dumps(metrics)
                            )
                        )
                    elif error_info:
                        await db.execute(
                            update(Server).where(Server.id == server.id).values(
                                last_error=error_info["message"],
                                error_code=error_info["code"]
                            )
                        )
                    await db.commit()
                return
            except Exception as e:
                is_deadlock = "deadlock" in str(e).lower()
                if is_deadlock and attempt < max_retries:
                    logger.debug(f"Deadlock collecting {server.name} (attempt {attempt}), retrying...")
                    await asyncio.sleep(0.3 * attempt)
                    continue
                logger.debug(f"Failed to collect from {server.name}: {e}")
                return
    
    async def _fetch_metrics(self, server: Server) -> tuple[Optional[dict], Optional[dict]]:
        """Fetch current metrics from server API. Returns (metrics, error_info)"""
        url = f"{server.url}/api/metrics"
        
        try:
            async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
                response = await client.get(
                    url,
                    headers={"X-API-Key": server.api_key}
                )
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
    
    async def _save_metrics(self, server_id: int, metrics: dict, db: AsyncSession):
        """Save metrics snapshot with calculated speeds"""
        current_time = time.time()
        
        # Get or create server state
        if server_id not in self._server_states:
            self._server_states[server_id] = ServerMetricsState()
        
        state = self._server_states[server_id]
        
        # Extract values from metrics
        cpu = metrics.get("cpu", {})
        memory = metrics.get("memory", {}).get("ram", {})
        swap = metrics.get("memory", {}).get("swap", {})
        network = metrics.get("network", {}).get("total", {})
        disk = metrics.get("disk", {})
        processes = metrics.get("processes", {})
        system = metrics.get("system", {})
        
        # Current network bytes
        net_rx = network.get("rx_bytes", 0)
        net_tx = network.get("tx_bytes", 0)
        
        # Current disk bytes (sum all disks)
        disk_read = sum(d.get("read_bytes", 0) for d in disk.get("io", {}).values())
        disk_write = sum(d.get("write_bytes", 0) for d in disk.get("io", {}).values())
        
        # Calculate speeds
        net_rx_speed = 0.0
        net_tx_speed = 0.0
        disk_read_speed = 0.0
        disk_write_speed = 0.0
        
        if state.initialized and state.prev_time > 0:
            dt = current_time - state.prev_time
            if dt > 0.5:  # At least 500ms between measurements
                # Network speed
                rx_diff = net_rx - state.prev_net_rx
                tx_diff = net_tx - state.prev_net_tx
                if rx_diff >= 0:  # Handle counter reset
                    net_rx_speed = rx_diff / dt
                if tx_diff >= 0:
                    net_tx_speed = tx_diff / dt
                
                # Disk speed
                read_diff = disk_read - state.prev_disk_read
                write_diff = disk_write - state.prev_disk_write
                if read_diff >= 0:
                    disk_read_speed = read_diff / dt
                if write_diff >= 0:
                    disk_write_speed = write_diff / dt
        
        # Update state for next calculation
        state.prev_net_rx = net_rx
        state.prev_net_tx = net_tx
        state.prev_disk_read = disk_read
        state.prev_disk_write = disk_write
        state.prev_time = current_time
        state.initialized = True
        
        # Get main disk percent
        disk_percent = 0.0
        partitions = disk.get("partitions", [])
        if partitions:
            disk_percent = partitions[0].get("percent", 0)
        
        # Get connections count and detailed TCP states
        connections = system.get("connections", {})
        connections_count = connections.get("established", 0) + connections.get("listen", 0)
        
        connections_detailed = system.get("connections_detailed", {})
        tcp_states = connections_detailed.get("tcp", {})
        
        # Get per-CPU percentages
        per_cpu = cpu.get("per_cpu_percent", [])
        
        # Create snapshot with naive UTC timestamp (stored without timezone)
        snapshot = MetricsSnapshot(
            server_id=server_id,
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            cpu_usage=cpu.get("usage_percent", 0),
            load_avg_1=cpu.get("load_avg_1", 0),
            load_avg_5=cpu.get("load_avg_5", 0),
            load_avg_15=cpu.get("load_avg_15", 0),
            memory_total=memory.get("total", 0),
            memory_used=memory.get("used", 0),
            memory_available=memory.get("available", 0),
            memory_percent=memory.get("percent", 0),
            swap_used=swap.get("used", 0),
            swap_percent=swap.get("percent", 0),
            net_rx_bytes_per_sec=net_rx_speed,
            net_tx_bytes_per_sec=net_tx_speed,
            net_rx_bytes=net_rx,
            net_tx_bytes=net_tx,
            disk_percent=disk_percent,
            disk_read_bytes_per_sec=disk_read_speed,
            disk_write_bytes_per_sec=disk_write_speed,
            process_count=processes.get("total", 0),
            connections_count=connections_count,
            tcp_established=tcp_states.get("established"),
            tcp_listen=tcp_states.get("listen"),
            tcp_time_wait=tcp_states.get("time_wait"),
            tcp_close_wait=tcp_states.get("close_wait"),
            tcp_syn_sent=tcp_states.get("syn_sent"),
            tcp_syn_recv=tcp_states.get("syn_recv") if tcp_states.get("syn_recv") is not None else tcp_states.get("syn_received"),
            tcp_fin_wait=tcp_states.get("fin_wait"),
            per_cpu_percent=json.dumps(per_cpu) if per_cpu else None
        )
        
        db.add(snapshot)
    
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
        """Background loop for caching HAProxy and Traffic data (every 30 seconds)"""
        while self._running:
            try:
                await asyncio.sleep(self._haproxy_interval)
                await self._cache_haproxy_traffic_data()
            except Exception as e:
                logger.error(f"HAProxy/Traffic cache error: {e}")
    
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
        """Cache HAProxy and Traffic data for a single server (uses own DB session)"""
        try:
            async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
                headers = {"X-API-Key": server.api_key}
                haproxy_data = {}
                traffic_data = {}
                
                # Fetch HAProxy status
                try:
                    status_res = await client.get(
                        f"{server.url}/api/haproxy/status",
                        headers=headers
                    )
                    if status_res.status_code == 200:
                        haproxy_data["status"] = status_res.json()
                except Exception:
                    pass
                
                # Fetch HAProxy rules
                try:
                    rules_res = await client.get(
                        f"{server.url}/api/haproxy/rules",
                        headers=headers
                    )
                    if rules_res.status_code == 200:
                        haproxy_data["rules"] = rules_res.json()
                except Exception:
                    pass
                
                # Fetch HAProxy certs (all in one request)
                try:
                    certs_res = await client.get(
                        f"{server.url}/api/haproxy/certs/all",
                        headers=headers
                    )
                    if certs_res.status_code == 200:
                        haproxy_data["certs"] = certs_res.json()
                except Exception:
                    pass
                
                # Fetch Firewall rules
                try:
                    fw_res = await client.get(
                        f"{server.url}/api/haproxy/firewall/rules",
                        headers=headers
                    )
                    if fw_res.status_code == 200:
                        haproxy_data["firewall"] = fw_res.json()
                except Exception:
                    pass
                
                # Fetch Traffic summary
                try:
                    traffic_res = await client.get(
                        f"{server.url}/api/traffic/summary",
                        headers=headers,
                        params={"days": self._traffic_period_days}
                    )
                    if traffic_res.status_code == 200:
                        traffic_data["summary"] = traffic_res.json()
                except Exception:
                    pass
                
                # Fetch tracked ports
                try:
                    ports_res = await client.get(
                        f"{server.url}/api/traffic/ports/tracked",
                        headers=headers
                    )
                    if ports_res.status_code == 200:
                        traffic_data["tracked_ports"] = ports_res.json()
                except Exception:
                    pass
                
                # Update cache in DB (own session)
                update_values = {}
                if haproxy_data:
                    haproxy_data["cached_at"] = datetime.now(timezone.utc).isoformat()
                    update_values["last_haproxy_data"] = json.dumps(haproxy_data)
                if traffic_data:
                    traffic_data["cached_at"] = datetime.now(timezone.utc).isoformat()
                    update_values["last_traffic_data"] = json.dumps(traffic_data)
                
                if update_values:
                    for attempt in range(1, 4):
                        try:
                            async with async_session() as db:
                                await db.execute(
                                    update(Server).where(Server.id == server.id).values(**update_values)
                                )
                                await db.commit()
                            break
                        except Exception as db_err:
                            if "deadlock" in str(db_err).lower() and attempt < 3:
                                await asyncio.sleep(0.3 * attempt)
                                continue
                            raise
                    
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
        """Check xray availability on every active server, update has_xray_node."""
        async with async_session() as db:
            result = await db.execute(
                select(Server).where(Server.is_active == True)
            )
            servers = result.scalars().all()
        
        if not servers:
            return
        
        async def _probe(server: Server) -> tuple[int, bool]:
            try:
                async with httpx.AsyncClient(verify=False, timeout=12.0) as client:
                    resp = await client.get(
                        f"{server.url}/api/remnawave/status",
                        headers={"X-API-Key": server.api_key}
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        return server.id, bool(data.get("available", False))
            except Exception:
                pass
            return server.id, False
        
        results = await asyncio.gather(*[_probe(s) for s in servers], return_exceptions=True)
        
        updates: dict[int, bool] = {}
        for r in results:
            if isinstance(r, tuple):
                sid, available = r
                updates[sid] = available
        
        if not updates:
            return
        
        for server in servers:
            new_val = updates.get(server.id)
            if new_val is not None and new_val != server.has_xray_node:
                try:
                    async with async_session() as db:
                        await db.execute(
                            update(Server).where(Server.id == server.id).values(
                                has_xray_node=new_val
                            )
                        )
                        await db.commit()
                    logger.info(f"Server {server.name}: has_xray_node = {new_val}")
                except Exception as e:
                    logger.warning(f"Failed to update has_xray_node for {server.name}: {e}")
    
    async def _aggregate_hourly(self, db: AsyncSession):
        """Aggregate raw metrics to hourly summaries"""
        # Get all servers
        result = await db.execute(select(Server.id))
        server_ids = [row[0] for row in result.fetchall()]
        
        # Aggregate last complete hour (use naive UTC)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        hour_end = now.replace(minute=0, second=0, microsecond=0)
        hour_start = hour_end - timedelta(hours=1)
        
        for server_id in server_ids:
            # Check if already aggregated
            existing = await db.execute(
                select(AggregatedMetrics).where(
                    AggregatedMetrics.server_id == server_id,
                    AggregatedMetrics.period_type == 'hour',
                    AggregatedMetrics.timestamp == hour_start
                )
            )
            if existing.scalar_one_or_none():
                continue
            
            # Get raw data for this hour
            result = await db.execute(
                select(
                    sql_func.avg(MetricsSnapshot.cpu_usage).label('avg_cpu'),
                    sql_func.max(MetricsSnapshot.cpu_usage).label('max_cpu'),
                    sql_func.avg(MetricsSnapshot.load_avg_1).label('avg_load'),
                    sql_func.avg(MetricsSnapshot.memory_percent).label('avg_memory'),
                    sql_func.max(MetricsSnapshot.memory_percent).label('max_memory'),
                    sql_func.avg(MetricsSnapshot.disk_percent).label('avg_disk'),
                    sql_func.sum(MetricsSnapshot.net_rx_bytes_per_sec * 5).label('total_rx'),  # 5 sec intervals
                    sql_func.sum(MetricsSnapshot.net_tx_bytes_per_sec * 5).label('total_tx'),
                    sql_func.avg(MetricsSnapshot.net_rx_bytes_per_sec).label('avg_rx'),
                    sql_func.avg(MetricsSnapshot.net_tx_bytes_per_sec).label('avg_tx'),
                    sql_func.avg(MetricsSnapshot.disk_read_bytes_per_sec).label('avg_disk_read'),
                    sql_func.avg(MetricsSnapshot.disk_write_bytes_per_sec).label('avg_disk_write'),
                    sql_func.avg(MetricsSnapshot.tcp_established).label('avg_tcp_established'),
                    sql_func.avg(MetricsSnapshot.tcp_listen).label('avg_tcp_listen'),
                    sql_func.avg(MetricsSnapshot.tcp_time_wait).label('avg_tcp_time_wait'),
                    sql_func.avg(MetricsSnapshot.tcp_close_wait).label('avg_tcp_close_wait'),
                    sql_func.avg(MetricsSnapshot.tcp_syn_sent).label('avg_tcp_syn_sent'),
                    sql_func.avg(MetricsSnapshot.tcp_syn_recv).label('avg_tcp_syn_recv'),
                    sql_func.avg(MetricsSnapshot.tcp_fin_wait).label('avg_tcp_fin_wait'),
                    sql_func.count().label('data_points')
                ).where(
                    MetricsSnapshot.server_id == server_id,
                    MetricsSnapshot.timestamp >= hour_start,
                    MetricsSnapshot.timestamp < hour_end
                )
            )
            row = result.fetchone()
            
            if row and row.data_points > 0:
                aggregated = AggregatedMetrics(
                    server_id=server_id,
                    timestamp=hour_start,
                    period_type='hour',
                    avg_cpu=row.avg_cpu or 0,
                    max_cpu=row.max_cpu or 0,
                    avg_load=row.avg_load or 0,
                    avg_memory_percent=row.avg_memory or 0,
                    max_memory_percent=row.max_memory or 0,
                    avg_disk_percent=row.avg_disk or 0,
                    total_rx_bytes=int(row.total_rx or 0),
                    total_tx_bytes=int(row.total_tx or 0),
                    avg_rx_speed=row.avg_rx or 0,
                    avg_tx_speed=row.avg_tx or 0,
                    avg_disk_read_speed=row.avg_disk_read or 0,
                    avg_disk_write_speed=row.avg_disk_write or 0,
                    avg_tcp_established=row.avg_tcp_established,
                    avg_tcp_listen=row.avg_tcp_listen,
                    avg_tcp_time_wait=row.avg_tcp_time_wait,
                    avg_tcp_close_wait=row.avg_tcp_close_wait,
                    avg_tcp_syn_sent=row.avg_tcp_syn_sent,
                    avg_tcp_syn_recv=row.avg_tcp_syn_recv,
                    avg_tcp_fin_wait=row.avg_tcp_fin_wait,
                    data_points=row.data_points
                )
                db.add(aggregated)
        
        await db.commit()
        logger.info(f"Hourly aggregation completed for {hour_start}")
    
    async def _aggregate_daily(self, db: AsyncSession):
        """Aggregate hourly metrics to daily summaries"""
        result = await db.execute(select(Server.id))
        server_ids = [row[0] for row in result.fetchall()]
        
        # Aggregate last complete day (use naive UTC)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start = day_end - timedelta(days=1)
        
        for server_id in server_ids:
            # Check if already aggregated
            existing = await db.execute(
                select(AggregatedMetrics).where(
                    AggregatedMetrics.server_id == server_id,
                    AggregatedMetrics.period_type == 'day',
                    AggregatedMetrics.timestamp == day_start
                )
            )
            if existing.scalar_one_or_none():
                continue
            
            # Get hourly data for this day
            result = await db.execute(
                select(
                    sql_func.avg(AggregatedMetrics.avg_cpu).label('avg_cpu'),
                    sql_func.max(AggregatedMetrics.max_cpu).label('max_cpu'),
                    sql_func.avg(AggregatedMetrics.avg_load).label('avg_load'),
                    sql_func.avg(AggregatedMetrics.avg_memory_percent).label('avg_memory'),
                    sql_func.max(AggregatedMetrics.max_memory_percent).label('max_memory'),
                    sql_func.avg(AggregatedMetrics.avg_disk_percent).label('avg_disk'),
                    sql_func.sum(AggregatedMetrics.total_rx_bytes).label('total_rx'),
                    sql_func.sum(AggregatedMetrics.total_tx_bytes).label('total_tx'),
                    sql_func.avg(AggregatedMetrics.avg_rx_speed).label('avg_rx'),
                    sql_func.avg(AggregatedMetrics.avg_tx_speed).label('avg_tx'),
                    sql_func.avg(AggregatedMetrics.avg_disk_read_speed).label('avg_disk_read'),
                    sql_func.avg(AggregatedMetrics.avg_disk_write_speed).label('avg_disk_write'),
                    sql_func.avg(AggregatedMetrics.avg_tcp_established).label('avg_tcp_established'),
                    sql_func.avg(AggregatedMetrics.avg_tcp_listen).label('avg_tcp_listen'),
                    sql_func.avg(AggregatedMetrics.avg_tcp_time_wait).label('avg_tcp_time_wait'),
                    sql_func.avg(AggregatedMetrics.avg_tcp_close_wait).label('avg_tcp_close_wait'),
                    sql_func.avg(AggregatedMetrics.avg_tcp_syn_sent).label('avg_tcp_syn_sent'),
                    sql_func.avg(AggregatedMetrics.avg_tcp_syn_recv).label('avg_tcp_syn_recv'),
                    sql_func.avg(AggregatedMetrics.avg_tcp_fin_wait).label('avg_tcp_fin_wait'),
                    sql_func.sum(AggregatedMetrics.data_points).label('data_points')
                ).where(
                    AggregatedMetrics.server_id == server_id,
                    AggregatedMetrics.period_type == 'hour',
                    AggregatedMetrics.timestamp >= day_start,
                    AggregatedMetrics.timestamp < day_end
                )
            )
            row = result.fetchone()
            
            if row and row.data_points and row.data_points > 0:
                aggregated = AggregatedMetrics(
                    server_id=server_id,
                    timestamp=day_start,
                    period_type='day',
                    avg_cpu=row.avg_cpu or 0,
                    max_cpu=row.max_cpu or 0,
                    avg_load=row.avg_load or 0,
                    avg_memory_percent=row.avg_memory or 0,
                    max_memory_percent=row.max_memory or 0,
                    avg_disk_percent=row.avg_disk or 0,
                    total_rx_bytes=int(row.total_rx or 0),
                    total_tx_bytes=int(row.total_tx or 0),
                    avg_rx_speed=row.avg_rx or 0,
                    avg_tx_speed=row.avg_tx or 0,
                    avg_disk_read_speed=row.avg_disk_read or 0,
                    avg_disk_write_speed=row.avg_disk_write or 0,
                    avg_tcp_established=row.avg_tcp_established,
                    avg_tcp_listen=row.avg_tcp_listen,
                    avg_tcp_time_wait=row.avg_tcp_time_wait,
                    avg_tcp_close_wait=row.avg_tcp_close_wait,
                    avg_tcp_syn_sent=row.avg_tcp_syn_sent,
                    avg_tcp_syn_recv=row.avg_tcp_syn_recv,
                    avg_tcp_fin_wait=row.avg_tcp_fin_wait,
                    data_points=row.data_points
                )
                db.add(aggregated)
        
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
