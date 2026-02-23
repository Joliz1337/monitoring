"""Torrent blocker service for Xray nodes.

Two detection modes:
1. Tag-based: lines with [... -> torrent] are blocked immediately.
2. Behavior-based: if a source IP connects to >= threshold unique raw-IP
   destinations per minute, it is flagged as torrent and blocked.

Blocks via ipset temporary ban + conntrack flush to kill existing connections.
Persists enabled state and threshold in /var/lib/monitoring/torrent_blocker.json.
"""

import asyncio
import ipaddress
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.services.ipset_manager import get_ipset_manager

logger = logging.getLogger(__name__)

PERSISTENT_FILE = "/var/lib/monitoring/torrent_blocker.json"
CONTAINER_NAME = "remnanode"
LOG_PATH = "/var/log/supervisor/xray.out.log"

# Regex: torrent-tagged lines — immediate block
TORRENT_LINE_PATTERN = re.compile(
    r'from (?:tcp:)?(\d+\.\d+\.\d+\.\d+):\d+\s+accepted\s+.+?\[.+?->\s*torrent\]'
)

# Regex: any accepted connection — extract source IP, dest host, email
# Matches both "from tcp:1.2.3.4:port" and "from 1.2.3.4:port"
# Dest can be ip or domain: "tcp:5.34.60.150:25402" or "tcp:panel.example.com:443"
ANY_CONNECTION_PATTERN = re.compile(
    r'from (?:tcp:)?(\d+\.\d+\.\d+\.\d+):\d+\s+accepted\s+(?:tcp|udp):([^:\s]+):\d+'
)

# Check if destination is a raw IP (not a domain)
RAW_IP_PATTERN = re.compile(r'^\d+\.\d+\.\d+\.\d+$')

DEFAULT_BEHAVIOR_THRESHOLD = 50
DEDUP_WINDOW_SEC = 60
TRACKER_CLEANUP_INTERVAL = 500

DEFAULT_WHITELIST = [
    "127.0.0.1",
    "::1",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
]


class ConnectionTracker:
    """Tracks unique destination IPs per source IP per minute for behavior detection."""

    def __init__(self):
        # source_ip -> {minute_bucket -> set(dest_ips)}
        self._connections: dict[str, dict[int, set[str]]] = {}

    def add_and_check(self, source_ip: str, dest_ip: str, threshold: int) -> bool:
        """Add connection, return True if source_ip exceeds threshold."""
        current_minute = int(time.time()) // 60

        if source_ip not in self._connections:
            self._connections[source_ip] = {}

        ip_data = self._connections[source_ip]

        if current_minute not in ip_data:
            ip_data[current_minute] = set()

        ip_data[current_minute].add(dest_ip)
        return len(ip_data[current_minute]) >= threshold

    def cleanup(self):
        """Remove buckets older than 2 minutes."""
        cutoff = int(time.time()) // 60 - 2
        stale_ips = []
        for ip, minutes in self._connections.items():
            stale_keys = [m for m in minutes if m < cutoff]
            for m in stale_keys:
                del minutes[m]
            if not minutes:
                stale_ips.append(ip)
        for ip in stale_ips:
            del self._connections[ip]

    def remove_ip(self, ip: str):
        """Remove tracked data for a blocked IP (no need to track further)."""
        self._connections.pop(ip, None)


