"""HAProxy configuration generator for the panel.
All config generation logic is here - node just applies the config.
"""

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Optional


RULES_START_MARKER = "# === RULES START ==="
RULES_END_MARKER = "# === RULES END ==="

VALID_ALGORITHMS = (
    "roundrobin", "static-rr", "leastconn", "source", "uri",
    "url_param", "hdr", "random", "first", "rdp-cookie",
)


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
    use_wildcard: bool = False
    is_balancer: bool = False
    servers: list[BackendServer] = field(default_factory=list)
    balancer_options: Optional[BalancerOptions] = None


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
    
    @staticmethod
    def _is_domain(target: str) -> bool:
        try:
            ipaddress.ip_address(target)
            return False
        except ValueError:
            return True
    
    def _build_server_line(self, srv: BackendServer, opts: BalancerOptions | None = None,
                           rule_type: str = "tcp") -> str:
        line = f"    server {srv.name} {srv.address}:{srv.port}"
        if srv.weight != 1:
            line += f" weight {srv.weight}"
        if srv.maxconn:
            line += f" maxconn {srv.maxconn}"
        if self._is_domain(srv.address):
            line += " resolvers mydns resolve-prefer ipv4 init-addr none"
        if rule_type == "https" and srv.send_proxy is False and srv.send_proxy_v2 is False:
            pass  # без proxy protocol по умолчанию для https
        if srv.send_proxy and not srv.send_proxy_v2:
            line += " send-proxy"
        if srv.send_proxy_v2:
            line += " send-proxy-v2"
        if srv.check:
            line += f" check inter {srv.inter} fall {srv.fall} rise {srv.rise}"
        if srv.backup:
            line += " backup"
        if srv.slowstart:
            line += f" slowstart {srv.slowstart}"
        line += " on-marked-down shutdown-sessions"
        if opts and opts.sticky_type == "cookie":
            line += f" cookie {srv.name}"
        if srv.disabled:
            line += " disabled"
        return line

    def _generate_balancer_block(self, rule: HAProxyRule, certs_base_path: str) -> str:
        opts = rule.balancer_options or BalancerOptions()
        mode = "tcp" if rule.rule_type == "tcp" else "http"
        frontend_name = f"{rule.rule_type}_{rule.name}"
        backend_name = f"backend_{rule.rule_type}_{rule.name}"

        # Frontend
        if rule.rule_type == "tcp":
            frontend = f"""
frontend {frontend_name}
    bind *:{rule.listen_port}
    mode tcp
    default_backend {backend_name}
"""
        else:
            cert_domain = rule.cert_domain
            if rule.use_wildcard and cert_domain:
                parts = cert_domain.split('.')
                if len(parts) > 2:
                    cert_domain = '.'.join(parts[1:])
            cert_path = f"{certs_base_path}/{cert_domain}/combined.pem"
            frontend = f"""
frontend {frontend_name}
    bind *:{rule.listen_port} ssl crt {cert_path}
    mode http
    default_backend {backend_name}
"""

        # Backend
        lines = [f"backend {backend_name}"]
        lines.append(f"    mode {mode}")

        # Алгоритм балансировки
        alg = opts.algorithm or "roundrobin"
        if alg == "random" and opts.algorithm_param:
            lines.append(f"    balance random({opts.algorithm_param})")
        elif alg in ("url_param", "hdr", "rdp-cookie") and opts.algorithm_param:
            lines.append(f"    balance {alg}({opts.algorithm_param})")
        else:
            lines.append(f"    balance {alg}")

        if opts.hash_type and alg in ("source", "uri", "url_param", "hdr"):
            lines.append(f"    hash-type {opts.hash_type}")

        # Health checks
        if opts.health_check_type == "tcp-check":
            lines.append("    option tcp-check")
        elif opts.health_check_type == "httpchk":
            method = opts.httpchk_method or "GET"
            uri = opts.httpchk_uri or "/"
            lines.append(f"    option httpchk {method} {uri}")
            if opts.httpchk_expect:
                lines.append(f"    http-check expect {opts.httpchk_expect}")

        # Sticky sessions
        if opts.sticky_type == "cookie":
            cookie_name = opts.cookie_name or "SERVERID"
            cookie_opts = opts.cookie_options or "insert indirect nocache"
            lines.append(f"    cookie {cookie_name} {cookie_opts}")
        elif opts.sticky_type == "stick-table":
            st_type = opts.stick_table_type or "ip"
            st_size = opts.stick_table_size or "200k"
            st_expire = opts.stick_table_expire or "30m"
            lines.append(f"    stick-table type {st_type} size {st_size} expire {st_expire}")
            lines.append("    stick on src")

        # Надёжность
        if opts.redispatch:
            lines.append("    option redispatch")
        if opts.retries != 3:
            lines.append(f"    retries {opts.retries}")
        if opts.allbackups:
            lines.append("    option allbackups")
        if opts.fullconn:
            lines.append(f"    fullconn {opts.fullconn}")
        if opts.timeout_queue:
            lines.append(f"    timeout queue {opts.timeout_queue}")

        # HTTP заголовки для HTTPS
        if rule.rule_type == "https":
            lines.append(f"    http-request set-header X-Forwarded-Proto https")
            lines.append(f"    http-request set-header X-Forwarded-For %[src]")

        # Серверы
        for srv in rule.servers:
            lines.append(self._build_server_line(srv, opts, rule.rule_type))

        return frontend + "\n".join(lines) + "\n"

    def generate_rule_block(self, rule: HAProxyRule, certs_base_path: str = "/etc/letsencrypt/live") -> str:
        """Generate frontend/backend block for a rule"""
        if rule.is_balancer and rule.servers:
            return self._generate_balancer_block(rule, certs_base_path)

        frontend_name = f"{rule.rule_type}_{rule.name}"
        backend_name = f"backend_{rule.rule_type}_{rule.name}"
        resolver_opts = " resolvers mydns resolve-prefer ipv4 init-addr none" if self._is_domain(rule.target_ip) else ""

        if rule.rule_type == "tcp":
            server_opts = ""
            if rule.send_proxy:
                server_opts += " send-proxy-v2"
            server_opts += " check inter 5s fall 3 rise 2"

            return f"""
frontend {frontend_name}
    bind *:{rule.listen_port}
    mode tcp
    default_backend {backend_name}

backend {backend_name}
    mode tcp
    option tcp-check
    server srv1 {rule.target_ip}:{rule.target_port}{resolver_opts}{server_opts}
"""
        else:
            cert_domain = rule.cert_domain
            if rule.use_wildcard and cert_domain:
                parts = cert_domain.split('.')
                if len(parts) > 2:
                    cert_domain = '.'.join(parts[1:])
            cert_path = f"{certs_base_path}/{cert_domain}/combined.pem"
            server_line = f"server srv1 {rule.target_ip}:{rule.target_port}"
            if rule.target_ssl:
                server_line += f" ssl verify none sni str({rule.target_ip})"
            server_line += resolver_opts

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

        if rule.rule_type not in ('tcp', 'https'):
            return False, "Invalid rule type (tcp or https)"

        if rule.rule_type == 'https' and not rule.cert_domain:
            return False, "Certificate domain required for HTTPS rules"

        if rule.is_balancer:
            if not rule.servers:
                return False, "At least one server required for load balancer"
            for srv in rule.servers:
                if not srv.address:
                    return False, f"Server '{srv.name}': address is required"
                if not 1 <= srv.port <= 65535:
                    return False, f"Server '{srv.name}': invalid port (1-65535)"
                if not 1 <= srv.weight <= 256:
                    return False, f"Server '{srv.name}': invalid weight (1-256)"
            opts = rule.balancer_options
            if opts:
                alg = opts.algorithm or "roundrobin"
                if alg not in VALID_ALGORITHMS:
                    return False, f"Invalid balance algorithm: {alg}"
        else:
            if not 1 <= rule.target_port <= 65535:
                return False, "Invalid target port (1-65535)"

        return True, "Valid"
    
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

        # balance algorithm
        bal_m = re.search(r'balance\s+(\S+?)(?:\((\S+?)\))?(?:\s|$)', block)
        if bal_m:
            opts.algorithm = bal_m.group(1)
            opts.algorithm_param = bal_m.group(2)

        ht_m = re.search(r'hash-type\s+(\S+)', block)
        if ht_m:
            opts.hash_type = ht_m.group(1)

        # health checks
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

        # sticky sessions
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

        # reliability
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

    def parse_rules_from_config(self, config: str) -> list[HAProxyRule]:
        """Parse rules from existing config"""
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
                "cert_domain": cert_match.group(1) if cert_match else None,
            }

        for match in backend_pattern.finditer(config):
            rule_type, name, block = match.groups()
            if name not in frontends:
                continue

            fe = frontends[name]
            server_lines = re.findall(r'^\s*server\s+.+', block, re.MULTILINE)
            has_balance = bool(re.search(r'^\s*balance\s+', block, re.MULTILINE))

            if len(server_lines) > 1 or has_balance:
                # Балансировщик
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
                    is_balancer=True, servers=servers, balancer_options=balancer_options,
                ))
            else:
                # Одиночный сервер (текущая логика)
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
                    ))

        return rules


def get_config_generator(cpu_cores: int = 1, ram_mb: int = 1024, ulimit: int = 1024) -> HAProxyConfigGenerator:
    """Create a config generator with system params"""
    return HAProxyConfigGenerator(cpu_cores, ram_mb, ulimit)
