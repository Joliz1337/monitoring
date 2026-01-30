"""Xray log collector for Remnawave nodes.

Reads and parses Xray access logs from remnanode container,
aggregates stats in memory, and provides them to the panel on request.
"""

import asyncio
import logging
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


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
    """In-memory aggregated statistics."""
    # Key: (destination, email) -> count
    visits: dict[tuple[str, int], int] = field(default_factory=lambda: defaultdict(int))
    # Key: (email, source_ip) -> count
    ip_visits: dict[tuple[int, str], int] = field(default_factory=lambda: defaultdict(int))
    total_entries: int = 0
    started_at: Optional[datetime] = None
    
    def add_entry(self, destination: str, email: int, source_ip: str):
        """Add a visit entry with IP tracking."""
        self.visits[(destination, email)] += 1
        self.ip_visits[(email, source_ip)] += 1
        self.total_entries += 1
    
    def clear(self):
        """Clear all stats."""
        self.visits.clear()
        self.ip_visits.clear()
        self.total_entries = 0
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


class XrayLogCollector:
    """Collector for Xray access logs from remnanode container."""
    
    # Log format regex
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
    
    def __init__(self):
        self._stats = AggregatedStats()
        self._stats.started_at = datetime.now(timezone.utc)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._available = False
        self._last_error: Optional[str] = None
    
    def _check_container_available(self) -> bool:
        """Check if remnanode container is running."""
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self.CONTAINER_NAME],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0 and result.stdout.strip() == "true"
        except Exception as e:
            logger.debug(f"Container check failed: {e}")
            return False
    
    def parse_log_line(self, line: str) -> Optional[XrayLogEntry]:
        """Parse a single log line into XrayLogEntry."""
        line = line.strip()
        if not line:
            return None
        
        # Check for blocked entries - skip them
        if "-> BLOCK" in line or ">> BLOCK" in line:
            return None
        
        match = self.LOG_PATTERN.match(line)
        if not match:
            return None
        
        try:
            timestamp_str, source_ip, source_port, protocol, destination, route, email = match.groups()
            
            # Parse timestamp: 2026/01/29 17:06:24.355538
            timestamp = datetime.strptime(timestamp_str[:19], "%Y/%m/%d %H:%M:%S")
            timestamp = timestamp.replace(tzinfo=timezone.utc)
            
            return XrayLogEntry(
                timestamp=timestamp,
                source_ip=source_ip,
                source_port=int(source_port),
                protocol=protocol,
                destination=destination,
                route=route,
                email=int(email),
                blocked=False
            )
        except (ValueError, IndexError) as e:
            logger.debug(f"Failed to parse log line: {e}")
            return None
    
    async def _read_loop(self):
        """Main loop reading from docker exec tail -f."""
        while self._running:
            try:
                # Check if container is available
                if not self._check_container_available():
                    self._available = False
                    self._last_error = "remnanode container not running"
                    logger.debug("remnanode container not available, waiting...")
                    await asyncio.sleep(30)
                    continue
                
                self._available = True
                self._last_error = None
                logger.info("Starting Xray log collection from remnanode")
                
                # Start docker exec with tail -f
                self._process = await asyncio.create_subprocess_exec(
                    "docker", "exec", self.CONTAINER_NAME,
                    "tail", "-f", "-n", "0", self.LOG_PATH,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                # Read lines continuously
                while self._running and self._process.stdout:
                    try:
                        line = await asyncio.wait_for(
                            self._process.stdout.readline(),
                            timeout=60.0
                        )
                        if not line:
                            break
                        
                        line_str = line.decode('utf-8', errors='ignore')
                        entry = self.parse_log_line(line_str)
                        
                        if entry:
                            self._stats.add_entry(entry.destination, entry.email, entry.source_ip)
                            
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
        """Start the log collector."""
        if self._running:
            return
        
        self._running = True
        self._stats.started_at = datetime.now(timezone.utc)
        self._task = asyncio.create_task(self._read_loop())
        logger.info("Xray log collector started")
    
    async def stop(self):
        """Stop the log collector."""
        self._running = False
        
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except Exception:
                if self._process:
                    self._process.kill()
            self._process = None
        
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        
        logger.info("Xray log collector stopped")
    
    def get_status(self) -> dict:
        """Get collector status."""
        return {
            "available": self._available,
            "running": self._running,
            "container": self.CONTAINER_NAME,
            "entries_collected": self._stats.total_entries,
            "unique_combinations": len(self._stats.visits),
            "unique_ip_combinations": len(self._stats.ip_visits),
            "started_at": self._stats.started_at.isoformat() if self._stats.started_at else None,
            "last_error": self._last_error
        }
    
    def collect_and_clear(self) -> dict:
        """Collect current stats and clear memory. Called by panel."""
        collected_at = datetime.now(timezone.utc)
        
        stats_list = self._stats.to_list()
        ip_stats_list = self._stats.ip_to_list()
        
        result = {
            "collected_at": collected_at.isoformat(),
            "period_start": self._stats.started_at.isoformat() if self._stats.started_at else collected_at.isoformat(),
            "entries_count": self._stats.total_entries,
            "stats": stats_list,
            "ip_stats": ip_stats_list
        }
        
        # Clear stats after collection
        self._stats.clear()
        
        logger.info(f"Collected {result['entries_count']} entries, {len(stats_list)} unique combinations, {len(ip_stats_list)} unique IP combinations")
        
        return result


# Singleton instance
_collector: Optional[XrayLogCollector] = None


def get_xray_log_collector() -> XrayLogCollector:
    """Get or create Xray log collector instance."""
    global _collector
    if _collector is None:
        _collector = XrayLogCollector()
    return _collector
