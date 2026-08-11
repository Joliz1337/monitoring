"""HAProxy configuration manager for native systemd HAProxy service"""

import asyncio
import ipaddress
import logging
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

import psutil

from app.config import get_settings
from app.services import cpu_affinity
from app.services.host_executor import get_host_executor

logger = logging.getLogger(__name__)

RULES_START_MARKER = "# === RULES START ==="
RULES_END_MARKER = "# === RULES END ==="

MAXCONN_PER_RAM_MB = 10
MAXCONN_MIN = 10000
MAXCONN_MAX = 500000

OPENSSL_TIMEOUT_SEC = 10
CERTBOT_ISSUE_TIMEOUT_SEC = 120
CERTBOT_RENEW_TIMEOUT_SEC = 300

# Срок сертификата меняется раз в месяцы, а get_cert_info вызывается на каждом
# опросе метрик по каждому домену — без кэша это форк openssl на домен раз в
# несколько секунд. Сбрасывается при выпуске/продлении/загрузке/удалении.
CERT_INFO_CACHE_TTL_SEC = 3600.0

# Домен подставляется в путь /etc/letsencrypt/live/<domain>/: пустые метки
# запрещены, поэтому «..» и ведущая точка не пройдут — иначе это запись за
# пределы каталога сертификатов. Конец строки — \Z, а не $: перед завершающим
# переводом строки $ совпадает, и «example.com\n» прошёл бы проверку.
_DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
CERT_DOMAIN_RE = re.compile(rf"^{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})*\Z")


@dataclass
class BackendServer:
    name: str
    address: str
    port: int
    weight: int = 1
    maxconn: Optional[int] = None
    check: bool = True
    inter: str = "5s"
    fall: int = 3
    rise: int = 2
    send_proxy: bool = False
    send_proxy_v2: bool = False
    backup: bool = False
    slowstart: Optional[str] = None
    on_marked_down: Optional[str] = None
    on_marked_up: Optional[str] = None
    disabled: bool = False


@dataclass
class BalancerOptions:
    algorithm: str = "roundrobin"
    algorithm_param: Optional[str] = None
    hash_type: Optional[str] = None
    health_check_type: Optional[str] = None
    httpchk_method: Optional[str] = None
    httpchk_uri: Optional[str] = None
    httpchk_expect: Optional[str] = None
    sticky_type: Optional[str] = None
    cookie_name: Optional[str] = None
    cookie_options: Optional[str] = None
    stick_table_type: Optional[str] = None
    stick_table_size: Optional[str] = None
    stick_table_expire: Optional[str] = None
    retries: int = 3
    redispatch: bool = True
    allbackups: bool = False
    fullconn: Optional[int] = None
    timeout_queue: Optional[str] = None


@dataclass
class HAProxyRule:
    """HAProxy routing rule"""
    name: str
    rule_type: str  # tcp or https
    listen_port: int
    target_ip: str
    target_port: int
    cert_domain: Optional[str] = None
    target_ssl: bool = False
    send_proxy: bool = False
    accept_proxy: bool = False
    use_wildcard: bool = False
    is_balancer: bool = False
    servers: list[BackendServer] = field(default_factory=list)
    balancer_options: Optional[BalancerOptions] = None


