"""HAProxy configuration generator for the panel.
All config generation logic is here - node just applies the config.
"""

import re
from dataclasses import dataclass
from typing import Optional


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
    target_ssl: bool = False  # Use SSL when connecting to target server


class HAProxyConfigGenerator:
    """Generates HAProxy configuration on the panel side"""
    
    def __init__(self, cpu_cores: int = 1, ram_mb: int = 1024, ulimit: int = 1024):
        # Kept for API compatibility but not used
        self.cpu_cores = cpu_cores
        self.ram_mb = ram_mb
        self.ulimit = ulimit
    
    def generate_base_config(self) -> str:
        """Generate base HAProxy config for high-speed TCP proxying"""
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
    
    def generate_rule_block(self, rule: HAProxyRule, certs_base_path: str = "/etc/letsencrypt/live") -> str:
        """Generate frontend/backend block for a rule"""
        frontend_name = f"{rule.rule_type}_{rule.name}"
        backend_name = f"backend_{rule.rule_type}_{rule.name}"
        
        if rule.rule_type == "tcp":
            return f"""
frontend {frontend_name}
    bind *:{rule.listen_port}
    mode tcp
    default_backend {backend_name}

backend {backend_name}
    mode tcp
    server srv1 {rule.target_ip}:{rule.target_port}
"""
        else:
            # HTTPS rule - build server line with optional SSL to target
            cert_path = f"{certs_base_path}/{rule.cert_domain}/combined.pem"
            server_line = f"server srv1 {rule.target_ip}:{rule.target_port}"
            if rule.target_ssl:
                server_line += f" ssl verify none sni str({rule.target_ip})"
            
            return f"""
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
    
    def generate_full_config(self, rules: list[HAProxyRule], certs_base_path: str = "/etc/letsencrypt/live") -> str:
        """Generate full HAProxy config with all rules"""
        config = self.generate_base_config()
        
        rules_content = ""
        for rule in rules:
            rules_content += self.generate_rule_block(rule, certs_base_path)
        
        if rules_content:
            config = config.replace(
                RULES_END_MARKER,
                rules_content.rstrip() + '\n' + RULES_END_MARKER
            )
        
        return config
    
    def validate_rule(self, rule: HAProxyRule) -> tuple[bool, str]:
        """Validate a rule before adding"""
        if not re.match(r'^[a-zA-Z0-9_-]+$', rule.name):
            return False, "Invalid rule name (use a-z, A-Z, 0-9, -, _)"
        
        if not 1 <= rule.listen_port <= 65535:
            return False, "Invalid listen port (1-65535)"
        
        if not 1 <= rule.target_port <= 65535:
            return False, "Invalid target port (1-65535)"
        
        if rule.rule_type not in ('tcp', 'https'):
            return False, "Invalid rule type (tcp or https)"
        
        if rule.rule_type == 'https' and not rule.cert_domain:
            return False, "Certificate domain required for HTTPS rules"
        
        return True, "Valid"
    
    def parse_rules_from_config(self, config: str) -> list[HAProxyRule]:
        """Parse rules from existing config (for migration/sync)"""
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
        for match in frontend_pattern.finditer(config):
            rule_type, name, block = match.groups()
            port_match = re.search(r'bind\s+\*:(\d+)', block)
            cert_match = re.search(r'ssl\s+crt\s+/etc/letsencrypt/live/([^/]+)/combined\.pem', block) if rule_type == "https" else None
            
            frontends[name] = {
                "type": rule_type,
                "port": int(port_match.group(1)) if port_match else 0,
                "cert_domain": cert_match.group(1) if cert_match else None
            }
        
        for match in backend_pattern.finditer(config):
            rule_type, name, block = match.groups()
            if name in frontends:
                server_match = re.search(r'server\s+\S+\s+(\S+):(\d+)', block)
                if server_match:
                    # Check if target_ssl is enabled (ssl verify in server line)
                    target_ssl = bool(re.search(r'server\s+\S+\s+\S+:\d+\s+ssl', block))
                    
                    rules.append(HAProxyRule(
                        name=name,
                        rule_type=frontends[name]["type"],
                        listen_port=frontends[name]["port"],
                        target_ip=server_match.group(1),
                        target_port=int(server_match.group(2)),
                        cert_domain=frontends[name]["cert_domain"],
                        target_ssl=target_ssl
                    ))
        
        return rules


def get_config_generator(cpu_cores: int = 1, ram_mb: int = 1024, ulimit: int = 1024) -> HAProxyConfigGenerator:
    """Create a config generator with system params"""
    return HAProxyConfigGenerator(cpu_cores, ram_mb, ulimit)
