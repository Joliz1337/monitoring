"""Xray log collector for Remnawave nodes.

Reads and parses Xray access logs from remnanode container,
aggregates stats in memory, and provides them to the panel on request.

Memory protection:
- MAX_MEMORY_MB: Hard limit on memory usage (default 256MB)
- MAX_ENTRIES_*: Limits per dictionary to prevent OOM
- Auto-flush when limits exceeded

Performance optimization:
- Batch processing: lines buffered and parsed every BATCH_INTERVAL_SEC
- Reduces CPU load by ~3-5x compared to per-line processing
- Event loop stays free for API requests between batches
"""

import asyncio
import logging
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Memory protection limits (optimized for reduced storage)
MAX_MEMORY_MB = 256  # Maximum memory for stats (MB)
MAX_ENTRIES_VISITS = 500_000  # Max unique (destination, email) pairs
MAX_ENTRIES_IP_VISITS = 1_000_000  # Max unique (email, source_ip) pairs
MAX_ENTRIES_IP_DEST = 1_000_000  # Max unique (email, source_ip, destination) tuples

# Batch processing settings
BATCH_INTERVAL_SEC = 5  # Process buffered lines every N seconds
MAX_BUFFER_LINES = 200_000  # Max lines in buffer (~50MB at 250 bytes/line)
MAX_BUFFER_MB = 100  # Hard limit on buffer memory


@dataclass
class XrayLogEntry:
    """Parsed Xray log entry."""
    timestamp: datetime
    source_ip: str
    source_port: int
    protocol: str
    destination: str
    route: str
    email: int
    blocked: bool = False


@dataclass
class AggregatedStats:
    """In-memory aggregated statistics with memory protection."""
    # Key: (destination, email) -> count
    visits: dict[tuple[str, int], int] = field(default_factory=lambda: defaultdict(int))
    # Key: (email, source_ip) -> count
    ip_visits: dict[tuple[int, str], int] = field(default_factory=lambda: defaultdict(int))
    # Key: (email, source_ip, destination) -> count
    ip_destination_visits: dict[tuple[int, str, str], int] = field(default_factory=lambda: defaultdict(int))
    total_entries: int = 0
    started_at: Optional[datetime] = None
    dropped_entries: int = 0  # Entries dropped due to limits
    auto_flushes: int = 0  # Number of auto-flushes due to memory limits
    
    def get_memory_usage_mb(self) -> float:
        """Estimate memory usage of all dictionaries in MB."""
        total_bytes = (
            sys.getsizeof(self.visits) + 
            sys.getsizeof(self.ip_visits) + 
            sys.getsizeof(self.ip_destination_visits)
        )
        # Each dict entry: ~120 bytes for tuple keys + int value + overhead
        entry_count = len(self.visits) + len(self.ip_visits) + len(self.ip_destination_visits)
        total_bytes += entry_count * 120
        return total_bytes / (1024 * 1024)
    
    def is_over_limits(self) -> bool:
        """Check if any limit is exceeded."""
        if self.get_memory_usage_mb() > MAX_MEMORY_MB:
            return True
        if len(self.visits) > MAX_ENTRIES_VISITS:
            return True
        if len(self.ip_visits) > MAX_ENTRIES_IP_VISITS:
            return True
        if len(self.ip_destination_visits) > MAX_ENTRIES_IP_DEST:
            return True
        return False
    
    def is_near_limits(self) -> bool:
        """Check if we're approaching limits (90%)."""
        if self.get_memory_usage_mb() > MAX_MEMORY_MB * 0.9:
            return True
        if len(self.visits) > MAX_ENTRIES_VISITS * 0.9:
            return True
        if len(self.ip_visits) > MAX_ENTRIES_IP_VISITS * 0.9:
            return True
        if len(self.ip_destination_visits) > MAX_ENTRIES_IP_DEST * 0.9:
            return True
        return False
    
    def add_entry(self, destination: str, email: int, source_ip: str):
        """Add a visit entry with IP tracking."""
        self.visits[(destination, email)] += 1
        self.ip_visits[(email, source_ip)] += 1
        self.ip_destination_visits[(email, source_ip, destination)] += 1
        self.total_entries += 1
    
    def clear(self):
        """Clear all stats."""
        self.visits.clear()
        self.ip_visits.clear()
        self.ip_destination_visits.clear()
        self.total_entries = 0
        self.dropped_entries = 0
        self.started_at = datetime.now(timezone.utc)
    
    def to_list(self) -> list[dict]:
        """Convert visits to list format for API response."""
        return [
            {"destination": dest, "email": email, "count": count}
            for (dest, email), count in self.visits.items()
        ]
    
    def ip_to_list(self) -> list[dict]:
        """Convert IP visits to list format for API response."""
        return [
            {"email": email, "source_ip": source_ip, "count": count}
            for (email, source_ip), count in self.ip_visits.items()
        ]
    
    def ip_destination_to_list(self) -> list[dict]:
        """Convert IP-destination visits to list format for API response."""
        return [
            {"email": email, "source_ip": source_ip, "destination": destination, "count": count}
            for (email, source_ip, destination), count in self.ip_destination_visits.items()
        ]


