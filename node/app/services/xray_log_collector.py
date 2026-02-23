"""Xray log collector for Remnawave nodes.

Reads and parses Xray access logs from remnanode container,
aggregates stats in memory, and provides them to the panel on request.

Memory protection:
- MAX_MEMORY_MB: Hard limit on memory usage (default 256MB)
- MAX_ENTRIES: Limit on dictionary size to prevent OOM
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

# Memory protection limits
MAX_MEMORY_MB = 256
MAX_ENTRIES = 1_000_000  # Max unique (email, source_ip, host) tuples

# Batch processing settings
BATCH_INTERVAL_SEC = 5
MAX_BUFFER_LINES = 200_000
MAX_BUFFER_MB = 100


def _extract_host(destination: str) -> str:
    """Extract host from destination, stripping :port suffix."""
    parts = destination.rsplit(':', 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return destination


@dataclass
class AggregatedStats:
    """In-memory aggregated statistics with memory protection.
    
    Single dictionary: (email, source_ip, host) -> count
    Contains ALL needed data — user, IP, destination, visit count.
    """
    # Key: (email, source_ip, host) -> count
    stats: dict[tuple[int, str, str], int] = field(default_factory=lambda: defaultdict(int))
    total_entries: int = 0
    started_at: Optional[datetime] = None
    dropped_entries: int = 0
    auto_flushes: int = 0
    
    def get_memory_usage_mb(self) -> float:
        total_bytes = sys.getsizeof(self.stats)
        total_bytes += len(self.stats) * 120
        return total_bytes / (1024 * 1024)
    
    def is_over_limits(self) -> bool:
        if self.get_memory_usage_mb() > MAX_MEMORY_MB:
            return True
        if len(self.stats) > MAX_ENTRIES:
            return True
        return False
    
    def is_near_limits(self) -> bool:
        if self.get_memory_usage_mb() > MAX_MEMORY_MB * 0.9:
            return True
        if len(self.stats) > MAX_ENTRIES * 0.9:
            return True
        return False
    
    def add_entry(self, destination: str, email: int, source_ip: str):
        """Add a visit entry — aggregated by (email, source_ip, host)."""
        self.stats[(email, source_ip, _extract_host(destination))] += 1
        self.total_entries += 1
    
    def clear(self):
        self.stats.clear()
        self.total_entries = 0
        self.dropped_entries = 0
        self.started_at = datetime.now(timezone.utc)
    
    def to_list(self) -> list[dict]:
        """Convert to list format for API response."""
        return [
            {"email": email, "source_ip": source_ip, "host": host, "count": count}
            for (email, source_ip, host), count in self.stats.items()
        ]


class XrayLogCollector:
    """Collector for Xray access logs from remnanode container."""
    
    LOG_PATTERN = re.compile(
        r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+'
        r'from (?:tcp:)?(\d+\.\d+\.\d+\.\d+):(\d+)\s+'
        r'accepted\s+'
        r'(tcp|udp):(.+?)\s+'
        r'\[(.+?)\]\s+'
        r'email:\s*(\d+)'
    )
    
    CONTAINER_NAME = "remnanode"
    LOG_PATH = "/var/log/supervisor/xray.out.log"
    AUTO_FLUSH_SECONDS = 600
    MEMORY_CHECK_INTERVAL = 30
    
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
        
        self._line_buffer: list[str] = []
        self._buffer_lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xray_parser")
        self._buffer_dropped_lines = 0
        self._total_lines_read = 0
        self._total_lines_parsed = 0
        self._last_batch_time: Optional[datetime] = None
        self._last_batch_duration_ms: float = 0
    
    async def _check_container_available(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "inspect", "-f", "{{.State.Running}}", self.CONTAINER_NAME,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return proc.returncode == 0 and stdout.decode().strip() == "true"
        except asyncio.TimeoutError:
            return False
        except Exception:
            return False
    
    def _get_buffer_size_mb(self) -> float:
        if not self._line_buffer:
            return 0.0
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
            
            if "-> BLOCK" in line or ">> BLOCK" in line or "-> torrent" in line:
                continue
            
            match = pattern.match(line)
            if not match:
                continue
            
            try:
                groups = match.groups()
                source_ip = groups[1]
                destination = groups[4]
                email = int(groups[6])
                results.append((destination, email, source_ip))
            except (ValueError, IndexError):
                continue
        
        return results
    
    async def _process_batch(self):
        async with self._buffer_lock:
            if not self._line_buffer:
                return
            lines_to_process = self._line_buffer
            self._line_buffer = []
        
        if not lines_to_process:
            return
        
        start_time = datetime.now(timezone.utc)
        
        if self._stats.is_near_limits():
            logger.warning(
                f"Stats near limits, skipping batch of {len(lines_to_process)} lines. "
                f"Memory: {self._stats.get_memory_usage_mb():.1f}MB"
            )
            self._stats.dropped_entries += len(lines_to_process)
            return
        
        loop = asyncio.get_event_loop()
        try:
            parsed_entries = await loop.run_in_executor(
                self._executor, self._parse_batch_sync, lines_to_process
            )
        except Exception as e:
            logger.error(f"Batch parsing error: {e}")
            return
        
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
        while self._running:
            try:
                await asyncio.sleep(BATCH_INTERVAL_SEC)
                await self._process_batch()
            except asyncio.CancelledError:
                await self._process_batch()
                raise
            except Exception as e:
                logger.error(f"Error in batch processor: {e}")
    
    async def _memory_monitor_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.MEMORY_CHECK_INTERVAL)
                
                if self._stats.is_over_limits():
                    logger.warning(
                        f"Stats memory limit exceeded! "
                        f"entries={len(self._stats.stats)}, "
                        f"memory={self._stats.get_memory_usage_mb():.1f}MB. "
                        f"Auto-flushing stats."
                    )
                    self._stats.auto_flushes += 1
                    self._stats.clear()
                    continue
                
                time_since_collection = (datetime.now(timezone.utc) - self._last_collection).total_seconds()
                if time_since_collection > self.AUTO_FLUSH_SECONDS and self._stats.total_entries > 0:
                    logger.warning(
                        f"No collection for {time_since_collection:.0f}s. "
                        f"Auto-flushing {self._stats.total_entries} entries."
                    )
                    self._stats.auto_flushes += 1
                    self._stats.clear()
                    self._last_collection = datetime.now(timezone.utc)
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in memory monitor: {e}")
    
    async def _read_loop(self):
        while self._running:
            try:
                if not await self._check_container_available():
                    self._available = False
                    self._last_error = "remnanode container not running"
                    await asyncio.sleep(30)
                    continue
                
                self._available = True
                self._last_error = None
                logger.info("Starting Xray log collection from remnanode (batch mode)")
                
                self._process = await asyncio.create_subprocess_exec(
                    "docker", "exec", self.CONTAINER_NAME,
                    "tail", "-f", "-n", "0", self.LOG_PATH,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                while self._running and self._process.stdout:
                    try:
                        line = await asyncio.wait_for(
                            self._process.stdout.readline(), timeout=60.0
                        )
                        if not line:
                            break
                        
                        self._total_lines_read += 1
                        
                        if len(self._line_buffer) >= MAX_BUFFER_LINES:
                            self._buffer_dropped_lines += 1
                            if self._buffer_dropped_lines % 10000 == 1:
                                logger.warning(f"Buffer overflow, dropped: {self._buffer_dropped_lines}")
                            continue
                        
                        if self._get_buffer_size_mb() >= MAX_BUFFER_MB:
                            self._buffer_dropped_lines += 1
                            continue
                        
                        self._line_buffer.append(line.decode('utf-8', errors='ignore'))
                            
                    except asyncio.TimeoutError:
                        if self._process.returncode is not None:
                            break
                        continue
                
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
        self._running = False
        
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except Exception:
                if self._process:
                    self._process.kill()
            self._process = None
        
        for task in [self._task, self._batch_task, self._memory_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        self._task = None
        self._batch_task = None
        self._memory_task = None
        self._executor.shutdown(wait=False)
        logger.info("Xray log collector stopped")
    
    def get_status(self) -> dict:
        stats_memory_mb = self._stats.get_memory_usage_mb()
        buffer_memory_mb = self._get_buffer_size_mb()
        
        return {
            "available": self._available,
            "running": self._running,
            "container": self.CONTAINER_NAME,
            "entries_collected": self._stats.total_entries,
            "unique_combinations": len(self._stats.stats),
            "started_at": self._stats.started_at.isoformat() if self._stats.started_at else None,
            "last_error": self._last_error,
            "batch_interval_sec": BATCH_INTERVAL_SEC,
            "buffer_lines": len(self._line_buffer),
            "buffer_memory_mb": round(buffer_memory_mb, 2),
            "buffer_dropped_lines": self._buffer_dropped_lines,
            "total_lines_read": self._total_lines_read,
            "total_lines_parsed": self._total_lines_parsed,
            "last_batch_duration_ms": round(self._last_batch_duration_ms, 1),
            "stats_memory_mb": round(stats_memory_mb, 2),
            "total_memory_mb": round(stats_memory_mb + buffer_memory_mb, 2),
            "memory_limit_mb": MAX_MEMORY_MB,
            "memory_usage_percent": round(stats_memory_mb / MAX_MEMORY_MB * 100, 1),
            "dropped_entries": self._stats.dropped_entries,
            "auto_flushes": self._stats.auto_flushes
        }
    
    async def collect_and_clear(self) -> dict:
        """Collect current stats and clear memory. Called by panel."""
        await self._process_batch()
        
        collected_at = datetime.now(timezone.utc)
        self._last_collection = collected_at
        
        stats_list = self._stats.to_list()
        
        result = {
            "collected_at": collected_at.isoformat(),
            "period_start": self._stats.started_at.isoformat() if self._stats.started_at else collected_at.isoformat(),
            "entries_count": self._stats.total_entries,
            "stats": stats_list,
            "total_lines_read": self._total_lines_read,
            "total_lines_parsed": self._total_lines_parsed,
            "buffer_dropped_lines": self._buffer_dropped_lines,
            "dropped_entries": self._stats.dropped_entries,
            "auto_flushes": self._stats.auto_flushes,
            "memory_usage_mb_before_clear": round(self._stats.get_memory_usage_mb(), 2)
        }
        
        self._stats.clear()
        self._total_lines_read = 0
        self._total_lines_parsed = 0
        self._buffer_dropped_lines = 0
        
        logger.info(
            f"Collected {result['entries_count']} entries, "
            f"{len(stats_list)} unique (user, ip, host) combos. "
            f"Lines read: {result['total_lines_read']}, "
            f"Buffer dropped: {result['buffer_dropped_lines']}"
        )
        
        return result


# Singleton instance
_collector: Optional[XrayLogCollector] = None


def get_xray_log_collector() -> XrayLogCollector:
    global _collector
    if _collector is None:
        _collector = XrayLogCollector()
    return _collector