class TorrentBlocker:
    """Monitors xray logs and blocks torrent users via ipset temp ban."""

    def __init__(self):
        self._enabled = False
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._started_at: Optional[datetime] = None

        # Stats
        self._total_blocked = 0
        self._tag_blocks = 0
        self._behavior_blocks = 0
        self._last_block_time: Optional[datetime] = None

        # Dedup: ip -> last_block_timestamp to avoid hammering ipset
        self._block_cache: dict[str, float] = {}

        # Behavior detection
        self._tracker = ConnectionTracker()
        self._behavior_threshold = DEFAULT_BEHAVIOR_THRESHOLD

        # Whitelist: IPs/CIDRs that should never be blocked
        self._whitelist: list[str] = list(DEFAULT_WHITELIST)
        self._whitelist_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []

        self._load_config()

    def _load_config(self):
        try:
            path = Path(PERSISTENT_FILE)
            if path.exists():
                data = json.loads(path.read_text())
                self._enabled = data.get("enabled", False)
                self._behavior_threshold = data.get(
                    "behavior_threshold", DEFAULT_BEHAVIOR_THRESHOLD
                )
                saved_wl = data.get("whitelist")
                if isinstance(saved_wl, list):
                    self._whitelist = saved_wl
        except Exception as e:
            logger.warning(f"Failed to load torrent blocker config: {e}")
        self._rebuild_whitelist_networks()

    def _save_config(self):
        try:
            path = Path(PERSISTENT_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "enabled": self._enabled,
                "behavior_threshold": self._behavior_threshold,
                "whitelist": self._whitelist,
            }, indent=2))
        except Exception as e:
            logger.error(f"Failed to save torrent blocker config: {e}")

    def _rebuild_whitelist_networks(self):
        """Parse whitelist entries into network objects for fast matching."""
        networks = []
        invalid = []
        for entry in self._whitelist:
            try:
                networks.append(ipaddress.ip_network(entry.strip(), strict=False))
            except ValueError:
                invalid.append(entry)
        self._whitelist_networks = networks
        if invalid:
            logger.warning(
                f"Whitelist: {len(networks)} valid, {len(invalid)} invalid entries: {invalid}"
            )
        else:
            logger.info(f"Whitelist rebuilt: {len(networks)} network entries")

    def _is_whitelisted(self, ip: str) -> bool:
        """Check if IP is in the whitelist (supports single IPs and CIDRs)."""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        matched = any(addr in net for net in self._whitelist_networks)
        if matched:
            logger.debug(f"Whitelist match: {ip} — skipping block")
        return matched

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def behavior_threshold(self) -> int:
        return self._behavior_threshold

    def set_behavior_threshold(self, value: int):
        self._behavior_threshold = max(5, min(value, 1000))
        self._save_config()

    @property
    def whitelist(self) -> list[str]:
        return list(self._whitelist)

    def set_whitelist(self, ips: list[str]):
        self._whitelist = ips
        self._rebuild_whitelist_networks()
        self._save_config()
        self._unban_whitelisted()
        logger.info(f"Torrent blocker whitelist updated: {len(ips)} entries")

    def _unban_whitelisted(self):
        """Remove existing temp bans for IPs that are now whitelisted."""
        if not self._whitelist_networks:
            return
        manager = get_ipset_manager()
        active_ips = manager.list_ips(permanent=False, direction="in")
        removed = 0
        for ip_str in active_ips:
            try:
                addr = ipaddress.ip_address(ip_str.split("/")[0])
            except ValueError:
                continue
            if any(addr in net for net in self._whitelist_networks):
                success, _ = manager.remove_ip(ip_str, permanent=False, direction="in")
                if success:
                    removed += 1
                    logger.info(f"Unbanned whitelisted IP from temp blocklist: {ip_str}")
        if removed:
            logger.info(f"Removed {removed} whitelisted IPs from temp blocklist")

    async def _check_container(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return proc.returncode == 0 and stdout.decode().strip() == "true"
        except Exception:
            return False

    def _should_block(self, ip: str) -> bool:
        """Check dedup cache — don't re-block same IP within DEDUP_WINDOW_SEC."""
        now = asyncio.get_event_loop().time()
        last = self._block_cache.get(ip)
        if last and (now - last) < DEDUP_WINDOW_SEC:
            return False
        self._block_cache[ip] = now
        return True

    def _cleanup_cache(self):
        """Remove stale entries from dedup cache."""
        now = asyncio.get_event_loop().time()
        stale = [
            ip for ip, ts in self._block_cache.items()
            if (now - ts) > DEDUP_WINDOW_SEC * 2
        ]
        for ip in stale:
            del self._block_cache[ip]

    async def _kill_connections(self, ip: str):
        """Delete conntrack entries to kill existing connections from blocked IP."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "nsenter", "-t", "1", "-m", "-u", "-n", "-i", "--",
                "conntrack", "-D", "-s", ip,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except Exception:
            pass

    async def _block_ip(self, ip: str, reason: str = "torrent_tag"):
        manager = get_ipset_manager()
        success, msg = manager.add_ip(ip, permanent=False, direction="in")
        if success:
            self._total_blocked += 1
            if reason == "torrent_tag":
                self._tag_blocks += 1
            elif reason == "behavior":
                self._behavior_blocks += 1
            self._last_block_time = datetime.now(timezone.utc)
            await self._kill_connections(ip)
            logger.info(f"Torrent blocker: blocked {ip} (reason: {reason})")
        else:
            logger.warning(f"Torrent blocker: failed to block {ip}: {msg}")

    async def _read_loop(self):
        line_counter = 0

        while self._running:
            try:
                if not await self._check_container():
                    logger.debug("Torrent blocker: remnanode container not running, waiting...")
                    await asyncio.sleep(30)
                    continue

                logger.info("Torrent blocker: starting log monitoring")

                self._process = await asyncio.create_subprocess_exec(
                    "docker", "exec", CONTAINER_NAME,
                    "tail", "-f", "-n", "0", LOG_PATH,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                while self._running and self._process.stdout:
                    try:
                        line_bytes = await asyncio.wait_for(
                            self._process.stdout.readline(), timeout=60.0
                        )
                        if not line_bytes:
                            break

                        line = line_bytes.decode('utf-8', errors='ignore').strip()
                        if not line:
                            continue

                        # 1) Tag-based detection: -> torrent
                        torrent_match = TORRENT_LINE_PATTERN.search(line)
                        if torrent_match:
                            source_ip = torrent_match.group(1)
                            if self._is_whitelisted(source_ip):
                                logger.info(f"Whitelist prevented tag-block for {source_ip}")
                            elif self._should_block(source_ip):
                                await self._block_ip(source_ip, reason="torrent_tag")
                                self._tracker.remove_ip(source_ip)

                        # 2) Behavior-based detection: many unique dest IPs per minute
                        conn_match = ANY_CONNECTION_PATTERN.search(line)
                        if conn_match and not torrent_match:
                            source_ip = conn_match.group(1)
                            dest_host = conn_match.group(2)

                            if RAW_IP_PATTERN.match(dest_host):
                                exceeded = self._tracker.add_and_check(
                                    source_ip, dest_host, self._behavior_threshold
                                )
                                if exceeded:
                                    if self._is_whitelisted(source_ip):
                                        logger.info(f"Whitelist prevented behavior-block for {source_ip}")
                                        self._tracker.remove_ip(source_ip)
                                    elif self._should_block(source_ip):
                                        await self._block_ip(source_ip, reason="behavior")
                                        self._tracker.remove_ip(source_ip)

                        line_counter += 1
                        if line_counter >= TRACKER_CLEANUP_INTERVAL:
                            self._cleanup_cache()
                            self._tracker.cleanup()
                            line_counter = 0

                    except asyncio.TimeoutError:
                        if self._process.returncode is not None:
                            break
                        self._tracker.cleanup()
                        continue

                if self._process:
                    try:
                        self._process.terminate()
                        await asyncio.wait_for(self._process.wait(), timeout=5.0)
                    except Exception:
                        if self._process:
                            self._process.kill()
                    self._process = None

                logger.info("Torrent blocker: log reader ended, restarting...")
                await asyncio.sleep(5)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Torrent blocker error: {e}")
                await asyncio.sleep(10)

    async def start(self):
        if self._running:
            return
        self._running = True
        self._enabled = True
        self._started_at = datetime.now(timezone.utc)
        self._save_config()
        self._task = asyncio.create_task(self._read_loop())
        logger.info("Torrent blocker started")

    async def _graceful_stop(self):
        """Stop the monitoring process without changing enabled state.
        
        Used during node shutdown — preserves config so blocker
        auto-starts on next boot if it was enabled.
        """
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

        logger.info("Torrent blocker gracefully stopped (state preserved)")

    async def disable(self):
        """User-initiated disable — sets enabled=false, saves config, stops process."""
        self._enabled = False
        self._save_config()
        await self._graceful_stop()
        logger.info("Torrent blocker disabled")

    async def auto_start_if_enabled(self):
        """Called on node startup — starts if previously enabled."""
        if self._enabled and not self._running:
            await self.start()

    def get_status(self) -> dict:
        # Query actual active IPs from ipset temp list
        manager = get_ipset_manager()
        active_ips = manager.list_ips(permanent=False, direction="in")

        return {
            "enabled": self._enabled,
            "running": self._running,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "total_blocked": self._total_blocked,
            "tag_blocks": self._tag_blocks,
            "behavior_blocks": self._behavior_blocks,
            "active_blocks": len(active_ips),
            "active_ips": active_ips,
            "last_block_time": self._last_block_time.isoformat() if self._last_block_time else None,
            "behavior_threshold": self._behavior_threshold,
            "whitelist": self._whitelist,
            "whitelist_parsed": len(self._whitelist_networks),
        }


_blocker: Optional[TorrentBlocker] = None


def get_torrent_blocker() -> TorrentBlocker:
    global _blocker
    if _blocker is None:
        _blocker = TorrentBlocker()
    return _blocker
