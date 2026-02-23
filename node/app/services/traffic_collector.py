"""Traffic collector with SQLite storage and per-port tracking via iptables."""

import asyncio
import json
import logging
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from app.config import get_settings

logger = logging.getLogger(__name__)


class TrafficCollector:
    """Collects and stores traffic statistics with per-port tracking."""
    
    IPTABLES_CHAIN_IN = "TRAFFIC_ACCOUNTING_IN"
    IPTABLES_CHAIN_OUT = "TRAFFIC_ACCOUNTING_OUT"
    
    def __init__(self):
        self.settings = get_settings()
        self.db_path = Path(self.settings.traffic_db_path)
        self.config_path = self.db_path.parent / "traffic_config.json"
        self.state_path = self.db_path.parent / "traffic_state.json"
        self._db: Optional[aiosqlite.Connection] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._prev_interface_bytes: dict[str, dict] = {}
        self._prev_port_bytes: dict[int, dict] = {}
        self._last_collect_time: Optional[datetime] = None
        self._tracked_ports: list[int] = []
        self._iptables_available = False
        self._rules_check_counter = 0
        # Cache for summary queries (reduces CPU on frequent requests)
        self._cache_ttl = 120  # 120 seconds (increased from 60)
        self._total_cache: dict[int, tuple[float, dict]] = {}  # days -> (timestamp, result)
        self._port_cache: dict[int, tuple[float, list]] = {}  # days -> (timestamp, result)
        self._iface_cache: dict[int, tuple[float, list]] = {}  # days -> (timestamp, result)
    
    async def init(self):
        """Initialize database, config and iptables rules."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._create_tables()
        self._load_config()
        self._load_state()
        self._check_iptables_available()
        await self._setup_iptables()
        logger.info(f"Traffic collector initialized, db: {self.db_path}, iptables: {self._iptables_available}")
    
    async def _create_tables(self):
        """Create database tables for traffic storage."""
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS interface_traffic (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                interface TEXT NOT NULL,
                rx_bytes INTEGER NOT NULL,
                tx_bytes INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_iface_ts ON interface_traffic(timestamp);
            CREATE INDEX IF NOT EXISTS idx_iface_name ON interface_traffic(interface);
            
            CREATE TABLE IF NOT EXISTS port_traffic (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                port INTEGER NOT NULL,
                protocol TEXT NOT NULL DEFAULT 'tcp',
                rx_bytes INTEGER NOT NULL,
                tx_bytes INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_port_ts ON port_traffic(timestamp);
            CREATE INDEX IF NOT EXISTS idx_port_num ON port_traffic(port);
            
            CREATE TABLE IF NOT EXISTS hourly_traffic (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hour TEXT NOT NULL,
                interface TEXT,
                port INTEGER,
                rx_bytes INTEGER NOT NULL,
                tx_bytes INTEGER NOT NULL,
                UNIQUE(hour, interface, port)
            );
            CREATE INDEX IF NOT EXISTS idx_hourly_hour ON hourly_traffic(hour);
            
            CREATE TABLE IF NOT EXISTS daily_traffic (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                interface TEXT,
                port INTEGER,
                rx_bytes INTEGER NOT NULL,
                tx_bytes INTEGER NOT NULL,
                UNIQUE(date, interface, port)
            );
            CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_traffic(date);
            
            CREATE TABLE IF NOT EXISTS monthly_traffic (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL,
                interface TEXT,
                port INTEGER,
                rx_bytes INTEGER NOT NULL,
                tx_bytes INTEGER NOT NULL,
                UNIQUE(month, interface, port)
            );
            CREATE INDEX IF NOT EXISTS idx_monthly_month ON monthly_traffic(month);
        """)
        await self._db.commit()
    
    def _load_config(self):
        """Load tracked ports from config file."""
        try:
            if self.config_path.exists():
                data = json.loads(self.config_path.read_text())
                self._tracked_ports = data.get("tracked_ports", [])
                logger.info(f"Loaded tracked ports: {self._tracked_ports}")
        except Exception as e:
            logger.warning(f"Failed to load traffic config: {e}")
            self._tracked_ports = []
    
    def _save_config(self):
        """Save tracked ports to config file."""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps({
                "tracked_ports": self._tracked_ports
            }, indent=2))
        except Exception as e:
            logger.error(f"Failed to save traffic config: {e}")
    
    def _load_state(self):
        """Load previous counter state (for handling restarts)."""
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text())
                # Restore previous bytes - used to detect counter resets
                self._prev_interface_bytes = data.get("interface_bytes", {})
                self._prev_port_bytes = {int(k): v for k, v in data.get("port_bytes", {}).items()}
                saved_time = data.get("timestamp")
                if saved_time:
                    self._last_collect_time = datetime.fromisoformat(saved_time)
                logger.info(f"Loaded state from {self.state_path}")
        except Exception as e:
            logger.warning(f"Failed to load state (first run or corrupted): {e}")
    
    def _save_state(self):
        """Save current counter state for graceful restart handling."""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Read current counters to save
            current_iface = self._read_interface_bytes()
            current_ports = self._read_port_bytes()
            
            state = {
                "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                "interface_bytes": current_iface,
                "port_bytes": {str(k): v for k, v in current_ports.items()}
            }
            self.state_path.write_text(json.dumps(state, indent=2))
            logger.debug("State saved")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
    
    def _check_iptables_available(self):
        """Check if iptables is available and we have permissions."""
        try:
            result = subprocess.run(
                ["iptables", "-L", "-n"],
                capture_output=True, timeout=5
            )
            self._iptables_available = result.returncode == 0
            if not self._iptables_available:
                logger.warning("iptables not available or no permissions - port tracking disabled")
        except Exception as e:
            logger.warning(f"iptables check failed: {e}")
            self._iptables_available = False
    
    def get_tracked_ports(self) -> list[int]:
        """Get list of currently tracked ports."""
        return self._tracked_ports.copy()
    
    async def add_tracked_port(self, port: int) -> dict:
        """Add a port to tracking."""
        if port in self._tracked_ports:
            return {"success": False, "message": f"Port {port} already tracked"}
        
        if not self._iptables_available:
            return {"success": False, "message": "iptables not available - port tracking disabled"}
        
        if not self._add_iptables_rules(port):
            return {"success": False, "message": f"Failed to add iptables rules for port {port}"}
        
        self._tracked_ports.append(port)
        self._tracked_ports.sort()
        self._save_config()
        logger.info(f"Added port {port} to tracking")
        return {"success": True, "message": f"Port {port} added to tracking"}
    
    async def remove_tracked_port(self, port: int) -> dict:
        """Remove a port from tracking."""
        if port not in self._tracked_ports:
            return {"success": False, "message": f"Port {port} not tracked"}
        
        self._remove_iptables_rules(port)
        self._tracked_ports.remove(port)
        self._save_config()
        logger.info(f"Removed port {port} from tracking")
        return {"success": True, "message": f"Port {port} removed from tracking"}
    
    def _run_iptables(self, cmd: str, check: bool = False) -> bool:
        """Run iptables command."""
        try:
            result = subprocess.run(cmd.split(), capture_output=True, timeout=10)
            return result.returncode == 0
        except Exception as e:
            if not check:
                logger.error(f"iptables command failed: {cmd} - {e}")
            return False
    
    def _check_chain_exists(self, chain: str) -> bool:
        """Check if iptables chain exists."""
        return self._run_iptables(f"iptables -L {chain} -n", check=True)
    
    def _check_rule_exists(self, chain: str, port: int, direction: str, protocol: str = "tcp") -> bool:
        """Check if specific rule exists in chain."""
        flag = "--dport" if direction == "in" else "--sport"
        return self._run_iptables(f"iptables -C {chain} -p {protocol} {flag} {port}", check=True)
    
    def _add_iptables_rules(self, port: int) -> bool:
        """Add iptables rules for a port."""
        if not self._iptables_available:
            return False
        
        try:
            # TCP incoming
            if not self._check_rule_exists(self.IPTABLES_CHAIN_IN, port, "in", "tcp"):
                self._run_iptables(f"iptables -A {self.IPTABLES_CHAIN_IN} -p tcp --dport {port}")
            # TCP outgoing
            if not self._check_rule_exists(self.IPTABLES_CHAIN_OUT, port, "out", "tcp"):
                self._run_iptables(f"iptables -A {self.IPTABLES_CHAIN_OUT} -p tcp --sport {port}")
            # UDP incoming
            if not self._check_rule_exists(self.IPTABLES_CHAIN_IN, port, "in", "udp"):
                self._run_iptables(f"iptables -A {self.IPTABLES_CHAIN_IN} -p udp --dport {port}")
            # UDP outgoing
            if not self._check_rule_exists(self.IPTABLES_CHAIN_OUT, port, "out", "udp"):
                self._run_iptables(f"iptables -A {self.IPTABLES_CHAIN_OUT} -p udp --sport {port}")
            return True
        except Exception as e:
            logger.error(f"Failed to add iptables rules for port {port}: {e}")
            return False
    
    def _remove_iptables_rules(self, port: int):
        """Remove iptables rules for a port."""
        if not self._iptables_available:
            return
        
        try:
            self._run_iptables(f"iptables -D {self.IPTABLES_CHAIN_IN} -p tcp --dport {port}")
            self._run_iptables(f"iptables -D {self.IPTABLES_CHAIN_OUT} -p tcp --sport {port}")
            self._run_iptables(f"iptables -D {self.IPTABLES_CHAIN_IN} -p udp --dport {port}")
            self._run_iptables(f"iptables -D {self.IPTABLES_CHAIN_OUT} -p udp --sport {port}")
        except Exception as e:
            logger.warning(f"Failed to remove some iptables rules for port {port}: {e}")
    
    def _ensure_iptables_rules(self):
        """Ensure all iptables chains and rules exist (called periodically)."""
        if not self._iptables_available or not self._tracked_ports:
            return
        
        # Check/create chains
        if not self._check_chain_exists(self.IPTABLES_CHAIN_IN):
            self._run_iptables(f"iptables -N {self.IPTABLES_CHAIN_IN}")
            self._run_iptables(f"iptables -I INPUT -j {self.IPTABLES_CHAIN_IN}")
            logger.info(f"Recreated chain {self.IPTABLES_CHAIN_IN}")
        
        if not self._check_chain_exists(self.IPTABLES_CHAIN_OUT):
            self._run_iptables(f"iptables -N {self.IPTABLES_CHAIN_OUT}")
            self._run_iptables(f"iptables -I OUTPUT -j {self.IPTABLES_CHAIN_OUT}")
            logger.info(f"Recreated chain {self.IPTABLES_CHAIN_OUT}")
        
        # Check/add rules for each port
        for port in self._tracked_ports:
            self._add_iptables_rules(port)
    
    async def _setup_iptables(self):
        """Setup iptables chains for port traffic accounting."""
        if not self._iptables_available:
            logger.info("Skipping iptables setup - not available")
            return
        
        # Create chains if not exist
        self._run_iptables(f"iptables -N {self.IPTABLES_CHAIN_IN}")
        self._run_iptables(f"iptables -N {self.IPTABLES_CHAIN_OUT}")
        
        # Ensure chains are in INPUT/OUTPUT (check first to avoid duplicates)
        if not self._run_iptables(f"iptables -C INPUT -j {self.IPTABLES_CHAIN_IN}", check=True):
            self._run_iptables(f"iptables -I INPUT -j {self.IPTABLES_CHAIN_IN}")
        
        if not self._run_iptables(f"iptables -C OUTPUT -j {self.IPTABLES_CHAIN_OUT}", check=True):
            self._run_iptables(f"iptables -I OUTPUT -j {self.IPTABLES_CHAIN_OUT}")
        
        # Add rules for saved ports
        for port in self._tracked_ports:
            self._add_iptables_rules(port)
        
        if self._tracked_ports:
            logger.info(f"iptables rules configured for ports: {self._tracked_ports}")
    
    def _read_interface_bytes(self) -> dict[str, dict]:
        """Read current interface bytes from /proc/net/dev."""
        result = {}
        try:
            with open("/proc/net/dev") as f:
                for line in f.readlines()[2:]:
                    if ":" not in line:
                        continue
                    parts = line.split(":")
                    iface = parts[0].strip()
                    if iface == "lo":
                        continue
                    values = parts[1].split()
                    if len(values) >= 16:
                        result[iface] = {
                            "rx_bytes": int(values[0]),
                            "tx_bytes": int(values[8])
                        }
        except Exception as e:
            logger.error(f"Error reading /proc/net/dev: {e}")
        return result
    
    def _read_port_bytes(self) -> dict[int, dict]:
        """Read port traffic from iptables counters."""
        result = {}
        if not self._iptables_available or not self._tracked_ports:
            return result
        
        for port in self._tracked_ports:
            result[port] = {"rx_bytes": 0, "tx_bytes": 0}
        
        try:
            # Read incoming traffic
            proc = subprocess.run(
                ["iptables", "-L", self.IPTABLES_CHAIN_IN, "-v", "-n", "-x"],
                capture_output=True, text=True, timeout=10
            )
            if proc.returncode == 0:
                for line in proc.stdout.split("\n"):
                    for port in self._tracked_ports:
                        if f"dpt:{port}" in line:
                            parts = line.split()
                            if len(parts) >= 2:
                                try:
                                    result[port]["rx_bytes"] += int(parts[1])
                                except ValueError:
                                    pass
            
            # Read outgoing traffic
            proc = subprocess.run(
                ["iptables", "-L", self.IPTABLES_CHAIN_OUT, "-v", "-n", "-x"],
                capture_output=True, text=True, timeout=10
            )
            if proc.returncode == 0:
                for line in proc.stdout.split("\n"):
                    for port in self._tracked_ports:
                        if f"spt:{port}" in line:
                            parts = line.split()
                            if len(parts) >= 2:
                                try:
                                    result[port]["tx_bytes"] += int(parts[1])
                                except ValueError:
                                    pass
        except Exception as e:
            logger.error(f"Error reading iptables counters: {e}")
        
        return result
    
    def _calculate_delta(self, current: int, previous: int) -> int:
        """Calculate delta handling counter resets (reboot)."""
        if previous == 0:
            # First collection after start - don't count existing bytes
            return 0
        
        delta = current - previous
        
        if delta < 0:
            # Counter reset (reboot) - count from 0
            logger.debug(f"Counter reset detected: current={current}, previous={previous}")
            return current
        
        return delta
    
    async def collect_snapshot(self):
        """Collect and store current traffic snapshot."""
        if not self._db:
            return
        
        # Use UTC for consistent timestamps across timezones
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat().replace('+00:00', 'Z')
        hour = now.strftime("%Y-%m-%d %H:00")
        date = now.strftime("%Y-%m-%d")
        month = now.strftime("%Y-%m")
        
        # Collect interface traffic
        current_iface = self._read_interface_bytes()
        for iface, data in current_iface.items():
            prev = self._prev_interface_bytes.get(iface, {"rx_bytes": 0, "tx_bytes": 0})
            rx_delta = self._calculate_delta(data["rx_bytes"], prev["rx_bytes"])
            tx_delta = self._calculate_delta(data["tx_bytes"], prev["tx_bytes"])
            
            if rx_delta > 0 or tx_delta > 0:
                await self._db.execute(
                    "INSERT INTO interface_traffic (timestamp, interface, rx_bytes, tx_bytes) VALUES (?, ?, ?, ?)",
                    (timestamp, iface, rx_delta, tx_delta)
                )
                
                await self._db.execute("""
                    INSERT INTO hourly_traffic (hour, interface, port, rx_bytes, tx_bytes)
                    VALUES (?, ?, NULL, ?, ?)
                    ON CONFLICT(hour, interface, port) DO UPDATE SET
                        rx_bytes = rx_bytes + excluded.rx_bytes,
                        tx_bytes = tx_bytes + excluded.tx_bytes
                """, (hour, iface, rx_delta, tx_delta))
                
                await self._db.execute("""
                    INSERT INTO daily_traffic (date, interface, port, rx_bytes, tx_bytes)
                    VALUES (?, ?, NULL, ?, ?)
                    ON CONFLICT(date, interface, port) DO UPDATE SET
                        rx_bytes = rx_bytes + excluded.rx_bytes,
                        tx_bytes = tx_bytes + excluded.tx_bytes
                """, (date, iface, rx_delta, tx_delta))
                
                await self._db.execute("""
                    INSERT INTO monthly_traffic (month, interface, port, rx_bytes, tx_bytes)
                    VALUES (?, ?, NULL, ?, ?)
                    ON CONFLICT(month, interface, port) DO UPDATE SET
                        rx_bytes = rx_bytes + excluded.rx_bytes,
                        tx_bytes = tx_bytes + excluded.tx_bytes
                """, (month, iface, rx_delta, tx_delta))
        
        self._prev_interface_bytes = current_iface
        
        # Collect port traffic
        current_ports = self._read_port_bytes()
        for port, data in current_ports.items():
            prev = self._prev_port_bytes.get(port, {"rx_bytes": 0, "tx_bytes": 0})
            rx_delta = self._calculate_delta(data["rx_bytes"], prev["rx_bytes"])
            tx_delta = self._calculate_delta(data["tx_bytes"], prev["tx_bytes"])
            
            if rx_delta > 0 or tx_delta > 0:
                await self._db.execute(
                    "INSERT INTO port_traffic (timestamp, port, rx_bytes, tx_bytes) VALUES (?, ?, ?, ?)",
                    (timestamp, port, rx_delta, tx_delta)
                )
                
                await self._db.execute("""
                    INSERT INTO hourly_traffic (hour, interface, port, rx_bytes, tx_bytes)
                    VALUES (?, NULL, ?, ?, ?)
                    ON CONFLICT(hour, interface, port) DO UPDATE SET
                        rx_bytes = rx_bytes + excluded.rx_bytes,
                        tx_bytes = tx_bytes + excluded.tx_bytes
                """, (hour, port, rx_delta, tx_delta))
                
                await self._db.execute("""
                    INSERT INTO daily_traffic (date, interface, port, rx_bytes, tx_bytes)
                    VALUES (?, NULL, ?, ?, ?)
                    ON CONFLICT(date, interface, port) DO UPDATE SET
                        rx_bytes = rx_bytes + excluded.rx_bytes,
                        tx_bytes = tx_bytes + excluded.tx_bytes
                """, (date, port, rx_delta, tx_delta))
                
                await self._db.execute("""
                    INSERT INTO monthly_traffic (month, interface, port, rx_bytes, tx_bytes)
                    VALUES (?, NULL, ?, ?, ?)
                    ON CONFLICT(month, interface, port) DO UPDATE SET
                        rx_bytes = rx_bytes + excluded.rx_bytes,
                        tx_bytes = tx_bytes + excluded.tx_bytes
                """, (month, port, rx_delta, tx_delta))
        
        self._prev_port_bytes = current_ports
        await self._db.commit()
        self._last_collect_time = now
    
    async def cleanup_old_data(self):
        """Remove data older than retention period."""
        if not self._db:
            return
        
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.settings.traffic_retention_days)).isoformat().replace('+00:00', 'Z')
        await self._db.execute("DELETE FROM interface_traffic WHERE timestamp < ?", (cutoff,))
        await self._db.execute("DELETE FROM port_traffic WHERE timestamp < ?", (cutoff,))
        await self._db.commit()
        logger.info(f"Cleaned up traffic data older than {cutoff}")
    
    async def _collection_loop(self):
        """Background loop for collecting traffic snapshots."""
        cleanup_counter = 0
        state_save_counter = 0
        
        while self._running:
            try:
                await self.collect_snapshot()
                cleanup_counter += 1
                state_save_counter += 1
                self._rules_check_counter += 1
                
                # Save state every 5 minutes
                if state_save_counter >= 300 // self.settings.traffic_collect_interval:
                    self._save_state()
                    state_save_counter = 0
                
                # Check iptables rules every 10 minutes
                if self._rules_check_counter >= 600 // self.settings.traffic_collect_interval:
                    self._ensure_iptables_rules()
                    self._rules_check_counter = 0
                
                # Cleanup once per day
                if cleanup_counter >= 86400 // self.settings.traffic_collect_interval:
                    await self.cleanup_old_data()
                    cleanup_counter = 0
                    
            except Exception as e:
                logger.error(f"Error in traffic collection: {e}")
            
            await asyncio.sleep(self.settings.traffic_collect_interval)
    
    async def start(self):
        """Start background traffic collection."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._collection_loop())
        logger.info("Traffic collector started")
    
    async def stop(self):
        """Stop traffic collection with graceful shutdown."""
        self._running = False
        
        # Save state before stopping
        self._save_state()
        logger.info("Traffic state saved before shutdown")
        
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._db:
            await self._db.close()
        logger.info("Traffic collector stopped")
    
    # Query methods
    async def get_hourly_traffic(
        self,
        hours: int = 24,
        interface: Optional[str] = None,
        port: Optional[int] = None
    ) -> list[dict]:
        """Get hourly traffic for the last N hours."""
        if not self._db:
            return []
        
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:00")
        
        if interface:
            cursor = await self._db.execute(
                "SELECT hour, rx_bytes, tx_bytes FROM hourly_traffic WHERE hour >= ? AND interface = ? AND port IS NULL ORDER BY hour",
                (cutoff, interface)
            )
        elif port:
            cursor = await self._db.execute(
                "SELECT hour, rx_bytes, tx_bytes FROM hourly_traffic WHERE hour >= ? AND port = ? ORDER BY hour",
                (cutoff, port)
            )
        else:
            cursor = await self._db.execute(
                "SELECT hour, SUM(rx_bytes), SUM(tx_bytes) FROM hourly_traffic WHERE hour >= ? AND port IS NULL GROUP BY hour ORDER BY hour",
                (cutoff,)
            )
        
        rows = await cursor.fetchall()
        return [{"hour": r[0], "rx_bytes": r[1], "tx_bytes": r[2]} for r in rows]
    
    async def get_daily_traffic(
        self,
        days: int = 30,
        interface: Optional[str] = None,
        port: Optional[int] = None
    ) -> list[dict]:
        """Get daily traffic for the last N days."""
        if not self._db:
            return []
        
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        
        if interface:
            cursor = await self._db.execute(
                "SELECT date, rx_bytes, tx_bytes FROM daily_traffic WHERE date >= ? AND interface = ? AND port IS NULL ORDER BY date",
                (cutoff, interface)
            )
        elif port:
            cursor = await self._db.execute(
                "SELECT date, rx_bytes, tx_bytes FROM daily_traffic WHERE date >= ? AND port = ? ORDER BY date",
                (cutoff, port)
            )
        else:
            cursor = await self._db.execute(
                "SELECT date, SUM(rx_bytes), SUM(tx_bytes) FROM daily_traffic WHERE date >= ? AND port IS NULL GROUP BY date ORDER BY date",
                (cutoff,)
            )
        
        rows = await cursor.fetchall()
        return [{"date": r[0], "rx_bytes": r[1], "tx_bytes": r[2]} for r in rows]
    
    async def get_monthly_traffic(
        self,
        months: int = 12,
        interface: Optional[str] = None,
        port: Optional[int] = None
    ) -> list[dict]:
        """Get monthly traffic for the last N months."""
        if not self._db:
            return []
        
        cutoff = (datetime.now(timezone.utc) - timedelta(days=months * 30)).strftime("%Y-%m")
        
        if interface:
            cursor = await self._db.execute(
                "SELECT month, rx_bytes, tx_bytes FROM monthly_traffic WHERE month >= ? AND interface = ? AND port IS NULL ORDER BY month",
                (cutoff, interface)
            )
        elif port:
            cursor = await self._db.execute(
                "SELECT month, rx_bytes, tx_bytes FROM monthly_traffic WHERE month >= ? AND port = ? ORDER BY month",
                (cutoff, port)
            )
        else:
            cursor = await self._db.execute(
                "SELECT month, SUM(rx_bytes), SUM(tx_bytes) FROM monthly_traffic WHERE month >= ? AND port IS NULL GROUP BY month ORDER BY month",
                (cutoff,)
            )
        
        rows = await cursor.fetchall()
        return [{"month": r[0], "rx_bytes": r[1], "tx_bytes": r[2]} for r in rows]
    
    async def get_port_summary(self, days: int = 30) -> list[dict]:
        """Get traffic summary per port for the last N days (cached 60s)."""
        now = time.time()
        if days in self._port_cache:
            cache_time, result = self._port_cache[days]
            if now - cache_time < self._cache_ttl:
                return result
        
        if not self._db:
            return []
        
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        cursor = await self._db.execute(
            "SELECT port, SUM(rx_bytes), SUM(tx_bytes) FROM daily_traffic WHERE date >= ? AND port IS NOT NULL GROUP BY port ORDER BY port",
            (cutoff,)
        )
        rows = await cursor.fetchall()
        result = [{"port": r[0], "rx_bytes": r[1], "tx_bytes": r[2]} for r in rows]
        self._port_cache[days] = (now, result)
        return result
    
    async def get_interface_summary(self, days: int = 30) -> list[dict]:
        """Get traffic summary per interface for the last N days (cached 60s)."""
        now = time.time()
        if days in self._iface_cache:
            cache_time, result = self._iface_cache[days]
            if now - cache_time < self._cache_ttl:
                return result
        
        if not self._db:
            return []
        
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        cursor = await self._db.execute(
            "SELECT interface, SUM(rx_bytes), SUM(tx_bytes) FROM daily_traffic WHERE date >= ? AND interface IS NOT NULL AND port IS NULL GROUP BY interface ORDER BY interface",
            (cutoff,)
        )
        rows = await cursor.fetchall()
        result = [{"interface": r[0], "rx_bytes": r[1], "tx_bytes": r[2]} for r in rows]
        self._iface_cache[days] = (now, result)
        return result
    
    async def get_total_traffic(self, days: int = 30) -> dict:
        """Get total traffic for the last N days (cached 60s)."""
        now = time.time()
        if days in self._total_cache:
            cache_time, result = self._total_cache[days]
            if now - cache_time < self._cache_ttl:
                return result
        
        if not self._db:
            return {"rx_bytes": 0, "tx_bytes": 0, "days": days}
        
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        cursor = await self._db.execute(
            "SELECT SUM(rx_bytes), SUM(tx_bytes) FROM daily_traffic WHERE date >= ? AND port IS NULL",
            (cutoff,)
        )
        row = await cursor.fetchone()
        result = {
            "rx_bytes": row[0] or 0,
            "tx_bytes": row[1] or 0,
            "days": days
        }
        self._total_cache[days] = (now, result)
        return result


# Singleton
_collector: Optional[TrafficCollector] = None


def get_traffic_collector() -> TrafficCollector:
    """Get or create traffic collector instance."""
    global _collector
    if _collector is None:
        _collector = TrafficCollector()
    return _collector
