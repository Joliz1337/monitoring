"""Torrent blocker service for Xray nodes.

Monitors Xray access logs for torrent-routed connections (-> torrent),
extracts source IPs, and blocks them via ipset temporary ban.

Persists enabled state across restarts in /var/lib/monitoring/torrent_blocker.json.
"""

import asyncio
import json
import logging
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.services.ipset_manager import get_ipset_manager

logger = logging.getLogger(__name__)

PERSISTENT_FILE = "/var/lib/monitoring/torrent_blocker.json"
CONTAINER_NAME = "remnanode"
LOG_PATH = "/var/log/supervisor/xray.out.log"

# Regex: extract source IP from torrent-routed lines
# Example: "2026/02/11 18:37:38.637506 from tcp:92.39.216.40:53396 accepted udp:65.108.224.72:6881 [dom3 -> torrent] email: 4065"
TORRENT_LINE_PATTERN = re.compile(
    r'from (?:tcp:)?(\d+\.\d+\.\d+\.\d+):\d+\s+accepted\s+.+?\[.+?->\s*torrent\]'
)

MAX_RECENT_BLOCKS = 50
DEDUP_WINDOW_SEC = 60


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
        self._unique_ips: set[str] = set()
        self._recent_blocks: deque[dict] = deque(maxlen=MAX_RECENT_BLOCKS)
        self._last_block_time: Optional[datetime] = None

        # Dedup: ip -> last_block_timestamp to avoid hammering ipset
        self._block_cache: dict[str, float] = {}

        self._load_config()

    def _load_config(self):
        try:
            path = Path(PERSISTENT_FILE)
            if path.exists():
                data = json.loads(path.read_text())
                self._enabled = data.get("enabled", False)
        except Exception as e:
            logger.warning(f"Failed to load torrent blocker config: {e}")

    def _save_config(self):
        try:
            path = Path(PERSISTENT_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"enabled": self._enabled}, indent=2))
        except Exception as e:
            logger.error(f"Failed to save torrent blocker config: {e}")

    @property
    def is_enabled(self) -> bool:
        return self._enabled

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
        stale = [ip for ip, ts in self._block_cache.items() if (now - ts) > DEDUP_WINDOW_SEC * 2]
        for ip in stale:
            del self._block_cache[ip]

    def _block_ip(self, ip: str):
        manager = get_ipset_manager()
        success, msg = manager.add_ip(ip, permanent=False, direction="in")
        if success:
            self._total_blocked += 1
            self._unique_ips.add(ip)
            self._last_block_time = datetime.now(timezone.utc)
            self._recent_blocks.append({
                "ip": ip,
                "time": self._last_block_time.isoformat()
            })
            logger.info(f"Torrent blocker: blocked {ip}")
        else:
            logger.warning(f"Torrent blocker: failed to block {ip}: {msg}")

    async def _read_loop(self):
        cleanup_counter = 0

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

                        match = TORRENT_LINE_PATTERN.search(line)
                        if not match:
                            continue

                        source_ip = match.group(1)
                        if self._should_block(source_ip):
                            self._block_ip(source_ip)

                        cleanup_counter += 1
                        if cleanup_counter >= 1000:
                            self._cleanup_cache()
                            cleanup_counter = 0

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

    async def stop(self):
        self._running = False
        self._enabled = False
        self._save_config()

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

        logger.info("Torrent blocker stopped")

    async def auto_start_if_enabled(self):
        """Called on node startup — starts if previously enabled."""
        if self._enabled and not self._running:
            await self.start()

    def get_status(self) -> dict:
        return {
            "enabled": self._enabled,
            "running": self._running,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "total_blocked": self._total_blocked,
            "unique_ips_blocked": len(self._unique_ips),
            "last_block_time": self._last_block_time.isoformat() if self._last_block_time else None,
            "recent_blocks": list(self._recent_blocks),
            "cache_size": len(self._block_cache),
        }


_blocker: Optional[TorrentBlocker] = None


def get_torrent_blocker() -> TorrentBlocker:
    global _blocker
    if _blocker is None:
        _blocker = TorrentBlocker()
    return _blocker
