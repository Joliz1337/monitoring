"""HAProxy configuration manager for native systemd HAProxy service"""

import asyncio
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.services.host_executor import get_host_executor

logger = logging.getLogger(__name__)

RULES_START_MARKER = "# === RULES START ==="
RULES_END_MARKER = "# === RULES END ==="


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
    send_proxy: bool = False  # PROXY protocol to backend


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
    
    def _backup_config(self):
        """Create backup of current config"""
        if self.config_path.exists():
            backup_path = Path(f"{self.config_path}.bak")
            shutil.copy(self.config_path, backup_path)
    
    def _restore_config(self):
        """Restore config from backup"""
        backup_path = Path(f"{self.config_path}.bak")
        if backup_path.exists():
            shutil.copy(backup_path, self.config_path)
    
    def _generate_base_config(self) -> str:
        """Generate base HAProxy config for high-speed TCP relay"""
        return f"""global
    stats socket /var/run/haproxy.sock mode 660 level admin expose-fd listeners
    no log
    tune.bufsize 32768
    tune.maxpollevents 1024
    tune.recv_enough 16384

defaults
    mode tcp
    timeout connect 5s
    timeout client 30m
    timeout server 30m
    timeout tunnel 2h
    timeout client-fin 5s
    timeout server-fin 5s
    option dontlognull
    option redispatch
    option tcp-smart-accept
    option tcp-smart-connect
    option splice-auto
    option clitcpka
    option srvtcpka

{RULES_START_MARKER}
{RULES_END_MARKER}
"""
    
    def regenerate_config(self, preserve_rules: bool = True) -> tuple[bool, str]:
        """Regenerate HAProxy config preserving rules"""
        rules_content = ""
        
        if preserve_rules and self.config_path.exists():
            content = self._read_config()
            match = re.search(
                rf'{re.escape(RULES_START_MARKER)}(.*?){re.escape(RULES_END_MARKER)}',
                content, re.DOTALL
            )
            if match:
                rules_content = match.group(1)
        
        self._backup_config()
        new_config = self._generate_base_config()
        
        if rules_content.strip():
            new_config = new_config.replace(
                RULES_END_MARKER,
                rules_content.rstrip() + '\n' + RULES_END_MARKER
            )
        
        self._write_config(new_config)
        logger.info("Config regenerated")
        
        return True, "Config regenerated"
    
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
                return False, f"HAProxy failed to start. Check logs for details."
        
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
            # Match path like /etc/letsencrypt/live/{domain}/combined.pem
            cert_match = re.search(r'ssl\s+crt\s+/etc/letsencrypt/live/([^/]+)/combined\.pem', block) if rule_type == "https" else None
            
            frontends[name] = {
                "type": rule_type,
                "port": int(port_match.group(1)) if port_match else 0,
                "cert_domain": cert_match.group(1) if cert_match else None
            }
        
        for match in backend_pattern.finditer(content):
            rule_type, name, block = match.groups()
            if name in frontends:
                server_match = re.search(r'server\s+\S+\s+(\S+):(\d+)', block)
                if server_match:
                    target_ssl = bool(re.search(r'server\s+\S+\s+\S+:\d+\s+ssl', block))
                    send_proxy = bool(re.search(r'send-proxy', block))
                    
                    rules.append(HAProxyRule(
                        name=name,
                        rule_type=frontends[name]["type"],
                        listen_port=frontends[name]["port"],
                        target_ip=server_match.group(1),
                        target_port=int(server_match.group(2)),
                        cert_domain=frontends[name]["cert_domain"],
                        target_ssl=target_ssl,
                        send_proxy=send_proxy
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
        
        self._backup_config()
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
        
        frontend_name = f"{rule.rule_type}_{rule.name}"
        backend_name = f"backend_{rule.rule_type}_{rule.name}"
        
        if rule.rule_type == "tcp":
            server_opts = ""
            if rule.send_proxy:
                server_opts += " send-proxy"
            server_opts += " check inter 5s fall 3 rise 2"
            
            new_block = f"""
frontend {frontend_name}
    bind *:{rule.listen_port}
    mode tcp
    default_backend {backend_name}

backend {backend_name}
    mode tcp
    option tcp-check
    server srv1 {rule.target_ip}:{rule.target_port}{server_opts}
"""
        else:
            if not rule.cert_domain:
                self._restore_config()
                return False, "Certificate domain required for HTTPS"
            
            cert_path = self._get_cert_path(rule.cert_domain)
            if not cert_path.exists():
                created = self._create_combined_cert(rule.cert_domain)
                if not created:
                    self._restore_config()
                    return False, f"Certificate for {rule.cert_domain} not found"
                cert_path = created
            
            # Build server line with optional SSL to target
            server_line = f"server srv1 {rule.target_ip}:{rule.target_port}"
            if rule.target_ssl:
                server_line += f" ssl verify none sni str({rule.target_ip})"
            
            new_block = f"""
frontend {frontend_name}
    bind *:{rule.listen_port} ssl crt {cert_path}
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
            self._restore_config()
            return False, f"Config validation failed: {error}"
        
        # Reload with auto_start=False - don't fail if service not running
        success, reload_msg = self.reload(auto_start=False)
        if not success:
            self._restore_config()
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
        
        self._backup_config()
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
            self._restore_config()
            return False, f"Config validation failed: {error}"
        
        # Reload with auto_start=False - don't fail if service not running
        success, reload_msg = self.reload(auto_start=False)
        if not success:
            self._restore_config()
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
        
        type_changed = new_type != rule.rule_type
        cert_changed = new_cert_domain != rule.cert_domain
        ssl_changed = new_target_ssl != rule.target_ssl
        proxy_changed = new_send_proxy != rule.send_proxy
        
        if type_changed or cert_changed or ssl_changed or proxy_changed:
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
                send_proxy=new_send_proxy
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
        self._backup_config()
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
        
        self._write_config(content)
        
        is_valid, error = self.check_config()
        if not is_valid:
            self._restore_config()
            return False, f"Config validation failed: {error}"
        
        # Reload with auto_start=False - don't fail if service not running
        success, reload_msg = self.reload(auto_start=False)
        if not success:
            self._restore_config()
            return False, f"Reload failed: {reload_msg}"
        
        # Return message based on reload result
        if "not running" in reload_msg or "stopped" in reload_msg:
            return True, f"Rule updated ({reload_msg})"
        return True, "Rule updated"
    
    def get_config(self) -> str:
        """Get full config content"""
        return self._read_config()
    
    def apply_config(self, config_content: str, reload_after: bool = True) -> tuple[bool, str, bool]:
        """Apply HAProxy config from panel.
        
        Args:
            config_content: Full config content
            reload_after: Whether to reload HAProxy after applying
        
        Returns: (success, message, reloaded)
        """
        self._backup_config()
        
        try:
            self._write_config(config_content)
        except Exception as e:
            return False, f"Failed to write config: {e}", False
        
        # Validate config
        is_valid, error = self.check_config()
        if not is_valid:
            self._restore_config()
            return False, f"Config validation failed: {error}", False
        
        if reload_after:
            # Reload with auto_start=False - don't fail if service not running
            success, reload_msg = self.reload(auto_start=False)
            if not success:
                self._restore_config()
                return False, f"Reload failed: {reload_msg}", False
            
            # Check if HAProxy was actually reloaded or just config saved
            if "not running" in reload_msg or "stopped" in reload_msg:
                return True, f"Config applied ({reload_msg})", False
            return True, "Config applied and reloaded", True
        
        return True, "Config applied (reload skipped)", False
    
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
    
    def get_cert_info(self, domain: str) -> Optional[dict]:
        """Get certificate information including expiry date and file paths"""
        # Find actual cert directory (handles suffixes like -0001)
        cert_dir = self._find_cert_dir(domain)
        if not cert_dir:
            cert_dir = self.certs_dir / domain
        
        cert_file = cert_dir / "fullchain.pem"
        
        if not cert_file.exists():
            return None
        
        try:
            result = subprocess.run(
                ["openssl", "x509", "-enddate", "-noout", "-in", str(cert_file)],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                from datetime import datetime
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
                
                return {
                    "domain": domain,
                    "expiry_date": expiry.isoformat(),
                    "days_left": days_left,
                    "expired": days_left < 0,
                    "combined_exists": combined.exists(),
                    "cert_path": str(cert_dir),
                    "files": files
                }
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
    
    async def generate_certificate(
        self, 
        domain: str, 
        email: str = None,
        method: str = "standalone"
    ) -> tuple[bool, str, Optional[str]]:
        """Generate Let's Encrypt certificate using certbot (async)
        
        Returns: (success, message, error_log)
        """
        from app.services.firewall_manager import get_firewall_manager
        
        if not shutil.which("certbot"):
            return False, "certbot not installed in container", None
        
        # Build certbot command
        cmd = ["certbot", "certonly", "--non-interactive", "--agree-tos"]
        
        if email:
            cmd.extend(["--email", email])
        else:
            cmd.append("--register-unsafely-without-email")
        
        if method == "standalone":
            # Open port 80 in firewall for domain validation
            firewall = get_firewall_manager()
            port_opened, fw_msg, fw_error = firewall.add_rule(80, "tcp")
            if port_opened:
                logger.info(f"Firewall: port 80 opened for certificate generation")
            else:
                logger.warning(f"Could not open port 80: {fw_msg}")
            
            was_running = self.is_running()
            
            rules = self.parse_rules()
            uses_port_80 = any(r.listen_port == 80 for r in rules)
            
            if uses_port_80 and was_running:
                success, msg = self._temporary_stop()
                if success:
                    logger.info("Stopped HAProxy for certificate generation")
                else:
                    return False, f"Failed to stop HAProxy: {msg}", None
            
            cmd.extend(["--standalone", "-d", domain])
            error_log = None
            
            try:
                # Use async subprocess to avoid blocking event loop
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
                    stdout_str = stdout.decode('utf-8', errors='replace') if stdout else ""
                    stderr_str = stderr.decode('utf-8', errors='replace') if stderr else ""
                    returncode = process.returncode
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    return False, "Certificate generation timed out (120s)", f"Command: {' '.join(cmd)}\n\nError: Timeout"
                
                if returncode == 0:
                    # Find the actual cert directory (may have -0001 suffix)
                    cert_dir = self._find_cert_dir(domain)
                    if cert_dir:
                        actual_domain = cert_dir.name
                        self._create_combined_cert(actual_domain)
                        message = f"Certificate for {domain} generated successfully"
                        logger.info(message)
                        success = True
                        # Ensure auto-renewal cron is configured
                        self.ensure_cert_renewal_cron()
                    else:
                        message = f"Certificate created but directory not found"
                        error_log = f"Looked in: {self.certs_dir}"
                        success = False
                else:
                    message = stderr_str or stdout_str or "Unknown error"
                    error_log = f"Command: {' '.join(cmd)}\n\nExit code: {returncode}\n\nStdout:\n{stdout_str}\n\nStderr:\n{stderr_str}"
                    logger.error(f"Certbot failed: {message}")
                    success = False
                
            except Exception as e:
                message = str(e)
                error_log = f"Command: {' '.join(cmd)}\n\nException: {str(e)}"
                success = False
            finally:
                if uses_port_80 and was_running:
                    start_success, start_msg = self._temporary_start()
                    if start_success:
                        logger.info("HAProxy restarted after certificate generation")
                    else:
                        logger.error(f"Failed to restart HAProxy: {start_msg}")
            
            return success, message, error_log
        
        elif method == "webroot":
            webroot_path = "/var/www/html"
            cmd.extend(["--webroot", "-w", webroot_path, "-d", domain])
            
            try:
                # Use async subprocess to avoid blocking event loop
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
                    stdout_str = stdout.decode('utf-8', errors='replace') if stdout else ""
                    stderr_str = stderr.decode('utf-8', errors='replace') if stderr else ""
                    returncode = process.returncode
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    return False, "Certificate generation timed out (120s)", f"Command: {' '.join(cmd)}\n\nError: Timeout"
                
                if returncode == 0:
                    cert_dir = self._find_cert_dir(domain)
                    if cert_dir:
                        self._create_combined_cert(cert_dir.name)
                    return True, f"Certificate for {domain} generated successfully", None
                else:
                    error_log = f"Command: {' '.join(cmd)}\n\nExit code: {returncode}\n\nStdout:\n{stdout_str}\n\nStderr:\n{stderr_str}"
                    return False, stderr_str or stdout_str or "Unknown error", error_log
                    
            except Exception as e:
                error_log = f"Command: {' '.join(cmd)}\n\nException: {str(e)}"
                return False, str(e), error_log
        
        else:
            return False, f"Unknown method: {method}. Use 'standalone' or 'webroot'", None
    
    async def renew_certificates(self) -> tuple[bool, str, list[str]]:
        """Renew all Let's Encrypt certificates (async)"""
        from app.services.firewall_manager import get_firewall_manager
        
        if not shutil.which("certbot"):
            return False, "certbot not installed", []
        
        logger.info("Starting renewal of all certificates")
        
        # Open port 80 in firewall for domain validation
        firewall = get_firewall_manager()
        port_opened, fw_msg, fw_error = firewall.add_rule(80, "tcp")
        if port_opened:
            logger.info("Firewall: port 80 opened for certificate renewal")
        else:
            logger.warning(f"Could not open port 80: {fw_msg}")
        
        # Stop HAProxy for renewal - standalone mode needs port 80 free
        was_running = self.is_running()
        
        if was_running:
            success, msg = self._temporary_stop()
            if success:
                logger.info("Stopped HAProxy for certificate renewal")
            else:
                logger.warning(f"Could not stop HAProxy: {msg}")
        
        try:
            logger.info("Running certbot renew --non-interactive")
            
            # Use async subprocess to avoid blocking event loop
            process = await asyncio.create_subprocess_exec(
                "certbot", "renew", "--non-interactive",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
                stdout_str = stdout.decode('utf-8', errors='replace') if stdout else ""
                stderr_str = stderr.decode('utf-8', errors='replace') if stderr else ""
                returncode = process.returncode
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                logger.error("Certificate renewal timed out")
                return False, "Renewal timed out (300s)", []
            
            logger.info(f"Certbot finished with exit code {returncode}")
            if stdout_str:
                logger.debug(f"Certbot stdout: {stdout_str[:500]}")
            if stderr_str:
                logger.debug(f"Certbot stderr: {stderr_str[:500]}")
            
            renewed = []
            failed = []
            
            # Update combined certs for ALL available certificates
            available_certs = self.get_available_certs()
            logger.info(f"Updating combined certificates for {len(available_certs)} domains: {available_certs}")
            
            for domain in available_certs:
                logger.info(f"Processing certificate for {domain}")
                if self._create_combined_cert(domain):
                    renewed.append(domain)
                else:
                    failed.append(domain)
                    logger.warning(f"Failed to update combined cert for {domain}")
            
            if returncode == 0:
                message = f"Renewal completed. Updated: {len(renewed)}, Failed: {len(failed)}"
                success = True
                logger.info(f"Renewal completed, updated {len(renewed)} certificates, failed {len(failed)}")
            else:
                message = stderr_str or stdout_str or "Renewal failed"
                success = False
                logger.error(f"Renewal failed: {message[:200]}")
                
        except Exception as e:
            message = str(e)
            success = False
            renewed = []
            logger.exception("Exception during certificate renewal")
        finally:
            if was_running:
                start_success, start_msg = self._temporary_start()
                if start_success:
                    logger.info("HAProxy restarted after certificate renewal")
                else:
                    logger.error(f"Failed to restart HAProxy: {start_msg}")
        
        if renewed:
            self.reload(auto_start=False)
        
        return success, message, renewed
    
    async def renew_certificate(self, domain: str) -> tuple[bool, str, Optional[str]]:
        """
        Renew specific Let's Encrypt certificate (async)
        
        Args:
            domain: Domain name to renew
        
        Returns: (success, message, output_log)
        """
        from app.services.firewall_manager import get_firewall_manager
        
        if not shutil.which("certbot"):
            return False, "certbot not installed", None
        
        # Check if certificate exists
        cert_dir = self._find_cert_dir(domain)
        if not cert_dir:
            return False, f"Certificate for {domain} not found", None
        
        logger.info(f"Starting certificate renewal for {domain} (cert_dir: {cert_dir.name})")
        
        # Open port 80 in firewall for domain validation (same as generate)
        firewall = get_firewall_manager()
        port_opened, fw_msg, fw_error = firewall.add_rule(80, "tcp")
        if port_opened:
            logger.info("Firewall: port 80 opened for certificate renewal")
        else:
            logger.warning(f"Could not open port 80: {fw_msg}")
        
        # Stop HAProxy for renewal - standalone mode needs port 80 free
        was_running = self.is_running()
        
        if was_running:
            success, msg = self._temporary_stop()
            if success:
                logger.info("Stopped HAProxy for certificate renewal")
            else:
                logger.warning(f"Could not stop HAProxy: {msg}")
        
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
            
            # Use async subprocess to avoid blocking event loop
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
                stdout_str = stdout.decode('utf-8', errors='replace') if stdout else ""
                stderr_str = stderr.decode('utf-8', errors='replace') if stderr else ""
                returncode = process.returncode
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                logger.error(f"Certificate renewal timed out for {domain}")
                return False, "Certificate renewal timed out (300s)", f"Command: {' '.join(cmd)}\n\nError: Timeout"
            
            output_log = f"Command: {' '.join(cmd)}\n\nExit code: {returncode}\n\nStdout:\n{stdout_str}\n\nStderr:\n{stderr_str}"
            
            logger.info(f"Certbot finished with exit code {returncode}")
            if stdout_str:
                logger.debug(f"Certbot stdout: {stdout_str[:500]}")
            if stderr_str:
                logger.debug(f"Certbot stderr: {stderr_str[:500]}")
            
            if returncode == 0:
                # Find the cert directory (may have suffix like -0001) and update combined cert
                renewed_cert_dir = self._find_cert_dir(domain)
                if renewed_cert_dir:
                    self._create_combined_cert(renewed_cert_dir.name)
                    message = f"Certificate for {domain} renewed successfully"
                    success = True
                    logger.info(message)
                else:
                    message = f"Certificate renewed but directory not found for {domain}"
                    success = False
                    logger.error(message)
            else:
                message = stderr_str or stdout_str or "Renewal failed"
                success = False
                logger.error(f"Certificate renewal failed for {domain}: {message[:200]}")
                
        except Exception as e:
            message = str(e)
            output_log = f"Exception: {str(e)}"
            success = False
            logger.exception(f"Exception during certificate renewal for {domain}")
        finally:
            if was_running:
                start_success, start_msg = self._temporary_start()
                if start_success:
                    logger.info("HAProxy restarted after certificate renewal")
                else:
                    logger.error(f"Failed to restart HAProxy: {start_msg}")
        
        if success:
            self.reload(auto_start=False)
        
        return success, message, output_log
    
    def update_combined_certs(self) -> list[str]:
        """Update all combined certificates from Let's Encrypt"""
        updated = []
        for domain in self.get_available_certs():
            if self._create_combined_cert(domain):
                updated.append(domain)
        
        if updated:
            # Reload only if HAProxy is running
            self.reload(auto_start=False)
        
        return updated
    
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
        
        if not domain or not re.match(r'^[a-zA-Z0-9.-]+$', domain):
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
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as tmp:
                tmp.write(cert_content)
                tmp_path = tmp.name
            
            result = subprocess.run(
                ["openssl", "x509", "-noout", "-in", tmp_path],
                capture_output=True, text=True
            )
            Path(tmp_path).unlink()
            
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
    
    # ==================== Certificate Auto-Renewal Cron ====================
    
    CRON_FILE = Path("/etc/cron.d/certbot-renew")
    RENEWAL_SCRIPT = Path("/opt/monitoring-node/renew-certs.sh")
    
    def get_cron_status(self) -> dict:
        """Get certificate auto-renewal cron status"""
        cron_exists = self.CRON_FILE.exists()
        script_exists = self.RENEWAL_SCRIPT.exists()
        
        return {
            "enabled": cron_exists and script_exists,
            "cron_file": str(self.CRON_FILE),
            "cron_exists": cron_exists,
            "script_exists": script_exists,
            "schedule": "0 3 * * * (daily at 3:00 AM)" if cron_exists else None
        }
    
    def setup_cert_renewal_cron(self) -> tuple[bool, str]:
        """Setup certificate auto-renewal cron job
        
        Creates a cron job that runs daily at 3:00 AM to renew all certificates.
        This uses certbot renew which automatically renews certs expiring in < 30 days.
        """
        try:
            # Create renewal script
            self.RENEWAL_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
            
            script_content = '''#!/bin/bash
# Auto-renewal script for Let's Encrypt certificates
# Runs on host to renew certificates for native HAProxy

# Check if certbot is available
if ! command -v certbot &> /dev/null; then
    echo "certbot not found"
    exit 1
fi

# Stop HAProxy temporarily for renewal (standalone mode needs port 80)
HAPROXY_WAS_RUNNING=false
if systemctl is-active --quiet haproxy; then
    HAPROXY_WAS_RUNNING=true
    systemctl stop haproxy
fi

# Run certbot renew
certbot renew --non-interactive --quiet

# Update combined certificates for HAProxy
for cert_dir in /etc/letsencrypt/live/*/; do
    if [ -d "$cert_dir" ]; then
        domain=$(basename "$cert_dir")
        if [ -f "$cert_dir/fullchain.pem" ] && [ -f "$cert_dir/privkey.pem" ]; then
            cat "$cert_dir/fullchain.pem" "$cert_dir/privkey.pem" > "$cert_dir/combined.pem"
            chmod 600 "$cert_dir/combined.pem"
        fi
    fi
done

# Restart HAProxy if it was running
if [ "$HAPROXY_WAS_RUNNING" = true ]; then
    systemctl start haproxy
fi

# Reload HAProxy to pick up new certificates (if running)
if systemctl is-active --quiet haproxy; then
    systemctl reload haproxy 2>/dev/null || true
fi
'''
            self.RENEWAL_SCRIPT.write_text(script_content)
            self.RENEWAL_SCRIPT.chmod(0o755)
            
            # Create cron job
            cron_content = f'''# Auto-renewal of Let's Encrypt certificates
# Runs daily at 3:00 AM
0 3 * * * root {self.RENEWAL_SCRIPT} >> /var/log/certbot-renew.log 2>&1
'''
            self.CRON_FILE.write_text(cron_content)
            self.CRON_FILE.chmod(0o644)
            
            logger.info("Certificate auto-renewal cron configured")
            return True, "Certificate auto-renewal cron enabled (daily at 3:00 AM)"
            
        except Exception as e:
            logger.error(f"Failed to setup cron: {e}")
            return False, f"Failed to setup cron: {e}"
    
    def remove_cert_renewal_cron(self) -> tuple[bool, str]:
        """Remove certificate auto-renewal cron job"""
        try:
            removed = []
            
            if self.CRON_FILE.exists():
                self.CRON_FILE.unlink()
                removed.append("cron file")
            
            if self.RENEWAL_SCRIPT.exists():
                self.RENEWAL_SCRIPT.unlink()
                removed.append("renewal script")
            
            if removed:
                logger.info(f"Removed cert renewal cron: {', '.join(removed)}")
                return True, f"Removed: {', '.join(removed)}"
            else:
                return True, "Cron was not configured"
                
        except Exception as e:
            logger.error(f"Failed to remove cron: {e}")
            return False, f"Failed to remove cron: {e}"
    
    def ensure_cert_renewal_cron(self) -> None:
        """Ensure cron is setup if there are any certificates"""
        certs = self.get_available_certs()
        status = self.get_cron_status()
        
        if certs and not status["enabled"]:
            # Have certificates but no cron - set it up
            success, msg = self.setup_cert_renewal_cron()
            if success:
                logger.info("Auto-configured cert renewal cron")


# Singleton instance
_manager: Optional[HAProxyManager] = None


def get_haproxy_manager() -> HAProxyManager:
    """Get or create HAProxy manager instance"""
    global _manager
    if _manager is None:
        _manager = HAProxyManager()
    return _manager
