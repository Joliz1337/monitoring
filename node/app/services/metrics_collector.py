"""System metrics collector using psutil.

Мгновенный срез хоста плюс кумулятивные счётчики (их дельты и историю ведёт
панель). Скорости — CPU за последнюю секунду, байт/с по интерфейсам и дискам —
копируются из посекундного `RateSampler`, а не считаются в момент запроса.
"""

import asyncio
import json
import logging
import os
import socket
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil

from app.config import get_settings
from app.services.port_traffic_sampler import get_port_traffic_sampler
from app.services.rate_sampler import RateSample, RateSampler, get_rate_sampler, read_net_dev

logger = logging.getLogger(__name__)

NO_RATE = (0.0, 0.0)


class MetricsCollector:
    """Collects current system metrics from host."""

    def __init__(self, rate_sampler: Optional[RateSampler] = None):
        self.settings = get_settings()
        self._rate_sampler = rate_sampler or get_rate_sampler()
        # Process cache to avoid blocking
        self._processes_cache: list = []
        self._processes_cache_time: float = 0
        self._processes_cache_ttl: float = 5.0  # 5 seconds
        # System info cache (connections parsing is heavy)
        self._system_cache: dict = {}
        self._system_cache_time: float = 0
        self._system_cache_ttl: float = 5.0  # 5 seconds
        # boot_id не меняется в пределах жизни хоста — читаем лениво один раз
        self._boot_id: Optional[str] = None
        # Роутер системы уже читает /app/VERSION, а NODE_VERSION из app.main
        # импортировать нельзя — main сам импортирует этот модуль
        from app.routers.system import get_current_version
        self._agent_version: str = get_current_version()
        # Права в пределах процесса не меняются — собирать карту заново на
        # каждый опрос (а он раз в 10 секунд) незачем
        from app.capabilities import get_policy
        self._capabilities: Optional[dict] = get_policy().published()

    def _read_host_file(self, path: str) -> str:
        """Read file from host filesystem"""
        host_path = Path(self.settings.host_proc).parent / path.lstrip('/')
        if host_path.exists():
            return host_path.read_text(encoding='utf-8', errors='replace')
        fallback = Path(path)
        if fallback.exists():
            return fallback.read_text(encoding='utf-8', errors='replace')
        return ""

    @staticmethod
    def live_rates(rates: Optional[RateSample]) -> Optional[dict]:
        """Маркер для панели: скорости в ответе — реальные, за это окно.
        Без свежего замера маркера нет, и панель считает дельты сама."""
        if rates is None:
            return None
        return {"window_sec": round(rates.window_sec, 3), "sampled_at": rates.sampled_at}

    def get_cpu_info(self, rates: Optional[RateSample]) -> dict:
        """Get CPU information and usage"""
        cpu_count_logical = psutil.cpu_count(logical=True) or 1
        cpu_count_physical = psutil.cpu_count(logical=False) or 1

        per_cpu = list(rates.per_cpu_percent) if rates else []

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
        # Широкий перехват здесь осознанный: температуры — необязательная косметика,
        # а бэкенды psutil для hwmon/ipmi на экзотическом железе бросают что угодно.
        # Любое исключение отсюда поднялось бы в /api/metrics, а это единственный
        # эндпоинт, по которому панель определяет живость ноды — сервер ушёл бы в offline.
        except Exception as e:
            logger.debug(f"Temperature sensors unavailable: {e}")
        
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
    
    def get_disk_info(self, rates: Optional[RateSample]) -> dict:
        """Get disk partitions, usage, cumulative I/O counters and per-second rates"""
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
        
        disk_rates = rates.disk if rates else {}
        total_read, total_write = rates.disk_total if rates else NO_RATE

        io_counters = psutil.disk_io_counters(perdisk=True)
        io_stats = {}
        for disk, counters in (io_counters or {}).items():
            read_rate, write_rate = disk_rates.get(disk, NO_RATE)
            io_stats[disk] = {
                "read_bytes": counters.read_bytes,
                "write_bytes": counters.write_bytes,
                "read_count": counters.read_count,
                "write_count": counters.write_count,
                "read_time_ms": counters.read_time,
                "write_time_ms": counters.write_time,
                "read_bytes_per_sec": read_rate,
                "write_bytes_per_sec": write_rate,
            }

        return {
            "partitions": partitions,
            "io": io_stats,
            # Только целые диски: раздел sda1 уже внутри счётчика sda
            "io_total": {
                "read_bytes_per_sec": total_read,
                "write_bytes_per_sec": total_write,
            },
        }

    # Virtual/bridge/tunnel interfaces whose traffic is already counted on physical interfaces
    VIRTUAL_IFACE_PREFIXES = (
        'veth', 'docker', 'br-', 'virbr', 'flannel', 'cni', 'cali',
        'wg', 'tun', 'tap', 'warp', 'gre', 'sit', 'ip6tnl',
    )

    def _is_virtual_interface(self, name: str) -> bool:
        return name.startswith(self.VIRTUAL_IFACE_PREFIXES)

    @staticmethod
    def _get_bond_slaves() -> set[str]:
        """Read slave interfaces from all bond masters via /sys/class/net/*/bonding/slaves.
        Returns set of slave interface names to exclude from total traffic calculation
        (bond master already accounts for all slave traffic).
        """
        slaves = set()
        try:
            for bond_dir in Path("/sys/class/net").iterdir():
                slaves_file = bond_dir / "bonding" / "slaves"
                if slaves_file.exists():
                    content = slaves_file.read_text().strip()
                    if content:
                        slaves.update(content.split())
        except OSError as e:
            logger.debug(f"Failed to enumerate bond slaves, totals may double-count: {e}")
        return slaves

    def get_network_info(self, rates: Optional[RateSample]) -> dict:
        """Get network interfaces: cumulative counters plus one-second rates"""
        interfaces = []

        host_net_stats = read_net_dev()

        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()

        # Bond slaves duplicate traffic already counted on bond master
        bond_slaves = self._get_bond_slaves()

        net_rates = rates.net if rates else {}

        for iface, io in host_net_stats.items():
            is_virtual = self._is_virtual_interface(iface) or iface in bond_slaves
            rx_rate, tx_rate = net_rates.get(iface, NO_RATE)
            iface_info = {
                "name": iface,
                "addresses": [],
                "mac": None,
                "mtu": None,
                "speed_mbps": None,
                "is_up": True,
                "is_virtual": is_virtual,
                "rx_bytes": io['rx_bytes'],
                "tx_bytes": io['tx_bytes'],
                "rx_packets": io['rx_packets'],
                "tx_packets": io['tx_packets'],
                "rx_errors": io['rx_errors'],
                "tx_errors": io['tx_errors'],
                "rx_drops": io['rx_drops'],
                "tx_drops": io['tx_drops'],
                "rx_bytes_per_sec": rx_rate,
                "tx_bytes_per_sec": tx_rate,
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

        # Total traffic — only physical interfaces, excluding bond slaves to avoid double-counting
        # (bond master already includes all slave traffic; veth/docker/br-* mirror physical)
        physical = [
            iface for iface in host_net_stats
            if not self._is_virtual_interface(iface) and iface not in bond_slaves
        ]
        total = {
            "rx_bytes": sum(host_net_stats[i]['rx_bytes'] for i in physical),
            "tx_bytes": sum(host_net_stats[i]['tx_bytes'] for i in physical),
            "rx_packets": sum(host_net_stats[i]['rx_packets'] for i in physical),
            "tx_packets": sum(host_net_stats[i]['tx_packets'] for i in physical),
            "rx_bytes_per_sec": sum(net_rates.get(i, NO_RATE)[0] for i in physical),
            "tx_bytes_per_sec": sum(net_rates.get(i, NO_RATE)[1] for i in physical),
        }

        ports, ports_available, ports_sampled_at = self._port_counters()

        return {
            "interfaces": interfaces,
            "total": total,
            "ports": ports,
            "ports_available": ports_available,
            "ports_sampled_at": ports_sampled_at,
        }

    @staticmethod
    def _port_counters() -> tuple[list[dict], bool, Optional[float]]:
        """Снимок счётчиков по портам — уже в памяти, его наполняет фоновый семплер.

        Перехват широкий по той же причине, что и у температур: /api/metrics —
        единственный признак живости ноды, и учёт по портам не стоит того,
        чтобы из-за него сервер уходил в offline.
        """
        try:
            return get_port_traffic_sampler().snapshot()
        except Exception as e:
            logger.error(f"Port traffic snapshot unavailable: {e}")
            return [], False, None

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
            host_path = Path(self.settings.host_proc) / tcp_file.removeprefix('/proc/')
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
            except OSError as e:
                logger.warning(f"Failed to read {host_path}, TCP connection counters are incomplete: {e}")

        # Read UDP (IPv4 + IPv6)
        for udp_file in ['/proc/net/udp', '/proc/net/udp6']:
            host_path = Path(self.settings.host_proc) / udp_file.removeprefix('/proc/')
            try:
                if host_path.exists():
                    content = host_path.read_text()
                    lines = content.strip().split('\n')[1:]  # Skip header
                    udp_stats['total'] += len(lines)
            except OSError as e:
                logger.warning(f"Failed to read {host_path}, UDP connection counters are incomplete: {e}")

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
            "timezone": self._get_timezone_info(),
            "boot_id": self._get_boot_id(),
        }
        
        # Cache the result
        self._system_cache = result
        self._system_cache_time = current_time
        
        return result
    
    def _get_boot_id(self) -> Optional[str]:
        """Идентификатор загрузки ядра — по нему панель отличает ребут от сбоя счётчика."""
        # Пустой результат не кэшируем: одна осечка на старте контейнера иначе навсегда
        # лишила бы панель точного признака ребута и оставила только эвристику по аптайму.
        if not self._boot_id:
            self._boot_id = self._read_host_file("/proc/sys/kernel/random/boot_id").strip()
        return self._boot_id or None

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
            logger.error(f"Failed to get certificates info: {e}")
            return {"count": 0, "closest_expiry": None}
    
    def _read_proc_int(self, path: str, default=None):
        """Read a single integer out of /proc, preferring the host mount."""
        for base in ("/host", ""):
            try:
                with open(f"{base}{path}", "r") as fh:
                    return int(fh.read().strip())
            except (OSError, ValueError):
                continue
        return default

    def _read_hex_column_sum(self, path: str, column: int):
        """Sum one hex column across all rows (/proc/net/softnet_stat)."""
        for base in ("/host", ""):
            try:
                total = 0
                with open(f"{base}{path}", "r") as fh:
                    for line in fh:
                        parts = line.split()
                        if len(parts) > column:
                            total += int(parts[column], 16)
                return total
            except (OSError, ValueError):
                continue
        return None

    def _read_netstat_counters(self, names) -> dict:
        """Pull named TcpExt counters out of /proc/net/netstat."""
        out = {}
        for base in ("/host", ""):
            try:
                header = None
                with open(f"{base}/proc/net/netstat", "r") as fh:
                    for line in fh:
                        if not line.startswith("TcpExt:"):
                            continue
                        fields = line.split()
                        if header is None:
                            header = fields
                            continue
                        for name in names:
                            if name in header:
                                idx = header.index(name)
                                if idx < len(fields):
                                    out[name] = int(fields[idx])
                        break
                if out:
                    return out
            except (OSError, ValueError):
                continue
        return out

    def get_antiddos_info(self) -> dict:
        """Anti-DDoS watchdog state plus the counters it decides on.

        /proc is read straight from the container's /host mount — no nsenter, so
        this stays cheap enough to run on every metrics poll.
        """
        info = {"mode": "off", "source": "none", "since": 0, "watchdog": "off"}

        try:
            with open("/opt/monitoring/antiddos/state.json", "r") as fh:
                state = json.load(fh)
            for key in ("mode", "source", "watchdog"):
                if state.get(key):
                    info[key] = str(state[key])
            info["since"] = int(state.get("since") or 0)
        except (OSError, ValueError, TypeError):
            # No state file: the watchdog is not installed on this node.
            pass

        ct_count = self._read_proc_int("/proc/sys/net/netfilter/nf_conntrack_count")
        ct_max = self._read_proc_int("/proc/sys/net/netfilter/nf_conntrack_max")
        info["conntrack_count"] = ct_count
        info["conntrack_max"] = ct_max
        if ct_count is not None and ct_max:
            info["conntrack_fill_pct"] = round(ct_count * 100.0 / ct_max, 1)

        info["softnet_dropped_total"] = self._read_hex_column_sum("/proc/net/softnet_stat", 1)

        counters = self._read_netstat_counters(
            ["SyncookiesSent", "ListenOverflows", "ListenDrops"]
        )
        if "SyncookiesSent" in counters:
            info["syncookies_sent_total"] = counters["SyncookiesSent"]
        # Разведены намеренно: ListenDrops растёт при штатной смене слушающих
        # сокетов, а полную очередь accept означает только ListenOverflows.
        # Суммирование этих двух в один показатель приводило к ложным
        # срабатываниям вотчдога.
        if "ListenOverflows" in counters:
            info["listen_overflows_total"] = counters["ListenOverflows"]
        if "ListenDrops" in counters:
            info["listen_drops_total"] = counters["ListenDrops"]

        # insert_failed is the "table full, dropping packets" counter; it is
        # per-CPU hex in a named-column file.
        for base in ("/host", ""):
            try:
                with open(f"{base}/proc/net/stat/nf_conntrack", "r") as fh:
                    lines = fh.read().splitlines()
                if len(lines) < 2:
                    break
                header = lines[0].split()
                if "insert_failed" not in header:
                    break
                idx = header.index("insert_failed")
                total = 0
                for row in lines[1:]:
                    parts = row.split()
                    if len(parts) > idx:
                        total += int(parts[idx], 16)
                info["insert_failed_total"] = total
                break
            except (OSError, ValueError):
                continue

        return info

    async def get_all_metrics(self) -> dict:
        """Collect all metrics in parallel using thread pool"""
        tz_info = self._get_timezone_info()
        # Один сэмпл на весь ответ: маркер live_rates и сами скорости — из одного окна
        rates = self._rate_sampler.snapshot()
        cpu, memory, disk, network, processes, system, certs, antiddos = await asyncio.gather(
            asyncio.to_thread(self.get_cpu_info, rates),
            asyncio.to_thread(self.get_memory_info),
            asyncio.to_thread(self.get_disk_info, rates),
            asyncio.to_thread(self.get_network_info, rates),
            asyncio.to_thread(self.get_processes_info),
            asyncio.to_thread(self.get_system_info),
            asyncio.to_thread(self.get_certificates_info),
            asyncio.to_thread(self.get_antiddos_info),
        )
        return {
            "timestamp": datetime.now().isoformat(),
            "server_name": self.settings.node_name,
            "timezone": tz_info,
            "cpu": cpu,
            "memory": memory,
            "disk": disk,
            "network": network,
            "processes": processes,
            "system": system,
            "certificates": certs,
            "antiddos": antiddos,
            "live_rates": self.live_rates(rates),
            "agent_version": self._agent_version,
            "capabilities": self._capabilities,
        }


# Singleton instance
_collector = None


def get_collector() -> MetricsCollector:
    """Get or create metrics collector instance"""
    global _collector
    if _collector is None:
        _collector = MetricsCollector()
    return _collector
