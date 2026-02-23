"""System metrics collector using psutil - returns raw current values only.
All speed calculations are done on the panel side.
"""

import os
import socket
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

from app.config import get_settings


class MetricsCollector:
    """Collects current system metrics from host - raw values only.
    Speed calculations are done on the panel side.
    """
    
    def __init__(self):
        self.settings = get_settings()
        # Initialize CPU baseline for non-blocking calls
        psutil.cpu_percent(percpu=True)
        self._last_cpu_percent: list = [0.0] * (psutil.cpu_count() or 1)
        # Process cache to avoid blocking
        self._processes_cache: list = []
        self._processes_cache_time: float = 0
        self._processes_cache_ttl: float = 5.0  # 5 seconds
        # System info cache (connections parsing is heavy)
        self._system_cache: dict = {}
        self._system_cache_time: float = 0
        self._system_cache_ttl: float = 5.0  # 5 seconds
    
    def _read_host_file(self, path: str) -> str:
        """Read file from host filesystem"""
        host_path = Path(self.settings.host_proc).parent / path.lstrip('/')
        if host_path.exists():
            return host_path.read_text(encoding='utf-8', errors='replace')
        fallback = Path(path)
        if fallback.exists():
            return fallback.read_text(encoding='utf-8', errors='replace')
        return ""
    
    def get_cpu_info(self) -> dict:
        """Get CPU information and usage"""
        cpu_count_logical = psutil.cpu_count(logical=True) or 1
        cpu_count_physical = psutil.cpu_count(logical=False) or 1
        
        per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        if per_cpu and all(v == 0.0 for v in per_cpu):
            per_cpu = self._last_cpu_percent
        else:
            self._last_cpu_percent = per_cpu
        
        try:
            load_avg = os.getloadavg()
        except (OSError, AttributeError):
            load_avg = (0.0, 0.0, 0.0)
        
        freq = psutil.cpu_freq()
        cpu_freq = {
            "current": freq.current if freq else 0,
            "min": freq.min if freq else 0,
            "max": freq.max if freq else 0
        }
        
        temps = {}
        try:
            temp_data = psutil.sensors_temperatures()
            if temp_data:
                for name, entries in temp_data.items():
                    temps[name] = [
                        {"label": e.label or f"core_{i}", "current": e.current, "high": e.high, "critical": e.critical}
                        for i, e in enumerate(entries)
                    ]
        except (AttributeError, Exception):
            pass
        
        model = "Unknown"
        cpuinfo = self._read_host_file("/proc/cpuinfo")
        for line in cpuinfo.split('\n'):
            if line.startswith('model name'):
                model = line.split(':')[1].strip()
                break
        
        return {
            "cores_physical": cpu_count_physical,
            "cores_logical": cpu_count_logical,
            "model": model,
            "usage_percent": sum(per_cpu) / len(per_cpu) if per_cpu else 0,
            "per_cpu_percent": per_cpu,
            "load_avg_1": load_avg[0],
            "load_avg_5": load_avg[1],
            "load_avg_15": load_avg[2],
            "frequency": cpu_freq,
            "temperatures": temps
        }
    
    def get_memory_info(self) -> dict:
        """Get RAM and swap information"""
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        
        return {
            "ram": {
                "total": mem.total,
                "used": mem.used,
                "free": mem.free,
                "available": mem.available,
                "percent": mem.percent,
                "buffers": getattr(mem, 'buffers', 0),
                "cached": getattr(mem, 'cached', 0)
            },
            "swap": {
                "total": swap.total,
                "used": swap.used,
                "free": swap.free,
                "percent": swap.percent
            }
        }
    
    def get_disk_info(self) -> dict:
        """Get disk partitions and usage - raw bytes, no speed calculation"""
        partitions = []
        
        for part in psutil.disk_partitions(all=False):
            if part.fstype and not part.mountpoint.startswith(('/snap', '/boot/efi')):
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    partitions.append({
                        "device": part.device,
                        "mountpoint": part.mountpoint,
                        "fstype": part.fstype,
                        "total": usage.total,
                        "used": usage.used,
                        "free": usage.free,
                        "percent": usage.percent
                    })
                except (PermissionError, OSError):
                    continue
        
        # Disk I/O - raw bytes only
        io_counters = psutil.disk_io_counters(perdisk=True)
        io_stats = {}
        
        for disk, counters in (io_counters or {}).items():
            io_stats[disk] = {
                "read_bytes": counters.read_bytes,
                "write_bytes": counters.write_bytes,
                "read_count": counters.read_count,
                "write_count": counters.write_count,
                "read_time_ms": counters.read_time,
                "write_time_ms": counters.write_time
            }
        
        return {
            "partitions": partitions,
            "io": io_stats
        }
    
    def _read_host_net_dev(self) -> dict[str, dict]:
        """Read network stats - with network_mode: host psutil sees real traffic"""
        result = {}
        # With network_mode: host, use standard /proc/net/dev (real host network)
        net_dev_path = Path("/proc/net/dev")
        
        try:
            content = net_dev_path.read_text()
            for line in content.split('\n')[2:]:  # Skip headers
                if ':' not in line:
                    continue
                parts = line.split(':')
                iface = parts[0].strip()
                if iface == 'lo':
                    continue
                values = parts[1].split()
                if len(values) >= 16:
                    result[iface] = {
                        'rx_bytes': int(values[0]),
                        'rx_packets': int(values[1]),
                        'rx_errors': int(values[2]),
                        'rx_drops': int(values[3]),
                        'tx_bytes': int(values[8]),
                        'tx_packets': int(values[9]),
                        'tx_errors': int(values[10]),
                        'tx_drops': int(values[11]),
                    }
        except Exception:
            pass
        return result
    
    def get_network_info(self) -> dict:
        """Get network interfaces - raw bytes only, speed calculated on panel"""
        interfaces = []
        
        # Read from HOST's /proc/net/dev for real traffic
        host_net_stats = self._read_host_net_dev()
        
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        
        # Process interfaces from host stats
        for iface, io in host_net_stats.items():
            iface_info = {
                "name": iface,
                "addresses": [],
                "mac": None,
                "mtu": None,
                "speed_mbps": None,
                "is_up": True,
                "rx_bytes": io['rx_bytes'],
                "tx_bytes": io['tx_bytes'],
                "rx_packets": io['rx_packets'],
                "tx_packets": io['tx_packets'],
                "rx_errors": io['rx_errors'],
                "tx_errors": io['tx_errors'],
                "rx_drops": io['rx_drops'],
                "tx_drops": io['tx_drops'],
                # Speed fields for backward compatibility (panel calculates actual values)
                "rx_bytes_per_sec": 0.0,
                "tx_bytes_per_sec": 0.0,
            }
            
            # Get addresses if available (from container's view)
            if iface in addrs:
                for addr in addrs[iface]:
                    if addr.family == socket.AF_INET:
                        iface_info["addresses"].append({
                            "type": "ipv4",
                            "address": addr.address,
                            "netmask": addr.netmask
                        })
                    elif addr.family == socket.AF_INET6:
                        iface_info["addresses"].append({
                            "type": "ipv6",
                            "address": addr.address
                        })
                    elif addr.family == psutil.AF_LINK:
                        iface_info["mac"] = addr.address
            
            if iface in stats:
                s = stats[iface]
                iface_info["mtu"] = s.mtu
                iface_info["speed_mbps"] = s.speed if s.speed > 0 else None
                iface_info["is_up"] = s.isup
            
            interfaces.append(iface_info)
        
        # Total traffic from host
        total_rx = sum(io['rx_bytes'] for io in host_net_stats.values())
        total_tx = sum(io['tx_bytes'] for io in host_net_stats.values())
        total_rx_packets = sum(io['rx_packets'] for io in host_net_stats.values())
        total_tx_packets = sum(io['tx_packets'] for io in host_net_stats.values())
        
        total = {
            "rx_bytes": total_rx,
            "tx_bytes": total_tx,
            "rx_packets": total_rx_packets,
            "tx_packets": total_tx_packets,
            # Speed fields for backward compatibility (panel calculates actual values)
            "rx_bytes_per_sec": 0.0,
            "tx_bytes_per_sec": 0.0
        }
        
        return {
            "interfaces": interfaces,
            "total": total
        }
    
    def get_processes_info(self, top_n: int = 10) -> dict:
        """Get process statistics and top processes with caching to avoid blocking"""
        current_time = time.time()
        cpu_count = psutil.cpu_count() or 1
        
        # Cache processes for 5 seconds to avoid blocking on frequent requests
        if current_time - self._processes_cache_time > self._processes_cache_ttl or not self._processes_cache:
            processes = []
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status']):
                try:
                    pinfo = proc.info
                    # Normalize cpu_percent to 0-100% range (psutil returns 0 to 100*cpu_count)
                    raw_cpu = pinfo['cpu_percent'] or 0
                    normalized_cpu = raw_cpu / cpu_count
                    processes.append({
                        "pid": pinfo['pid'],
                        "name": pinfo['name'],
                        "cpu_percent": round(normalized_cpu, 1),
                        "memory_percent": pinfo['memory_percent'] or 0,
                        "status": pinfo['status']
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            self._processes_cache = processes
            self._processes_cache_time = current_time
        
        processes = self._processes_cache
        top_by_cpu = sorted(processes, key=lambda x: x['cpu_percent'], reverse=True)[:top_n]
        top_by_memory = sorted(processes, key=lambda x: x['memory_percent'], reverse=True)[:top_n]
        
        return {
            "total": len(processes),
            "running": sum(1 for p in processes if p['status'] == 'running'),
            "sleeping": sum(1 for p in processes if p['status'] == 'sleeping'),
            "top_by_cpu": top_by_cpu,
            "top_by_memory": top_by_memory
        }
    
    def _read_host_connections(self) -> dict:
        """Read TCP/UDP connection stats from host's /proc/net/*"""
        # TCP states mapping (hex -> name)
        tcp_states = {
            '01': 'established',
            '02': 'syn_sent',
            '03': 'syn_recv',
            '04': 'fin_wait1',
            '05': 'fin_wait2',
            '06': 'time_wait',
            '07': 'close',
            '08': 'close_wait',
            '09': 'last_ack',
            '0A': 'listen',
            '0B': 'closing',
        }
        
        tcp_stats = {
            'total': 0,
            'established': 0,
            'listen': 0,
            'time_wait': 0,
            'close_wait': 0,
            'syn_sent': 0,
            'syn_recv': 0,
            'fin_wait': 0,
            'other': 0,
        }
        udp_stats = {'total': 0}
        
        # Read TCP (IPv4 + IPv6)
        for tcp_file in ['/proc/net/tcp', '/proc/net/tcp6']:
            host_path = Path(self.settings.host_proc) / tcp_file.lstrip('/proc/')
            try:
                if host_path.exists():
                    content = host_path.read_text()
                    for line in content.strip().split('\n')[1:]:  # Skip header
                        parts = line.split()
                        if len(parts) >= 4:
                            state = parts[3].upper()
                            tcp_stats['total'] += 1
                            state_name = tcp_states.get(state, 'other')
                            if state_name == 'established':
                                tcp_stats['established'] += 1
                            elif state_name == 'listen':
                                tcp_stats['listen'] += 1
                            elif state_name == 'time_wait':
                                tcp_stats['time_wait'] += 1
                            elif state_name == 'close_wait':
                                tcp_stats['close_wait'] += 1
                            elif state_name == 'syn_sent':
                                tcp_stats['syn_sent'] += 1
                            elif state_name == 'syn_recv':
                                tcp_stats['syn_recv'] += 1
                            elif state_name in ('fin_wait1', 'fin_wait2'):
                                tcp_stats['fin_wait'] += 1
                            else:
                                tcp_stats['other'] += 1
            except Exception:
                pass
        
        # Read UDP (IPv4 + IPv6)
        for udp_file in ['/proc/net/udp', '/proc/net/udp6']:
            host_path = Path(self.settings.host_proc) / udp_file.lstrip('/proc/')
            try:
                if host_path.exists():
                    content = host_path.read_text()
                    lines = content.strip().split('\n')[1:]  # Skip header
                    udp_stats['total'] += len(lines)
            except Exception:
                pass
        
        return {
            'tcp': tcp_stats,
            'udp': udp_stats,
        }
    
    def get_system_info(self) -> dict:
        """Get general system information with caching for heavy operations"""
        current_time = time.time()
        
        # Return cached result if still valid
        if current_time - self._system_cache_time < self._system_cache_ttl and self._system_cache:
            # Update only lightweight fields
            boot_time = datetime.fromtimestamp(psutil.boot_time())
            uptime_seconds = (datetime.now() - boot_time).total_seconds()
            self._system_cache["uptime_seconds"] = int(uptime_seconds)
            self._system_cache["uptime_human"] = self._format_uptime(uptime_seconds)
            return self._system_cache
        
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        uptime_seconds = (datetime.now() - boot_time).total_seconds()
        
        os_release = self._read_host_file("/etc/os-release")
        os_name = "Unknown"
        for line in os_release.split('\n'):
            if line.startswith('PRETTY_NAME='):
                os_name = line.split('=')[1].strip().strip('"')
                break
        
        try:
            kernel = platform.release()
        except Exception:
            kernel = "Unknown"
        
        try:
            open_files = len(psutil.Process().open_files())
        except Exception:
            open_files = 0
        
        # Get connections from host /proc/net/* (heavy operation)
        conn_stats = self._read_host_connections()
        
        # Legacy format for backward compatibility
        connections = {
            "established": conn_stats['tcp']['established'],
            "listen": conn_stats['tcp']['listen'],
            "time_wait": conn_stats['tcp']['time_wait'],
            "other": conn_stats['tcp']['other'],
        }
        
        result = {
            "hostname": socket.gethostname(),
            "os": os_name,
            "kernel": kernel,
            "architecture": platform.machine(),
            "boot_time": boot_time.isoformat(),
            "uptime_seconds": int(uptime_seconds),
            "uptime_human": self._format_uptime(uptime_seconds),
            "open_files": open_files,
            "connections": connections,
            "connections_detailed": conn_stats,
            "server_name": self.settings.node_name,
            "timezone": self._get_timezone_info()
        }
        
        # Cache the result
        self._system_cache = result
        self._system_cache_time = current_time
        
        return result
    
    def _format_uptime(self, seconds: float) -> str:
        """Format uptime in human readable format"""
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        minutes = int((seconds % 3600) // 60)
        
        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0 or not parts:
            parts.append(f"{minutes}m")
        
        return " ".join(parts)
    
    def _get_timezone_info(self) -> dict:
        """Get server timezone information"""
        now = datetime.now()
        utc_now = datetime.now(timezone.utc)
        
        # Calculate offset in seconds
        local_offset = now.astimezone().utcoffset()
        offset_seconds = int(local_offset.total_seconds()) if local_offset else 0
        offset_hours = offset_seconds / 3600
        
        # Format offset as +03:00 or -05:00
        sign = '+' if offset_hours >= 0 else '-'
        abs_hours = abs(int(offset_hours))
        abs_minutes = abs(int((offset_hours % 1) * 60))
        offset_string = f"{sign}{abs_hours:02d}:{abs_minutes:02d}"
        
        # Try to get timezone name
        tz_name = time.tzname[time.daylight] if time.daylight else time.tzname[0]
        
        # Try reading /etc/timezone for more readable name
        tz_file = self._read_host_file("/etc/timezone").strip()
        if tz_file:
            tz_name = tz_file
        
        return {
            "name": tz_name,
            "offset": offset_string,
            "offset_seconds": offset_seconds
        }
    
    def get_certificates_info(self) -> dict:
        """Get SSL certificate information (closest to expiry)"""
        try:
            from app.services.haproxy_manager import get_haproxy_manager
            manager = get_haproxy_manager()
            certs = manager.get_all_certs_info()
            
            if not certs:
                return {"count": 0, "closest_expiry": None}
            
            # Find closest to expiry (already sorted by days_left)
            closest = certs[0]
            
            return {
                "count": len(certs),
                "closest_expiry": {
                    "domain": closest["domain"],
                    "days_left": closest["days_left"],
                    "expiry_date": closest["expiry_date"],
                    "expired": closest["expired"],
                }
            }
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to get certificates info: {e}")
            return {"count": 0, "closest_expiry": None}
    
    def get_all_metrics(self) -> dict:
        """Collect all metrics at once"""
        tz_info = self._get_timezone_info()
        return {
            "timestamp": datetime.now().isoformat(),
            "server_name": self.settings.node_name,
            "timezone": tz_info,
            "cpu": self.get_cpu_info(),
            "memory": self.get_memory_info(),
            "disk": self.get_disk_info(),
            "network": self.get_network_info(),
            "processes": self.get_processes_info(),
            "system": self.get_system_info(),
            "certificates": self.get_certificates_info()
        }


# Singleton instance
_collector = None


def get_collector() -> MetricsCollector:
    """Get or create metrics collector instance"""
    global _collector
    if _collector is None:
        _collector = MetricsCollector()
    return _collector