class XrayLogCollector:
    """Collector for Xray access logs from remnanode container.
    
    Performance optimization (batch processing):
    - Lines buffered in memory, parsed every BATCH_INTERVAL_SEC seconds
    - Parsing runs in thread pool to not block event loop
    - CPU load reduced ~3-5x compared to per-line processing
    
    Memory protection:
    - Buffer limited to MAX_BUFFER_LINES / MAX_BUFFER_MB
    - Stats limited to MAX_ENTRIES_* / MAX_MEMORY_MB
    - Auto-flush when limits exceeded
    """
    
    # Log format regex (compiled once, reused for all lines)
    # Example: 2026/01/29 17:06:24.355538 from 90.156.214.233:33574 accepted tcp:p19-common-sign.tiktokcdn-us.com:443 [usa1 >> direct] email: 4385
    LOG_PATTERN = re.compile(
        r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+'  # timestamp
        r'from (?:tcp:)?(\d+\.\d+\.\d+\.\d+):(\d+)\s+'      # source IP:port
        r'accepted\s+'                                       # accepted
        r'(tcp|udp):(.+?)\s+'                                # protocol:destination
        r'\[(.+?)\]\s+'                                      # route
        r'email:\s*(\d+)'                                    # email (user ID)
    )
    
    CONTAINER_NAME = "remnanode"
    LOG_PATH = "/var/log/supervisor/xray.out.log"
    AUTO_FLUSH_SECONDS = 600  # Auto-flush if no collection for 10 minutes
    MEMORY_CHECK_INTERVAL = 30  # Check memory limits every 30 seconds
    
    def __init__(self):
        self._stats = AggregatedStats()
        self._stats.started_at = datetime.now(timezone.utc)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._batch_task: Optional[asyncio.Task] = None
        self._memory_task: Optional[asyncio.Task] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._available = False
        self._last_error: Optional[str] = None
        self._last_collection: datetime = datetime.now(timezone.utc)
        
        # Batch processing state
        self._line_buffer: list[str] = []
        self._buffer_lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xray_parser")
        self._buffer_dropped_lines = 0  # Lines dropped due to buffer overflow
        self._total_lines_read = 0
        self._total_lines_parsed = 0
        self._last_batch_time: Optional[datetime] = None
        self._last_batch_duration_ms: float = 0
    
    async def _check_container_available(self) -> bool:
        """Check if remnanode container is running (async)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "inspect", "-f", "{{.State.Running}}", self.CONTAINER_NAME,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return proc.returncode == 0 and stdout.decode().strip() == "true"
        except asyncio.TimeoutError:
            logger.debug("Container check timed out")
            return False
        except Exception as e:
            logger.debug(f"Container check failed: {e}")
            return False
    
    def _get_buffer_size_mb(self) -> float:
        """Estimate buffer memory usage in MB."""
        if not self._line_buffer:
            return 0.0
        # Average line ~200 bytes + list overhead
        return len(self._line_buffer) * 250 / (1024 * 1024)
    
    def _parse_batch_sync(self, lines: list[str]) -> list[tuple[str, int, str]]:
        """Parse batch of lines synchronously (runs in thread pool).
        
        Returns list of (destination, email, source_ip) tuples.
        """
        results = []
        pattern = self.LOG_PATTERN
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Skip blocked entries
            if "-> BLOCK" in line or ">> BLOCK" in line:
                continue
            
            match = pattern.match(line)
            if not match:
                continue
            
            try:
                # Extract only what we need: source_ip (group 2), destination (group 5), email (group 7)
                groups = match.groups()
                source_ip = groups[1]
                destination = groups[4]
                email = int(groups[6])
                results.append((destination, email, source_ip))
            except (ValueError, IndexError):
                continue
        
        return results
    
    async def _process_batch(self):
        """Process buffered lines in thread pool."""
        async with self._buffer_lock:
            if not self._line_buffer:
                return
            
            # Take buffer and clear it
            lines_to_process = self._line_buffer
            self._line_buffer = []
        
        if not lines_to_process:
            return
        
        start_time = datetime.now(timezone.utc)
        
        # Check if stats are near limits before processing
        if self._stats.is_near_limits():
            logger.warning(
                f"Stats near limits, skipping batch of {len(lines_to_process)} lines. "
                f"Memory: {self._stats.get_memory_usage_mb():.1f}MB"
            )
            self._stats.dropped_entries += len(lines_to_process)
            return
        
        # Parse in thread pool to not block event loop
        loop = asyncio.get_event_loop()
        try:
            parsed_entries = await loop.run_in_executor(
                self._executor,
                self._parse_batch_sync,
                lines_to_process
            )
        except Exception as e:
            logger.error(f"Batch parsing error: {e}")
            return
        
        # Add parsed entries to stats (this is fast, just dict updates)
        for destination, email, source_ip in parsed_entries:
            self._stats.add_entry(destination, email, source_ip)
        
        self._total_lines_parsed += len(parsed_entries)
        self._last_batch_time = datetime.now(timezone.utc)
        self._last_batch_duration_ms = (self._last_batch_time - start_time).total_seconds() * 1000
        
        if len(lines_to_process) > 1000:
            logger.debug(
                f"Batch processed: {len(lines_to_process)} lines -> "
                f"{len(parsed_entries)} entries in {self._last_batch_duration_ms:.0f}ms"
            )
    
    async def _batch_processor_loop(self):
        """Background task to process buffered lines every BATCH_INTERVAL_SEC."""
        while self._running:
            try:
                await asyncio.sleep(BATCH_INTERVAL_SEC)
                await self._process_batch()
            except asyncio.CancelledError:
                # Process remaining buffer before exit
                await self._process_batch()
                raise
            except Exception as e:
                logger.error(f"Error in batch processor: {e}")
    
    async def _memory_monitor_loop(self):
        """Background task to monitor memory and auto-flush if needed."""
        while self._running:
            try:
                await asyncio.sleep(self.MEMORY_CHECK_INTERVAL)
                
                # Check stats memory limits
                if self._stats.is_over_limits():
                    logger.warning(
                        f"Stats memory limit exceeded! "
                        f"visits={len(self._stats.visits)}, "
                        f"ip_visits={len(self._stats.ip_visits)}, "
                        f"ip_dest={len(self._stats.ip_destination_visits)}, "
                        f"memory={self._stats.get_memory_usage_mb():.1f}MB. "
                        f"Auto-flushing stats."
                    )
                    self._stats.auto_flushes += 1
                    self._stats.clear()
                    continue
                
                # Check auto-flush timeout
                time_since_collection = (datetime.now(timezone.utc) - self._last_collection).total_seconds()
                if time_since_collection > self.AUTO_FLUSH_SECONDS and self._stats.total_entries > 0:
                    logger.warning(
                        f"No collection for {time_since_collection:.0f}s "
                        f"(limit {self.AUTO_FLUSH_SECONDS}s). "
                        f"Auto-flushing {self._stats.total_entries} entries."
                    )
                    self._stats.auto_flushes += 1
                    self._stats.clear()
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in memory monitor: {e}")
    
    async def _read_loop(self):
        """Main loop reading from docker exec tail -f, buffering lines for batch processing."""
        while self._running:
            try:
                # Check if container is available (async)
                if not await self._check_container_available():
                    self._available = False
                    self._last_error = "remnanode container not running"
                    logger.debug("remnanode container not available, waiting...")
                    await asyncio.sleep(30)
                    continue
                
                self._available = True
                self._last_error = None
                logger.info("Starting Xray log collection from remnanode (batch mode)")
                
                # Start docker exec with tail -f
                self._process = await asyncio.create_subprocess_exec(
                    "docker", "exec", self.CONTAINER_NAME,
                    "tail", "-f", "-n", "0", self.LOG_PATH,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                # Read lines continuously, add to buffer (no parsing here!)
                while self._running and self._process.stdout:
                    try:
                        line = await asyncio.wait_for(
                            self._process.stdout.readline(),
                            timeout=60.0
                        )
                        if not line:
                            break
                        
                        self._total_lines_read += 1
                        
                        # Check buffer limits
                        buffer_size = len(self._line_buffer)
                        if buffer_size >= MAX_BUFFER_LINES:
                            self._buffer_dropped_lines += 1
                            if self._buffer_dropped_lines % 10000 == 1:
                                logger.warning(
                                    f"Buffer overflow, dropping lines. "
                                    f"Dropped: {self._buffer_dropped_lines}"
                                )
                            continue
                        
                        if self._get_buffer_size_mb() >= MAX_BUFFER_MB:
                            self._buffer_dropped_lines += 1
                            continue
                        
                        # Add to buffer (will be parsed in batch processor)
                        line_str = line.decode('utf-8', errors='ignore')
                        self._line_buffer.append(line_str)
                            
                    except asyncio.TimeoutError:
                        # No new lines for 60s, check if process still alive
                        if self._process.returncode is not None:
                            break
                        continue
                
                # Process ended, cleanup
                if self._process:
                    try:
                        self._process.terminate()
                        await asyncio.wait_for(self._process.wait(), timeout=5.0)
                    except Exception:
                        if self._process:
                            self._process.kill()
                    self._process = None
                
                logger.info("Xray log reader process ended, will restart...")
                await asyncio.sleep(5)
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_error = str(e)
                logger.error(f"Error in Xray log collection: {e}")
                await asyncio.sleep(10)
    
    async def start(self):
        """Start the log collector, batch processor, and memory monitor."""
        if self._running:
            return
        
        self._running = True
        self._stats.started_at = datetime.now(timezone.utc)
        self._last_collection = datetime.now(timezone.utc)
        self._task = asyncio.create_task(self._read_loop())
        self._batch_task = asyncio.create_task(self._batch_processor_loop())
        self._memory_task = asyncio.create_task(self._memory_monitor_loop())
        logger.info(
            f"Xray log collector started (batch mode). "
            f"Batch interval: {BATCH_INTERVAL_SEC}s, "
            f"Buffer: {MAX_BUFFER_LINES} lines / {MAX_BUFFER_MB}MB, "
            f"Stats: {MAX_MEMORY_MB}MB"
        )
    
    async def stop(self):
        """Stop the log collector, batch processor, and memory monitor."""
        self._running = False
        
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except Exception:
                if self._process:
                    self._process.kill()
            self._process = None
        
        # Stop main task
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        
        # Stop batch processor (will process remaining buffer)
        if self._batch_task:
            self._batch_task.cancel()
            try:
                await self._batch_task
            except asyncio.CancelledError:
                pass
            self._batch_task = None
        
        # Stop memory monitor
        if self._memory_task:
            self._memory_task.cancel()
            try:
                await self._memory_task
            except asyncio.CancelledError:
                pass
            self._memory_task = None
        
        # Shutdown thread pool
        self._executor.shutdown(wait=False)
        
        logger.info("Xray log collector stopped")
    
    def get_status(self) -> dict:
        """Get collector status including batch processing and memory info."""
        stats_memory_mb = self._stats.get_memory_usage_mb()
        buffer_memory_mb = self._get_buffer_size_mb()
        total_memory_mb = stats_memory_mb + buffer_memory_mb
        
        return {
            "available": self._available,
            "running": self._running,
            "container": self.CONTAINER_NAME,
            "entries_collected": self._stats.total_entries,
            "unique_combinations": len(self._stats.visits),
            "unique_ip_combinations": len(self._stats.ip_visits),
            "unique_ip_dest_combinations": len(self._stats.ip_destination_visits),
            "started_at": self._stats.started_at.isoformat() if self._stats.started_at else None,
            "last_error": self._last_error,
            # Batch processing stats
            "batch_mode": True,
            "batch_interval_sec": BATCH_INTERVAL_SEC,
            "buffer_lines": len(self._line_buffer),
            "buffer_memory_mb": round(buffer_memory_mb, 2),
            "buffer_dropped_lines": self._buffer_dropped_lines,
            "total_lines_read": self._total_lines_read,
            "total_lines_parsed": self._total_lines_parsed,
            "last_batch_duration_ms": round(self._last_batch_duration_ms, 1),
            # Memory stats
            "stats_memory_mb": round(stats_memory_mb, 2),
            "total_memory_mb": round(total_memory_mb, 2),
            "memory_limit_mb": MAX_MEMORY_MB,
            "memory_usage_percent": round(stats_memory_mb / MAX_MEMORY_MB * 100, 1),
            "dropped_entries": self._stats.dropped_entries,
            "auto_flushes": self._stats.auto_flushes,
            # Limits info
            "limits": {
                "max_stats_memory_mb": MAX_MEMORY_MB,
                "max_buffer_lines": MAX_BUFFER_LINES,
                "max_buffer_mb": MAX_BUFFER_MB,
                "max_visits": MAX_ENTRIES_VISITS,
                "max_ip_visits": MAX_ENTRIES_IP_VISITS,
                "max_ip_dest": MAX_ENTRIES_IP_DEST,
                "batch_interval_sec": BATCH_INTERVAL_SEC,
                "auto_flush_seconds": self.AUTO_FLUSH_SECONDS
            }
        }
    
    async def collect_and_clear(self) -> dict:
        """Collect current stats and clear memory. Called by panel.
        
        Now async to process remaining buffer before collection.
        """
        # Process remaining buffer first
        await self._process_batch()
        
        collected_at = datetime.now(timezone.utc)
        self._last_collection = collected_at
        
        stats_list = self._stats.to_list()
        ip_stats_list = self._stats.ip_to_list()
        ip_dest_stats_list = self._stats.ip_destination_to_list()
        
        result = {
            "collected_at": collected_at.isoformat(),
            "period_start": self._stats.started_at.isoformat() if self._stats.started_at else collected_at.isoformat(),
            "entries_count": self._stats.total_entries,
            "stats": stats_list,
            "ip_stats": ip_stats_list,
            "ip_destination_stats": ip_dest_stats_list,
            # Processing stats
            "total_lines_read": self._total_lines_read,
            "total_lines_parsed": self._total_lines_parsed,
            "buffer_dropped_lines": self._buffer_dropped_lines,
            "dropped_entries": self._stats.dropped_entries,
            "auto_flushes": self._stats.auto_flushes,
            "memory_usage_mb_before_clear": round(self._stats.get_memory_usage_mb(), 2)
        }
        
        # Clear stats after collection
        self._stats.clear()
        
        # Reset counters
        self._total_lines_read = 0
        self._total_lines_parsed = 0
        self._buffer_dropped_lines = 0
        
        logger.info(
            f"Collected {result['entries_count']} entries, "
            f"{len(stats_list)} unique visits, "
            f"{len(ip_stats_list)} unique IPs, "
            f"{len(ip_dest_stats_list)} unique IP-dest. "
            f"Lines read: {result['total_lines_read']}, "
            f"Buffer dropped: {result['buffer_dropped_lines']}"
        )
        
        return result


# Singleton instance
_collector: Optional[XrayLogCollector] = None


def get_xray_log_collector() -> XrayLogCollector:
    """Get or create Xray log collector instance."""
    global _collector
    if _collector is None:
        _collector = XrayLogCollector()
    return _collector