@dataclass
class CertbotResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class HAProxyManager:
    """Manages HAProxy configuration via native systemd service"""
    
    def __init__(self):
        self.settings = get_settings()
        self.config_path = self.settings.haproxy_config
        self.certs_dir = self.settings.haproxy_certs
        self._executor = get_host_executor()
        # Status cache to reduce systemctl calls
        self._status_cache: Optional[dict] = None
        self._status_cache_time: float = 0
        self._status_cache_ttl: float = 5.0  # 5 seconds
        self._cert_info_cache: dict[str, tuple[float, dict]] = {}

    @staticmethod
    def _is_domain(target: str) -> bool:
        try:
            ipaddress.ip_address(target)
            return False
        except ValueError:
            return True
    
    @classmethod
    def _patch_dns_resolvers(cls, content: str) -> str:
        """Add/update resolver params on server lines with domain targets"""
        resolver_suffix = " resolvers mydns resolve-prefer ipv4 init-addr none"
        
        def patch_server_line(match: re.Match) -> str:
            line = match.group(0)
            server_match = re.search(r'server\s+\S+\s+(\S+):\d+', line)
            if not server_match:
                return line
            target = server_match.group(1)
            if not cls._is_domain(target):
                return line
            if "resolvers " in line:
                return line
            # Insert resolver params right after host:port (before other options like send-proxy, check, ssl)
            host_port_end = server_match.end()
            return line[:host_port_end] + resolver_suffix + line[host_port_end:]
        
        return re.sub(r'^    server .+$', patch_server_line, content, flags=re.MULTILINE)
    
    def _read_config(self) -> str:
        """Read HAProxy config file"""
        if self.config_path.exists():
            return self.config_path.read_text(encoding='utf-8', errors='replace')
        return ""
    
    def _write_config(self, content: str):
        """Write HAProxy config file"""
        content = content.encode('utf-8', errors='replace').decode('utf-8')
        if not content.endswith('\n'):
            content += '\n'
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(content, encoding='utf-8')
    
    @contextmanager
    def _config_rollback(self) -> Iterator[Callable[[], None]]:
        """Снимок конфига на время правки; отдаёт функцию отката и удаляет снимок
        на выходе.

        Имя снимка уникально: с общим haproxy.cfg.bak откат одной операции
        подхватывал снимок другой, шедшей параллельно, и возвращал конфиг к
        чужому состоянию."""
        backup_path: Optional[Path] = None
        if self.config_path.exists():
            backup_path = self.config_path.with_name(f"{self.config_path.name}.{uuid4().hex}.bak")
            shutil.copy(self.config_path, backup_path)

        def rollback() -> None:
            if backup_path and backup_path.exists():
                shutil.copy(backup_path, self.config_path)

        try:
            yield rollback
        finally:
            if backup_path:
                backup_path.unlink(missing_ok=True)

    def _read_nofile_limit(self) -> int:
        """HAProxy's real RLIMIT_NOFILE ceiling, from the tuning facts file.

        NOT /proc/sys/fs/nr_open, which is what this used to read: nr_open is the
        ceiling on what a process *may* set, not what haproxy.service actually
        gets. HAProxy is a native systemd unit whose limit comes from
        DefaultLimitNOFILE (and now from the LimitNOFILE drop-in the renderer
        writes). With nr_open=2097152 and a unit limit of 65536, the old formula
        returned up to 500000 and HAProxy — which runs with strict-limits by
        default since 2.5 — simply refused to start.
        """
        for path in (
            Path("/opt/monitoring/configs/tuning-facts.env"),
            Path("/host/opt/monitoring/configs/tuning-facts.env"),
        ):
            try:
                for line in path.read_text().splitlines():
                    if line.startswith("NOFILE_LIMIT="):
                        value = int(line.split("=", 1)[1].strip())
                        if value > 0:
                            return value
            except (OSError, ValueError):
                continue

        # No facts file (optimizations never applied): fall back to the value the
        # process itself is allowed, which is at least honest about the ceiling.
        try:
            return int(Path("/proc/sys/fs/nr_open").read_text().strip())
        except (OSError, ValueError):
            return 1048576

    def _compute_maxconn(self) -> int:
        """Потолок соединений от RAM хоста: ~40 КБ на соединение в худшем случае
        (2 буфера по 16 КБ + структуры сессии), 10 соединений на МБ RAM — HAProxy
        займёт не больше ~40% памяти. Дополнительно ограничен реальным
        RLIMIT_NOFILE процесса (~3 fd на соединение с запасом): при maxconn выше
        fd-лимита strict-limits не даст HAProxy стартовать."""
        ram_mb = psutil.virtual_memory().total // (1024 * 1024)
        nofile = self._read_nofile_limit()
        fd_based = (nofile - 1024) // 3
        return max(MAXCONN_MIN, min(ram_mb * MAXCONN_PER_RAM_MB, fd_based, MAXCONN_MAX))

    def _ensure_global_maxconn(self, content: str) -> str:
        """Вставляет рассчитанный maxconn в секцию global, если он не задан явно.

        Конфиг из панельного профиля один на много серверов с разной RAM,
        поэтому потолок соединений вычисляется на ноде при применении."""
        global_match = re.search(r'^global[ \t]*\n((?:[ \t]+\S.*\n?)*)', content, re.MULTILINE)
        if not global_match:
            return content
        if re.search(r'^[ \t]+maxconn\b', global_match.group(1), re.MULTILINE):
            return content
        insert_pos = global_match.start(1)
        return content[:insert_pos] + f"    maxconn {self._compute_maxconn()}\n" + content[insert_pos:]

    def _generate_base_config(self) -> str:
        """Generate base HAProxy config for high-speed TCP relay"""
        return f"""global
    stats socket /var/run/haproxy.sock mode 660 level admin expose-fd listeners
    no log
    maxconn {self._compute_maxconn()}
    tune.bufsize 16384
    tune.maxpollevents 1024
    tune.recv_enough 16384
    hard-stop-after 1h
    # cpu-affinity (auto)

defaults
    mode tcp
    timeout connect 5s
    timeout client 30m
    timeout server 30m
    timeout tunnel 1h
    timeout client-fin 5s
    timeout server-fin 5s
    option dontlognull
    option redispatch
    option tcp-smart-accept
    option tcp-smart-connect
    option splice-auto
    option clitcpka
    clitcpka-idle 60s
    clitcpka-intvl 10s
    clitcpka-cnt 3
    option srvtcpka
    srvtcpka-idle 60s
    srvtcpka-intvl 10s
    srvtcpka-cnt 3

resolvers mydns
    nameserver dns1 1.1.1.1:53
    nameserver dns2 8.8.8.8:53
    resolve_retries 3
    timeout resolve 1s
    timeout retry 1s
    hold valid 60s
    hold nx 10s
    hold other 10s

{RULES_START_MARKER}
{RULES_END_MARKER}
"""
    
    def init_config(self) -> tuple[bool, str]:
        """Initialize base HAProxy config if not exists"""
        if not self.config_path.exists():
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_config(self._generate_base_config())
            logger.info("HAProxy config initialized")
            return True, "Config initialized"
        return True, "Config already exists"
    
    def full_init(self) -> tuple[bool, str]:
        """Full initialization: create HAProxy config if needed"""
        if self.config_path.exists():
            return True, "Config already exists"
        return self.init_config()
    
    def check_config(self) -> tuple[bool, str]:
        """Validate HAProxy configuration using haproxy -c"""
        if not self.config_path.exists():
            return False, "Config file not found"
        
        # Use host executor to run haproxy check on host
        result = self._executor.execute_sync(
            f"haproxy -c -f {self.config_path}",
            timeout=30
        )
        
        if result.success:
            return True, "Configuration valid"
        
        # Parse error message
        error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
        if not error_msg:
            error_msg = "Configuration check failed"
        
        return False, error_msg
    
    def is_running(self) -> bool:
        """Check if HAProxy service is running"""
        result = self._executor.execute_sync(
            "systemctl is-active haproxy",
            timeout=10
        )
        return result.success and result.stdout.strip() == "active"
    
    def is_installed(self) -> bool:
        """Check if HAProxy is installed on the system"""
        result = self._executor.execute_sync(
            "command -v haproxy",
            timeout=10
        )
        return result.success and len(result.stdout.strip()) > 0
    
    def is_enabled(self) -> bool:
        """Check if HAProxy service is enabled for autostart"""
        result = self._executor.execute_sync(
            "systemctl is-enabled haproxy",
            timeout=10
        )
        return result.success and result.stdout.strip() == "enabled"
    
    def get_status(self) -> dict:
        """Get HAProxy service status with caching"""
        current_time = time.time()
        
        # Return cached status if still valid
        if current_time - self._status_cache_time < self._status_cache_ttl and self._status_cache:
            return self._status_cache
        
        is_installed = self.is_installed()
        is_running = self.is_running() if is_installed else False
        is_enabled = self.is_enabled() if is_installed else False
        is_valid, config_msg = self.check_config() if is_installed else (False, "HAProxy not installed")
        config_exists = self.config_path.exists()
        
        status_output = ""
        service_logs = ""
        
        if is_installed:
            # Get systemctl status
            status_result = self._executor.execute_sync(
                "systemctl status haproxy --no-pager -l",
                timeout=10
            )
            status_output = status_result.stdout if status_result.stdout else status_result.stderr
            
            # Get recent logs if not running
            if not is_running:
                logs_result = self._executor.execute_sync(
                    "journalctl -u haproxy -n 20 --no-pager",
                    timeout=10
                )
                service_logs = logs_result.stdout if logs_result.success else ""
        else:
            status_output = "HAProxy is not installed. Install with: apt install haproxy"
        
        result = {
            "running": is_running,
            "enabled": is_enabled,  # autostart on boot
            "installed": is_installed,
            "config_valid": is_valid,
            "config_exists": config_exists,
            "config_message": config_msg,
            "config_path": str(self.config_path),
            "status_output": status_output,
            "service_logs": service_logs
        }
        
        # Cache the result
        self._status_cache = result
        self._status_cache_time = current_time
        
        return result
    
    def get_logs(self, tail: int = 100) -> str:
        """Get HAProxy service logs via journalctl"""
        result = self._executor.execute_sync(
            f"journalctl -u haproxy -n {tail} --no-pager",
            timeout=30
        )
        
        if result.success:
            return result.stdout
        return f"Failed to get logs: {result.stderr or result.error}"
    
    def reload(self, auto_start: bool = True) -> tuple[bool, str]:
        """Reload HAProxy configuration via systemctl
        
        Args:
            auto_start: If True, start service if stopped. If False, skip reload silently.
        
        Returns: (success, message)
        """
        is_valid, config_error = self.check_config()
        if not is_valid:
            return False, f"Config error: {config_error}"
        
        if not self.is_installed():
            return False, "HAProxy is not installed"
        
        if not self.is_running():
            if auto_start:
                # Start service if not running
                return self.start_haproxy()
            # Service not running - config saved, no reload needed
            return True, "Config saved (HAProxy not running)"
        
        # Reload running service
        result = self._executor.execute_sync(
            "systemctl reload haproxy",
            timeout=30
        )
        
        # Invalidate status cache
        self._status_cache = None
        
        if result.success:
            logger.info("HAProxy reloaded via systemctl")
            return True, "HAProxy reloaded successfully"
        
        error_msg = result.stderr or result.stdout or "Reload failed"
        logger.error(f"HAProxy reload failed: {error_msg}")
        return False, f"Reload failed: {error_msg}"
    
    def restart(self) -> tuple[bool, str]:
        """Restart HAProxy service via systemctl"""
        if not self.is_installed():
            return False, "HAProxy is not installed"
        
        result = self._executor.execute_sync(
            "systemctl restart haproxy",
            timeout=30
        )
        
        # Invalidate status cache
        self._status_cache = None
        
        if result.success:
            logger.info("HAProxy restarted via systemctl")
            return True, "HAProxy restarted successfully"
        
        error_msg = result.stderr or result.stdout or "Restart failed"
        logger.error(f"HAProxy restart failed: {error_msg}")
        return False, f"Restart failed: {error_msg}"
    
    def start_haproxy(self) -> tuple[bool, str]:
        """Start HAProxy service via systemctl and enable autostart"""
        if not self.is_installed():
            return False, "HAProxy is not installed. Install with: apt install haproxy"
        
        if self.is_running():
            # Ensure autostart is enabled even if already running
            self._executor.execute_sync("systemctl enable haproxy", timeout=10)
            return True, "HAProxy is already running"
        
        # Check config before starting
        if not self.config_path.exists():
            return False, f"HAProxy config not found at {self.config_path}. Create config first."
        
        is_valid, config_error = self.check_config()
        if not is_valid:
            return False, f"Config validation failed: {config_error}"
        
        result = self._executor.execute_sync(
            "systemctl start haproxy",
            timeout=30
        )
        
        # Invalidate status cache
        self._status_cache = None
        
        if result.success:
            # Verify it actually started
            time.sleep(0.5)
            if self.is_running():
                # Enable autostart on boot
                enable_result = self._executor.execute_sync(
                    "systemctl enable haproxy",
                    timeout=10
                )
                if enable_result.success:
                    logger.info("HAProxy started and enabled for autostart")
                    return True, "HAProxy started successfully (autostart enabled)"
                else:
                    logger.warning(f"HAProxy started but failed to enable autostart: {enable_result.stderr}")
                    return True, "HAProxy started (warning: autostart not enabled)"
            else:
                # Get logs to understand why it failed
                logs = self.get_logs(tail=20)
                logger.error(f"HAProxy failed to start. Logs: {logs}")
                return False, "HAProxy failed to start. Check logs for details."
        
        error_msg = result.stderr or result.stdout or "Start failed"
        logger.error(f"HAProxy start failed: {error_msg}")
        return False, f"Failed to start: {error_msg}"
    
    def stop_haproxy(self) -> tuple[bool, str]:
        """Stop HAProxy service via systemctl and disable autostart"""
        if not self.is_installed():
            return True, "HAProxy is not installed"
        
        if not self.is_running():
            self._executor.execute_sync("systemctl disable haproxy", timeout=10)
            return True, "HAProxy is already stopped"
        
        result = self._executor.execute_sync(
            "systemctl stop haproxy",
            timeout=30
        )
        
        self._status_cache = None
        
        if result.success:
            disable_result = self._executor.execute_sync(
                "systemctl disable haproxy",
                timeout=10
            )
            if disable_result.success:
                logger.info("HAProxy stopped and disabled autostart")
                return True, "HAProxy stopped successfully (autostart disabled)"
            else:
                logger.warning(f"HAProxy stopped but failed to disable autostart: {disable_result.stderr}")
                return True, "HAProxy stopped (warning: autostart still enabled)"
        
        error_msg = result.stderr or result.stdout or "Stop failed"
        logger.error(f"HAProxy stop failed: {error_msg}")
        return False, f"Failed to stop: {error_msg}"
    
    def _temporary_stop(self) -> tuple[bool, str]:
        """Stop HAProxy without changing autostart state (for cert operations)"""
        if not self.is_installed():
            return True, "HAProxy is not installed"
        
        if not self.is_running():
            return True, "HAProxy is already stopped"
        
        result = self._executor.execute_sync(
            "systemctl stop haproxy",
            timeout=30
        )
        
        self._status_cache = None
        
        if result.success:
            logger.info("HAProxy temporarily stopped (autostart unchanged)")
            return True, "HAProxy stopped temporarily"
        
        error_msg = result.stderr or result.stdout or "Stop failed"
        logger.error(f"HAProxy temporary stop failed: {error_msg}")
        return False, f"Failed to stop: {error_msg}"
    
    def _temporary_start(self) -> tuple[bool, str]:
        """Start HAProxy without changing autostart state (for cert operations)"""
        if not self.is_installed():
            return False, "HAProxy is not installed"
        
        if self.is_running():
            return True, "HAProxy is already running"
        
        if not self.config_path.exists():
            return False, f"HAProxy config not found at {self.config_path}"
        
        is_valid, config_error = self.check_config()
        if not is_valid:
            return False, f"Config validation failed: {config_error}"
        
        result = self._executor.execute_sync(
            "systemctl start haproxy",
            timeout=30
        )
        
        self._status_cache = None
        
        if result.success:
            time.sleep(0.5)
            if self.is_running():
                logger.info("HAProxy temporarily started (autostart unchanged)")
                return True, "HAProxy started"
            else:
                return False, "HAProxy failed to start"
        
        error_msg = result.stderr or result.stdout or "Start failed"
        logger.error(f"HAProxy temporary start failed: {error_msg}")
        return False, f"Failed to start: {error_msg}"
    
    @staticmethod
    def _parse_server_opt(opts: str, name: str, cast=str, default=None):
        m = re.search(rf'{name}\s+(\S+)', opts)
        return cast(m.group(1)) if m else default

    def _parse_server_line(self, line: str) -> BackendServer | None:
        m = re.match(r'\s*server\s+(\S+)\s+(\S+):(\d+)(.*)', line)
        if not m:
            return None
        srv_name, addr, port, opts = m.groups()
        return BackendServer(
            name=srv_name, address=addr, port=int(port),
            weight=self._parse_server_opt(opts, "weight", int, 1),
            maxconn=self._parse_server_opt(opts, "maxconn", int, None),
            check="check" in opts.split(),
            inter=self._parse_server_opt(opts, "inter", str, "5s"),
            fall=self._parse_server_opt(opts, "fall", int, 3),
            rise=self._parse_server_opt(opts, "rise", int, 2),
            send_proxy="send-proxy-v2" not in opts and "send-proxy" in opts,
            send_proxy_v2="send-proxy-v2" in opts,
            backup="backup" in opts.split(),
            slowstart=self._parse_server_opt(opts, "slowstart", str, None),
            on_marked_down=self._parse_server_opt(opts, "on-marked-down", str, None),
            on_marked_up=self._parse_server_opt(opts, "on-marked-up", str, None),
            disabled="disabled" in opts.split(),
        )

    def _parse_balancer_options(self, block: str) -> BalancerOptions:
        opts = BalancerOptions()
        bal_m = re.search(r'balance\s+(\S+?)(?:\((\S+?)\))?(?:\s|$)', block)
        if bal_m:
            opts.algorithm = bal_m.group(1)
            opts.algorithm_param = bal_m.group(2)
        ht_m = re.search(r'hash-type\s+(\S+)', block)
        if ht_m:
            opts.hash_type = ht_m.group(1)
        if re.search(r'option\s+tcp-check', block):
            opts.health_check_type = "tcp-check"
        hc_m = re.search(r'option\s+httpchk\s+(\S+)\s+(\S+)', block)
        if hc_m:
            opts.health_check_type = "httpchk"
            opts.httpchk_method = hc_m.group(1)
            opts.httpchk_uri = hc_m.group(2)
        exp_m = re.search(r'http-check\s+expect\s+(.+)', block)
        if exp_m:
            opts.httpchk_expect = exp_m.group(1).strip()
        cookie_m = re.search(r'^\s+cookie\s+(\S+)\s+(.+)', block, re.MULTILINE)
        if cookie_m:
            opts.sticky_type = "cookie"
            opts.cookie_name = cookie_m.group(1)
            opts.cookie_options = cookie_m.group(2).strip()
        st_m = re.search(r'stick-table\s+type\s+(\S+)\s+size\s+(\S+)\s+expire\s+(\S+)', block)
        if st_m:
            opts.sticky_type = "stick-table"
            opts.stick_table_type = st_m.group(1)
            opts.stick_table_size = st_m.group(2)
            opts.stick_table_expire = st_m.group(3)
        opts.redispatch = bool(re.search(r'option\s+redispatch', block))
        opts.allbackups = bool(re.search(r'option\s+allbackups', block))
        ret_m = re.search(r'retries\s+(\d+)', block)
        if ret_m:
            opts.retries = int(ret_m.group(1))
        fc_m = re.search(r'fullconn\s+(\d+)', block)
        if fc_m:
            opts.fullconn = int(fc_m.group(1))
        tq_m = re.search(r'timeout\s+queue\s+(\S+)', block)
        if tq_m:
            opts.timeout_queue = tq_m.group(1)
        return opts

    def parse_rules(self) -> list[HAProxyRule]:
        """Parse rules from config"""
        content = self._read_config()
        if not content:
            return []

        rules = []

        frontend_pattern = re.compile(
            r'^frontend\s+(tcp|https)_(\S+)\s*\n(.*?)(?=^frontend|^backend|\Z)',
            re.MULTILINE | re.DOTALL
        )
        backend_pattern = re.compile(
            r'^backend\s+backend_(tcp|https)_(\S+)\s*\n(.*?)(?=^frontend|^backend|\Z)',
            re.MULTILINE | re.DOTALL
        )

        frontends = {}
        for match in frontend_pattern.finditer(content):
            rule_type, name, block = match.groups()
            port_match = re.search(r'bind\s+\*:(\d+)', block)
            cert_match = re.search(r'ssl\s+crt\s+/etc/letsencrypt/live/([^/]+)/combined\.pem', block) if rule_type == "https" else None
            bind_line_match = re.search(r'^\s*bind\s+.+$', block, re.MULTILINE)
            accept_proxy = bool(bind_line_match and 'accept-proxy' in bind_line_match.group(0))

            frontends[name] = {
                "type": rule_type,
                "port": int(port_match.group(1)) if port_match else 0,
                "cert_domain": cert_match.group(1) if cert_match else None,
                "accept_proxy": accept_proxy,
            }

        for match in backend_pattern.finditer(content):
            rule_type, name, block = match.groups()
            if name not in frontends:
                continue

            fe = frontends[name]
            server_lines = re.findall(r'^\s*server\s+.+', block, re.MULTILINE)
            has_balance = bool(re.search(r'^\s*balance\s+', block, re.MULTILINE))

            if len(server_lines) > 1 or has_balance:
                servers = []
                for line in server_lines:
                    srv = self._parse_server_line(line)
                    if srv:
                        servers.append(srv)
                balancer_options = self._parse_balancer_options(block)
                rules.append(HAProxyRule(
                    name=name, rule_type=fe["type"], listen_port=fe["port"],
                    target_ip=servers[0].address if servers else "",
                    target_port=servers[0].port if servers else 0,
                    cert_domain=fe["cert_domain"],
                    accept_proxy=fe["accept_proxy"],
                    is_balancer=True, servers=servers, balancer_options=balancer_options,
                ))
            else:
                server_match = re.search(r'server\s+\S+\s+(\S+):(\d+)', block)
                if server_match:
                    target_ssl = bool(re.search(r'server\s+\S+\s+\S+:\d+\s+ssl', block))
                    send_proxy = bool(re.search(r'send-proxy', block))
                    rules.append(HAProxyRule(
                        name=name, rule_type=fe["type"], listen_port=fe["port"],
                        target_ip=server_match.group(1),
                        target_port=int(server_match.group(2)),
                        cert_domain=fe["cert_domain"],
                        target_ssl=target_ssl, send_proxy=send_proxy,
                        accept_proxy=fe["accept_proxy"],
                    ))

        return rules
    
    def rule_exists(self, name: str) -> bool:
        """Check if rule with name exists"""
        return any(r.name == name for r in self.parse_rules())
    
    def get_rule(self, name: str) -> Optional[HAProxyRule]:
        """Get rule by name"""
        for rule in self.parse_rules():
            if rule.name == name:
                return rule
        return None
    
    @staticmethod
    def _extract_parent_domain(domain: str) -> str:
        """Extract parent domain: sub.example.com -> example.com"""
        parts = domain.split('.')
        if len(parts) <= 2:
            return domain
        return '.'.join(parts[1:])

    def _resolve_cert_domain(self, cert_domain: str, use_wildcard: bool) -> str:
        """Resolve actual cert domain, checking wildcard parent if needed."""
        if use_wildcard:
            return self._extract_parent_domain(cert_domain)
        return cert_domain

    def _get_cert_path(self, domain: str) -> Path:
        """Get path to combined certificate for domain"""
        # Try to find actual cert directory (may have -0001 suffix)
        cert_dir = self._find_cert_dir(domain)
        if cert_dir:
            return cert_dir / "combined.pem"
        return self.certs_dir / domain / "combined.pem"
    
    def _create_combined_cert(self, domain: str) -> Optional[Path]:
        """Create combined cert for HAProxy in /etc/letsencrypt/live/{domain}/"""
        # Find actual cert directory (may have -0001 suffix)
        cert_dir = self._find_cert_dir(domain)
        if not cert_dir:
            cert_dir = self.certs_dir / domain
        
        fullchain = cert_dir / "fullchain.pem"
        privkey = cert_dir / "privkey.pem"
        
        if not fullchain.exists():
            logger.warning(f"Certificate fullchain.pem not found for {domain} at {fullchain}")
            return None
        
        if not privkey.exists():
            logger.warning(f"Certificate privkey.pem not found for {domain} at {privkey}")
            return None
        
        try:
            combined = cert_dir / "combined.pem"
            content = fullchain.read_text() + privkey.read_text()
            combined.write_text(content)
            combined.chmod(0o600)
            self._invalidate_cert_info(cert_dir)
            logger.info(f"Created/updated combined cert for {domain} at {combined}")
            return combined
        except Exception as e:
            logger.error(f"Failed to create combined cert for {domain}: {e}")
            return None
    
    def add_rule(self, rule: HAProxyRule) -> tuple[bool, str]:
        """Add new rule to config"""
        if self.rule_exists(rule.name):
            return False, f"Rule '{rule.name}' already exists"
        
        if not re.match(r'^[a-zA-Z0-9_-]+$', rule.name):
            return False, "Invalid rule name (use a-z, A-Z, 0-9, -, _)"
        
        if not 1 <= rule.listen_port <= 65535:
            return False, "Invalid listen port"
        
        if not 1 <= rule.target_port <= 65535:
            return False, "Invalid target port"
        
        with self._config_rollback() as rollback:
            content = self._read_config()

            if not content:
                # No config exists - initialize with base config
                self.init_config()
                content = self._read_config()
            elif RULES_START_MARKER not in content:
                # Config exists but doesn't have our markers - add them at the end
                logger.info("Adding rule markers to existing config")
                content = content.rstrip() + f"\n\n{RULES_START_MARKER}\n{RULES_END_MARKER}\n"
                self._write_config(content)

            # Ensure resolvers section exists for domain targets
            if self._is_domain(rule.target_ip) and 'resolvers mydns' not in content:
                resolvers_block = (
                    "\nresolvers mydns\n"
                    "    nameserver dns1 1.1.1.1:53\n"
                    "    nameserver dns2 8.8.8.8:53\n"
                    "    resolve_retries 3\n"
                    "    timeout resolve 1s\n"
                    "    timeout retry 1s\n"
                    "    hold valid 60s\n"
                    "    hold nx 10s\n"
                    "    hold other 10s\n"
                )
                content = content.replace(RULES_START_MARKER, resolvers_block + "\n" + RULES_START_MARKER)
                self._write_config(content)

            frontend_name = f"{rule.rule_type}_{rule.name}"
            backend_name = f"backend_{rule.rule_type}_{rule.name}"

            resolver_opts = " resolvers mydns resolve-prefer ipv4 init-addr none" if self._is_domain(rule.target_ip) else ""
            accept_proxy_opt = " accept-proxy" if rule.accept_proxy else ""

            if rule.rule_type == "tcp":
                server_opts = ""
                if rule.send_proxy:
                    server_opts += " send-proxy check-send-proxy"
                server_opts += " check inter 5s fall 3 rise 2"

                new_block = f"""
frontend {frontend_name}
    bind *:{rule.listen_port}{accept_proxy_opt}
    mode tcp
    default_backend {backend_name}

backend {backend_name}
    mode tcp
    option tcp-check
    server srv1 {rule.target_ip}:{rule.target_port}{resolver_opts}{server_opts}
"""
            else:
                if not rule.cert_domain:
                    rollback()
                    return False, "Certificate domain required for HTTPS"

                resolved_domain = self._resolve_cert_domain(rule.cert_domain, rule.use_wildcard)

                cert_path = self._get_cert_path(resolved_domain)
                if not cert_path.exists():
                    created = self._create_combined_cert(resolved_domain)
                    if not created:
                        rollback()
                        if rule.use_wildcard:
                            return False, f"Wildcard certificate for {resolved_domain} not found (looked in {self.certs_dir / resolved_domain})"
                        return False, f"Certificate for {rule.cert_domain} not found"
                    cert_path = created

                server_line = f"server srv1 {rule.target_ip}:{rule.target_port}"
                if rule.target_ssl:
                    server_line += f" ssl verify none sni str({rule.target_ip})"
                server_line += resolver_opts

                new_block = f"""
frontend {frontend_name}
    bind *:{rule.listen_port} ssl crt {cert_path}{accept_proxy_opt}
    mode http
    default_backend {backend_name}

backend {backend_name}
    mode http
    http-request set-header Host {rule.target_ip}
    http-request set-header X-Forwarded-Proto https
    http-request set-header X-Forwarded-For %[src]
    {server_line}
"""

            content = content.replace(RULES_END_MARKER, new_block + RULES_END_MARKER)
            self._write_config(content)

            is_valid, error = self.check_config()
            if not is_valid:
                rollback()
                return False, f"Config validation failed: {error}"

            # Reload with auto_start=False - don't fail if service not running
            success, reload_msg = self.reload(auto_start=False)
            if not success:
                rollback()
                return False, f"Reload failed: {reload_msg}"

        # Return message based on reload result
        if "not running" in reload_msg or "stopped" in reload_msg:
            return True, f"Rule created ({reload_msg})"
        return True, "Rule created"

    def delete_rule(self, name: str) -> tuple[bool, str]:
        """Delete rule from config"""
        rule = self.get_rule(name)
        if not rule:
            return False, f"Rule '{name}' not found"

        with self._config_rollback() as rollback:
            content = self._read_config()

            frontend_name = f"{rule.rule_type}_{name}"
            backend_name = f"backend_{rule.rule_type}_{name}"

            content = re.sub(
                rf'^frontend\s+{re.escape(frontend_name)}\s*\n.*?(?=^frontend|^backend|{re.escape(RULES_END_MARKER)})',
                '', content, flags=re.MULTILINE | re.DOTALL
            )
            content = re.sub(
                rf'^backend\s+{re.escape(backend_name)}\s*\n.*?(?=^frontend|^backend|{re.escape(RULES_END_MARKER)})',
                '', content, flags=re.MULTILINE | re.DOTALL
            )
            content = re.sub(r'\n{3,}', '\n\n', content)

            self._write_config(content)

            is_valid, error = self.check_config()
            if not is_valid:
                rollback()
                return False, f"Config validation failed: {error}"

            # Reload with auto_start=False - don't fail if service not running
            success, reload_msg = self.reload(auto_start=False)
            if not success:
                rollback()
                return False, f"Reload failed: {reload_msg}"

        # Return message based on reload result
        if "not running" in reload_msg or "stopped" in reload_msg:
            return True, f"Rule deleted ({reload_msg})"
        return True, "Rule deleted"
    
    def update_rule(self, name: str, updates: dict) -> tuple[bool, str]:
        """Update rule fields. Recreates rule block if type changes."""
        rule = self.get_rule(name)
        if not rule:
            return False, f"Rule '{name}' not found"
        
        new_type = updates.get("rule_type", rule.rule_type)
        new_cert_domain = updates.get("cert_domain", rule.cert_domain)
        new_target_ssl = updates.get("target_ssl", rule.target_ssl)
        new_send_proxy = updates.get("send_proxy", rule.send_proxy)
        new_accept_proxy = updates.get("accept_proxy", rule.accept_proxy)
        new_use_wildcard = updates.get("use_wildcard", rule.use_wildcard)

        type_changed = new_type != rule.rule_type
        cert_changed = new_cert_domain != rule.cert_domain
        ssl_changed = new_target_ssl != rule.target_ssl
        proxy_changed = new_send_proxy != rule.send_proxy
        accept_proxy_changed = new_accept_proxy != rule.accept_proxy
        wildcard_changed = new_use_wildcard != rule.use_wildcard

        if type_changed or cert_changed or ssl_changed or proxy_changed or accept_proxy_changed or wildcard_changed:
            if new_type == "https" and not new_cert_domain:
                return False, "Certificate domain required for HTTPS rules"

            new_rule = HAProxyRule(
                name=name,
                rule_type=new_type,
                listen_port=updates.get("listen_port", rule.listen_port),
                target_ip=updates.get("target_ip", rule.target_ip),
                target_port=updates.get("target_port", rule.target_port),
                cert_domain=new_cert_domain if new_type == "https" else None,
                target_ssl=new_target_ssl,
                send_proxy=new_send_proxy,
                accept_proxy=new_accept_proxy,
                use_wildcard=new_use_wildcard,
            )
            
            # Delete old rule and add new one
            success, msg = self.delete_rule(name)
            if not success:
                return False, f"Failed to delete old rule: {msg}"
            
            success, msg = self.add_rule(new_rule)
            if not success:
                return False, f"Failed to create new rule: {msg}"
            
            return True, f"Rule recreated with new type: {new_type}"
        
        # Simple field updates (no type change) - use regex replacement
        with self._config_rollback() as rollback:
            content = self._read_config()

            frontend_name = f"{rule.rule_type}_{name}"
            backend_name = f"backend_{rule.rule_type}_{name}"

            if "listen_port" in updates:
                port = updates["listen_port"]
                if not 1 <= port <= 65535:
                    return False, "Invalid listen port"
                content = re.sub(
                    rf'(frontend\s+{re.escape(frontend_name)}.*?bind\s+\*:)\d+',
                    rf'\g<1>{port}', content, flags=re.DOTALL
                )

            if "target_ip" in updates:
                ip = updates["target_ip"]
                content = re.sub(
                    rf'(backend\s+{re.escape(backend_name)}.*?server\s+\S+\s+)\S+:(\d+)',
                    rf'\g<1>{ip}:\2', content, flags=re.DOTALL
                )

            if "target_port" in updates:
                port = updates["target_port"]
                if not 1 <= port <= 65535:
                    return False, "Invalid target port"
                content = re.sub(
                    rf'(backend\s+{re.escape(backend_name)}.*?server\s+\S+\s+\S+:)\d+',
                    rf'\g<1>{port}', content, flags=re.DOTALL
                )

            # Нормализация: send-proxy без check-send-proxy
            if new_send_proxy:
                lines = content.split('\n')
                for i, line in enumerate(lines):
                    if 'send-proxy' in line and 'check-send-proxy' not in line:
                        lines[i] = line.replace('send-proxy', 'send-proxy check-send-proxy')
                content = '\n'.join(lines)

            self._write_config(content)

            is_valid, error = self.check_config()
            if not is_valid:
                rollback()
                return False, f"Config validation failed: {error}"

            # Reload with auto_start=False - don't fail if service not running
            success, reload_msg = self.reload(auto_start=False)
            if not success:
                rollback()
                return False, f"Reload failed: {reload_msg}"

        # Return message based on reload result
        if "not running" in reload_msg or "stopped" in reload_msg:
            return True, f"Rule updated ({reload_msg})"
        return True, "Rule updated"
    
    def get_config(self) -> str:
        """Get full config content"""
        return self._read_config()
    
    def apply_config(self, config_content: str, reload_after: bool = True, ensure_started: bool = False) -> tuple[bool, str, bool]:
        """Apply HAProxy config from panel.

        Args:
            config_content: Full config content
            reload_after: Whether to reload HAProxy after applying
            ensure_started: Start HAProxy if stopped (enable autostart) instead of leaving it down

        Returns: (success, message, reloaded)
        """
        with self._config_rollback() as rollback:
            try:
                config_content = self._patch_dns_resolvers(config_content)
                config_content = self._ensure_global_maxconn(config_content)
                config_content = cpu_affinity.apply(config_content, psutil.cpu_count() or 1)
                self._write_config(config_content)
            except Exception as e:
                rollback()
                return False, f"Failed to write config: {e}", False

            # Validate config
            is_valid, error = self.check_config()
            if not is_valid:
                rollback()
                return False, f"Config validation failed: {error}", False

            if not reload_after:
                return True, "Config applied (reload skipped)", False

            # ensure_started=True поднимает остановленный сервис (start + enable),
            # иначе только reload работающего, а остановленный остаётся лежать
            success, reload_msg = self.reload(auto_start=ensure_started)
            if not success:
                rollback()
                return False, f"Reload failed: {reload_msg}", False

        # Check if HAProxy was actually reloaded or just config saved
        if "not running" in reload_msg or "stopped" in reload_msg:
            return True, f"Config applied ({reload_msg})", False
        return True, "Config applied and reloaded", True
    
    def get_available_certs(self) -> list[str]:
        """Get list of available certificates from /etc/letsencrypt/live/"""
        certs = []
        
        if self.certs_dir.exists():
            for d in self.certs_dir.iterdir():
                # Handle both directories and symlinks to directories
                if d.name == "README":
                    continue
                if d.is_dir() or (d.is_symlink() and d.resolve().is_dir()):
                    if (d / "fullchain.pem").exists() and (d / "privkey.pem").exists():
                        certs.append(d.name)
        
        return sorted(certs)
    
    def _invalidate_cert_info(self, cert_dir: Path) -> None:
        self._cert_info_cache.pop(str(cert_dir / "fullchain.pem"), None)

    def get_cert_info(self, domain: str) -> Optional[dict]:
        """Get certificate information including expiry date and file paths"""
        # Find actual cert directory (handles suffixes like -0001)
        cert_dir = self._find_cert_dir(domain)
        if not cert_dir:
            cert_dir = self.certs_dir / domain

        cert_file = cert_dir / "fullchain.pem"

        if not cert_file.exists():
            self._invalidate_cert_info(cert_dir)
            return None

        cached_at, cached_info = self._cert_info_cache.get(str(cert_file), (0.0, {}))
        if cached_info and time.time() - cached_at < CERT_INFO_CACHE_TTL_SEC:
            # Каталог мог быть найден по имени с суффиксом (-0001), а спрашивали
            # про базовый домен — отвечаем тем именем, о котором спросили
            return {**cached_info, "domain": domain}

        try:
            result = subprocess.run(
                ["openssl", "x509", "-enddate", "-noout", "-in", str(cert_file)],
                capture_output=True, text=True, timeout=OPENSSL_TIMEOUT_SEC
            )
            if result.returncode == 0:
                date_str = result.stdout.strip().split("=")[1]
                expiry = datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z")
                days_left = (expiry - datetime.now()).days

                combined = cert_dir / "combined.pem"

                files = {
                    "pem": str(combined) if combined.exists() else None,
                    "key": str(cert_dir / "privkey.pem") if (cert_dir / "privkey.pem").exists() else None,
                    "cert": str(cert_dir / "cert.pem") if (cert_dir / "cert.pem").exists() else None,
                    "fullchain": str(cert_file),
                    "chain": str(cert_dir / "chain.pem") if (cert_dir / "chain.pem").exists() else None,
                }

                info = {
                    "domain": domain,
                    "expiry_date": expiry.isoformat(),
                    "days_left": days_left,
                    "expired": days_left < 0,
                    "combined_exists": combined.exists(),
                    "cert_path": str(cert_dir),
                    "files": files
                }
                self._cert_info_cache[str(cert_file)] = (time.time(), info)
                return info
        except Exception as e:
            logger.error(f"Error getting cert info for {domain}: {e}")

        return None

    def _find_cert_dir(self, domain: str) -> Optional[Path]:
        """Find certificate directory for domain (handles -0001 suffixes and symlinks)"""
        # First try exact match
        exact = self.certs_dir / domain
        if exact.exists() and (exact / "fullchain.pem").exists():
            return exact
        
        # Look for directories with suffixes like domain-0001, domain-0002
        if self.certs_dir.exists():
            for d in sorted(self.certs_dir.iterdir(), reverse=True):
                # Handle both directories and symlinks
                is_dir = d.is_dir() or (d.is_symlink() and d.resolve().is_dir())
                if is_dir and d.name.startswith(domain):
                    if (d / "fullchain.pem").exists():
                        return d
        
        return None
    
    async def _run_certbot(self, cmd: list[str], timeout_sec: int) -> CertbotResult:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return CertbotResult(returncode=-1, stdout="", stderr="", timed_out=True)

        return CertbotResult(
            returncode=process.returncode,
            stdout=stdout.decode('utf-8', errors='replace') if stdout else "",
            stderr=stderr.decode('utf-8', errors='replace') if stderr else "",
        )

    def _release_port_80(self) -> tuple[bool, Optional[str]]:
        """Освободить 80-й порт под проверку certbot: открыть его в firewall и
        остановить HAProxy, но только если какое-то правило действительно этот
        порт слушает — безусловная остановка рвала бы установленные туннели на
        всех остальных портах на всё время выпуска сертификата.

        Возвращает (нужно ли поднимать HAProxy обратно, ошибку остановки)."""
        from app.services.firewall_manager import get_firewall_manager

        port_opened, fw_msg, _ = get_firewall_manager().add_rule(80, "tcp")
        if port_opened:
            logger.info("Firewall: port 80 opened for certbot")
        else:
            logger.warning(f"Could not open port 80: {fw_msg}")

        if not self.is_running():
            return False, None

        if not any(r.listen_port == 80 for r in self.parse_rules()):
            return False, None

        stopped, msg = self._temporary_stop()
        if not stopped:
            return False, msg

        logger.info("Stopped HAProxy for certbot (a rule binds port 80)")
        return True, None

    def _reclaim_port_80(self, resume_haproxy: bool) -> None:
        if not resume_haproxy:
            return

        started, msg = self._temporary_start()
        if started:
            logger.info("HAProxy restarted after certbot")
        else:
            logger.error(f"Failed to restart HAProxy: {msg}")

    async def generate_certificate(
        self,
        domain: str,
        email: str = None,
        method: str = "standalone"
    ) -> tuple[bool, str, Optional[str]]:
        """Generate Let's Encrypt certificate using certbot (async)

        Returns: (success, message, error_log)
        """
        if not shutil.which("certbot"):
            return False, "certbot not installed in container", None

        # Build certbot command
        cmd = ["certbot", "certonly", "--non-interactive", "--agree-tos"]

        if email:
            cmd.extend(["--email", email])
        else:
            cmd.append("--register-unsafely-without-email")

        if method == "standalone":
            resume_haproxy, stop_error = await asyncio.to_thread(self._release_port_80)
            if stop_error:
                return False, f"Failed to stop HAProxy: {stop_error}", None

            cmd.extend(["--standalone", "-d", domain])
            error_log = None

            try:
                result = await self._run_certbot(cmd, CERTBOT_ISSUE_TIMEOUT_SEC)

                if result.timed_out:
                    message = f"Certificate generation timed out ({CERTBOT_ISSUE_TIMEOUT_SEC}s)"
                    error_log = f"Command: {' '.join(cmd)}\n\nError: Timeout"
                    success = False
                elif result.returncode == 0:
                    # Find the actual cert directory (may have -0001 suffix)
                    cert_dir = await asyncio.to_thread(self._find_cert_dir, domain)
                    if cert_dir:
                        await asyncio.to_thread(self._create_combined_cert, cert_dir.name)
                        message = f"Certificate for {domain} generated successfully"
                        logger.info(message)
                        success = True
                    else:
                        message = "Certificate created but directory not found"
                        error_log = f"Looked in: {self.certs_dir}"
                        success = False
                else:
                    message = result.stderr or result.stdout or "Unknown error"
                    error_log = (
                        f"Command: {' '.join(cmd)}\n\nExit code: {result.returncode}\n\n"
                        f"Stdout:\n{result.stdout}\n\nStderr:\n{result.stderr}"
                    )
                    logger.error(f"Certbot failed: {message}")
                    success = False

            except Exception as e:
                message = str(e)
                error_log = f"Command: {' '.join(cmd)}\n\nException: {str(e)}"
                success = False
            finally:
                await asyncio.to_thread(self._reclaim_port_80, resume_haproxy)

            return success, message, error_log

        elif method == "webroot":
            webroot_path = "/var/www/html"
            cmd.extend(["--webroot", "-w", webroot_path, "-d", domain])

            try:
                result = await self._run_certbot(cmd, CERTBOT_ISSUE_TIMEOUT_SEC)

                if result.timed_out:
                    return (
                        False,
                        f"Certificate generation timed out ({CERTBOT_ISSUE_TIMEOUT_SEC}s)",
                        f"Command: {' '.join(cmd)}\n\nError: Timeout",
                    )

                if result.returncode == 0:
                    cert_dir = await asyncio.to_thread(self._find_cert_dir, domain)
                    if cert_dir:
                        await asyncio.to_thread(self._create_combined_cert, cert_dir.name)
                    return True, f"Certificate for {domain} generated successfully", None

                error_log = (
                    f"Command: {' '.join(cmd)}\n\nExit code: {result.returncode}\n\n"
                    f"Stdout:\n{result.stdout}\n\nStderr:\n{result.stderr}"
                )
                return False, result.stderr or result.stdout or "Unknown error", error_log

            except Exception as e:
                error_log = f"Command: {' '.join(cmd)}\n\nException: {str(e)}"
                return False, str(e), error_log

        else:
            return False, f"Unknown method: {method}. Use 'standalone' or 'webroot'", None

    async def renew_certificates(self) -> tuple[bool, str, list[str]]:
        """Renew all Let's Encrypt certificates (async)"""
        if not shutil.which("certbot"):
            return False, "certbot not installed", []

        logger.info("Starting renewal of all certificates")

        resume_haproxy, stop_error = await asyncio.to_thread(self._release_port_80)
        if stop_error:
            logger.warning(f"Could not stop HAProxy: {stop_error}")

        renewed = []
        try:
            logger.info("Running certbot renew --non-interactive")

            result = await self._run_certbot(
                ["certbot", "renew", "--non-interactive"], CERTBOT_RENEW_TIMEOUT_SEC
            )

            if result.timed_out:
                logger.error("Certificate renewal timed out")
                return False, f"Renewal timed out ({CERTBOT_RENEW_TIMEOUT_SEC}s)", []

            logger.info(f"Certbot finished with exit code {result.returncode}")
            if result.stdout:
                logger.debug(f"Certbot stdout: {result.stdout[:500]}")
            if result.stderr:
                logger.debug(f"Certbot stderr: {result.stderr[:500]}")

            failed = []

            # Update combined certs for ALL available certificates
            available_certs = await asyncio.to_thread(self.get_available_certs)
            logger.info(f"Updating combined certificates for {len(available_certs)} domains: {available_certs}")

            for domain in available_certs:
                logger.info(f"Processing certificate for {domain}")
                if await asyncio.to_thread(self._create_combined_cert, domain):
                    renewed.append(domain)
                else:
                    failed.append(domain)
                    logger.warning(f"Failed to update combined cert for {domain}")

            if result.returncode == 0:
                message = f"Renewal completed. Updated: {len(renewed)}, Failed: {len(failed)}"
                success = True
                logger.info(f"Renewal completed, updated {len(renewed)} certificates, failed {len(failed)}")
            else:
                message = result.stderr or result.stdout or "Renewal failed"
                success = False
                logger.error(f"Renewal failed: {message[:200]}")

        except Exception as e:
            message = str(e)
            success = False
            renewed = []
            logger.exception("Exception during certificate renewal")
        finally:
            await asyncio.to_thread(self._reclaim_port_80, resume_haproxy)

        if renewed:
            await asyncio.to_thread(self.reload, auto_start=False)

        return success, message, renewed

    async def renew_certificate(self, domain: str) -> tuple[bool, str, Optional[str]]:
        """
        Renew specific Let's Encrypt certificate (async)
        
        Args:
            domain: Domain name to renew
        
        Returns: (success, message, output_log)
        """
        if not shutil.which("certbot"):
            return False, "certbot not installed", None

        # Check if certificate exists
        cert_dir = await asyncio.to_thread(self._find_cert_dir, domain)
        if not cert_dir:
            return False, f"Certificate for {domain} not found", None

        logger.info(f"Starting certificate renewal for {domain} (cert_dir: {cert_dir.name})")

        resume_haproxy, stop_error = await asyncio.to_thread(self._release_port_80)
        if stop_error:
            logger.warning(f"Could not stop HAProxy: {stop_error}")

        output_log = ""
        try:
            # Use certonly --standalone like during generation (more reliable than renew)
            # This explicitly uses standalone authenticator and doesn't depend on renewal config
            cmd = [
                "certbot", "certonly",
                "--standalone",
                "--non-interactive",
                "--agree-tos",
                "--register-unsafely-without-email",
                "--force-renewal",
                "-d", domain
            ]
            
            logger.info(f"Running certbot command: {' '.join(cmd)}")

            result = await self._run_certbot(cmd, CERTBOT_RENEW_TIMEOUT_SEC)

            if result.timed_out:
                logger.error(f"Certificate renewal timed out for {domain}")
                return (
                    False,
                    f"Certificate renewal timed out ({CERTBOT_RENEW_TIMEOUT_SEC}s)",
                    f"Command: {' '.join(cmd)}\n\nError: Timeout",
                )

            output_log = (
                f"Command: {' '.join(cmd)}\n\nExit code: {result.returncode}\n\n"
                f"Stdout:\n{result.stdout}\n\nStderr:\n{result.stderr}"
            )

            logger.info(f"Certbot finished with exit code {result.returncode}")
            if result.stdout:
                logger.debug(f"Certbot stdout: {result.stdout[:500]}")
            if result.stderr:
                logger.debug(f"Certbot stderr: {result.stderr[:500]}")

            if result.returncode == 0:
                # Find the cert directory (may have suffix like -0001) and update combined cert
                renewed_cert_dir = await asyncio.to_thread(self._find_cert_dir, domain)
                if renewed_cert_dir:
                    await asyncio.to_thread(self._create_combined_cert, renewed_cert_dir.name)
                    message = f"Certificate for {domain} renewed successfully"
                    success = True
                    logger.info(message)
                else:
                    message = f"Certificate renewed but directory not found for {domain}"
                    success = False
                    logger.error(message)
            else:
                message = result.stderr or result.stdout or "Renewal failed"
                success = False
                logger.error(f"Certificate renewal failed for {domain}: {message[:200]}")

        except Exception as e:
            message = str(e)
            output_log = f"Exception: {str(e)}"
            success = False
            logger.exception(f"Exception during certificate renewal for {domain}")
        finally:
            await asyncio.to_thread(self._reclaim_port_80, resume_haproxy)

        if success:
            await asyncio.to_thread(self.reload, auto_start=False)
        
        return success, message, output_log
    
    def get_all_certs_info(self) -> list[dict]:
        """Get information about all available certificates"""
        certs_info = []
        
        for domain in self.get_available_certs():
            info = self.get_cert_info(domain)
            if info:
                certs_info.append(info)
        
        # Sort by days_left (closest to expiry first)
        certs_info.sort(key=lambda x: x.get("days_left", 999999))
        
        return certs_info
    
    def delete_certificate(self, domain: str) -> tuple[bool, str]:
        """Delete certificate files for a domain"""
        deleted_files = []
        errors = []
        
        # Check if certificate is used in any HAProxy rule
        rules = self.parse_rules()
        # Check both exact match and with suffix
        using_rules = [r.name for r in rules if r.cert_domain and (
            r.cert_domain == domain or 
            r.cert_domain.startswith(domain + "-") or
            domain.startswith(r.cert_domain + "-")
        )]
        if using_rules:
            return False, f"Certificate is used by rules: {', '.join(using_rules)}. Delete rules first."
        
        # Find actual cert directory (handles suffixes like -0001)
        cert_dir = self._find_cert_dir(domain)
        if not cert_dir:
            cert_dir = self.certs_dir / domain
        
        if not cert_dir.exists():
            return False, f"Certificate for {domain} not found"
        
        try:
            # Delete certificate files
            for cert_file in ["fullchain.pem", "privkey.pem", "cert.pem", "chain.pem", "combined.pem"]:
                f = cert_dir / cert_file
                if f.exists():
                    f.unlink()
                    deleted_files.append(str(f))
            
            # Try to remove the directory
            if cert_dir.exists() and not any(cert_dir.iterdir()):
                cert_dir.rmdir()
                deleted_files.append(str(cert_dir))

            self._invalidate_cert_info(cert_dir)
            logger.info(f"Deleted certificate for {domain}")
        except PermissionError:
            errors.append(f"No permission to delete: {cert_dir}")
        except Exception as e:
            errors.append(str(e))
        
        if not deleted_files and not errors:
            return False, f"Certificate for {domain} not found"
        
        if errors:
            return len(deleted_files) > 0, f"Partial delete. Deleted: {len(deleted_files)} files. Errors: {'; '.join(errors)}"
        
        return True, f"Certificate for {domain} deleted successfully ({len(deleted_files)} files)"
    
    def upload_certificate(
        self, 
        domain: str, 
        cert_content: str, 
        key_content: str
    ) -> tuple[bool, str]:
        """Upload custom certificate to /etc/letsencrypt/live/{domain}/"""

        if not CERT_DOMAIN_RE.match(domain):
            return False, "Invalid domain name"

        if not cert_content or not key_content:
            return False, "Certificate and key content required"
        
        if "-----BEGIN CERTIFICATE-----" not in cert_content:
            return False, "Invalid certificate format (missing BEGIN CERTIFICATE)"
        
        if "-----BEGIN" not in key_content or "PRIVATE KEY" not in key_content:
            return False, "Invalid key format (missing PRIVATE KEY)"
        
        cert_dir = self.certs_dir / domain
        cert_dir.mkdir(parents=True, exist_ok=True)
        
        fullchain_file = cert_dir / "fullchain.pem"
        privkey_file = cert_dir / "privkey.pem"
        combined_file = cert_dir / "combined.pem"
        
        try:
            # Validate certificate before saving
            with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as tmp:
                tmp.write(cert_content)
                tmp_path = tmp.name

            try:
                result = subprocess.run(
                    ["openssl", "x509", "-noout", "-in", tmp_path],
                    capture_output=True, text=True, timeout=OPENSSL_TIMEOUT_SEC
                )
            finally:
                Path(tmp_path).unlink(missing_ok=True)

            if result.returncode != 0:
                return False, "Invalid certificate: OpenSSL validation failed"

            # Save certificate (as fullchain)
            fullchain_file.write_text(cert_content.strip() + "\n")
            fullchain_file.chmod(0o644)

            # Save private key
            privkey_file.write_text(key_content.strip() + "\n")
            privkey_file.chmod(0o600)

            # Create combined .pem for HAProxy
            combined_content = cert_content.strip() + "\n" + key_content.strip() + "\n"
            combined_file.write_text(combined_content)
            combined_file.chmod(0o600)

            self._invalidate_cert_info(cert_dir)
            logger.info(f"Uploaded certificate for {domain}")
            return True, f"Certificate for {domain} uploaded successfully"
            
        except Exception as e:
            # Cleanup on error
            fullchain_file.unlink(missing_ok=True)
            privkey_file.unlink(missing_ok=True)
            combined_file.unlink(missing_ok=True)
            if cert_dir.exists() and not any(cert_dir.iterdir()):
                cert_dir.rmdir()
            logger.error(f"Failed to upload certificate: {e}")
            return False, f"Failed to upload: {e}"
    

# Singleton instance
_manager: Optional[HAProxyManager] = None


def get_haproxy_manager() -> HAProxyManager:
    """Get or create HAProxy manager instance"""
    global _manager
    if _manager is None:
        _manager = HAProxyManager()
    return _manager
