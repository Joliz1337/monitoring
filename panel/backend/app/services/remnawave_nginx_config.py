"""Генератор nginx.conf для Remnawave-нод.

Вся генерация — на панели, нода только применяет готовый конфиг.
Профиль хранит шаблон полного nginx.conf с плейсхолдером {{DOMAIN}}
(server_name и пути сертификатов); домен подставляется per-node при синке.

Схемы передачи реального IP клиента в Xray (gRPC за nginx):
- напрямую: `grpc_set_header X-Forwarded-For $remote_addr` (перезапись,
  никогда $proxy_add_x_forwarded_for — иначе клиент подделает IP);
- CDN: geo по доверенным диапазонам + цепочка map
  (CF-Connecting-IP → X-Real-IP → первый из XFF) → $client_ip;
- HAProxy: отдельный listen-порт с proxy_protocol + set_real_ip_from;
- универсальная: композиция обеих, всё стекается в $client_ip.

Правила (location-блоки) живут между маркерами и парсятся обратно из
конфига; опции схемы (CDN, PP и т.д.) хранятся в JSON-колонке профиля
и из конфига не парсятся.
"""

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Optional

DOMAIN_PLACEHOLDER = "{{DOMAIN}}"
LOCATIONS_START_MARKER = "# === LOCATIONS START ==="
LOCATIONS_END_MARKER = "# === LOCATIONS END ==="

# Строки с этим маркером нода пересчитывает под свой хост при применении
# (потолок дескрипторов контейнера и RAM у нод разные). Значения ниже —
# безопасный минимум, который работает даже на самом маленьком сервере
# и на нодах со старым агентом, который подстановки ещё не умеет.
AUTO_MARKER = "# auto: node"

# Публикуемые диапазоны Cloudflare (cloudflare.com/ips) — для кнопки
# «Cloudflare по умолчанию» на фронте и подсказки в API
CLOUDFLARE_RANGES = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
]

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_SERVICE_PATH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PROXY_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")
_TARGET_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(?::\d{1,5})?(?:/[^\s]*)?$")
_ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9.{}_/-]+$")
_SERVER_NAME_RE = re.compile(r"^\s*server_name\s+([^\s;]+)", re.MULTILINE)

_GRPC_BLOCK_RE = re.compile(
    r"# rule: (?P<name>\S+) type=grpc\n"
    r"\s*location \^~ /(?P<service_path>[^\s{]+) \{\n"
    r"(?P<body>.*?)\n\s*\}",
    re.DOTALL,
)
_PROXY_BLOCK_RE = re.compile(
    r"# rule: (?P<name>\S+) type=proxy\n"
    r"\s*location (?P<path>[^\s{]+) \{\n"
    r"(?P<body>.*?)\n\s*\}",
    re.DOTALL,
)
_GRPC_PASS_RE = re.compile(r"grpc_pass\s+grpc://127\.0\.0\.1:(\d+)\s*;")
_PROXY_PASS_RE = re.compile(r"proxy_pass\s+(\S+?)\s*;")


class RuleValidationError(ValueError):
    pass


class OptionsValidationError(ValueError):
    pass


class MissingMarkersError(ValueError):
    """В конфиге нет маркеров LOCATIONS — структурные правила недоступны."""


@dataclass
class GrpcRule:
    """gRPC-локация → Xray-инбаунд на 127.0.0.1 (serviceName = путь)."""
    name: str
    service_path: str
    port: int
    rule_type: str = "grpc"


@dataclass
class ProxyRule:
    """Обычное проксирование (fallback-сайт, панель и т.п.)."""
    name: str
    path: str
    target_url: str
    rule_type: str = "proxy"


@dataclass
class ProfileOptions:
    cdn_enabled: bool = False
    cdn_ranges: list[str] = field(default_factory=list)
    proxy_protocol_enabled: bool = False
    proxy_protocol_port: int = 8449
    haproxy_ip: str = ""
    http_redirect_enabled: bool = True
    acme_enabled: bool = True
    reject_default_server: bool = False
    ssl_cert_path: str = "/etc/letsencrypt/live/{{DOMAIN}}/fullchain.pem"
    ssl_key_path: str = "/etc/letsencrypt/live/{{DOMAIN}}/privkey.pem"
    # Куда проксировать «мусорный» трафик (всё, что не попало в правила)
    # и ошибки Xray (502/503/504) — снаружи сервер выглядит как обычный сайт
    fallback_url: str = ""

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ProfileOptions":
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        return {
            "cdn_enabled": self.cdn_enabled,
            "cdn_ranges": self.cdn_ranges,
            "proxy_protocol_enabled": self.proxy_protocol_enabled,
            "proxy_protocol_port": self.proxy_protocol_port,
            "haproxy_ip": self.haproxy_ip,
            "http_redirect_enabled": self.http_redirect_enabled,
            "acme_enabled": self.acme_enabled,
            "reject_default_server": self.reject_default_server,
            "ssl_cert_path": self.ssl_cert_path,
            "ssl_key_path": self.ssl_key_path,
            "fallback_url": self.fallback_url,
        }

    @property
    def client_ip_var(self) -> str:
        # При PP без CDN realip уже переписал $remote_addr адресом из
        # PROXY-заголовка, отдельная переменная не нужна
        return "$client_ip" if self.cdn_enabled else "$remote_addr"


def validate_rule(rule) -> None:
    if not _NAME_RE.match(rule.name):
        raise RuleValidationError(f"Недопустимое имя правила: {rule.name!r}")
    if isinstance(rule, GrpcRule):
        if not _SERVICE_PATH_RE.match(rule.service_path):
            raise RuleValidationError(f"Недопустимый serviceName: {rule.service_path!r}")
        if not 1 <= rule.port <= 65535:
            raise RuleValidationError(f"Недопустимый порт: {rule.port}")
    elif isinstance(rule, ProxyRule):
        if not _PROXY_PATH_RE.match(rule.path):
            raise RuleValidationError(f"Недопустимый путь локации: {rule.path!r}")
        if not _TARGET_URL_RE.match(rule.target_url):
            raise RuleValidationError(f"Недопустимый target URL: {rule.target_url!r}")
    else:
        raise RuleValidationError(f"Неизвестный тип правила: {type(rule).__name__}")


def validate_rules(grpc_rules: list[GrpcRule], proxy_rules: list[ProxyRule]) -> None:
    for rule in [*grpc_rules, *proxy_rules]:
        validate_rule(rule)
    names = [r.name for r in grpc_rules] + [r.name for r in proxy_rules]
    if len(names) != len(set(names)):
        raise RuleValidationError("Имена правил должны быть уникальны")
    paths = [f"/{r.service_path}" for r in grpc_rules] + [r.path for r in proxy_rules]
    if len(paths) != len(set(paths)):
        raise RuleValidationError("Пути локаций должны быть уникальны")


def validate_options(options: ProfileOptions) -> None:
    if options.cdn_enabled:
        if not options.cdn_ranges:
            raise OptionsValidationError("CDN включён, но список доверенных диапазонов пуст")
        for raw in options.cdn_ranges:
            try:
                ipaddress.ip_network(raw, strict=False)
            except ValueError:
                raise OptionsValidationError(f"Некорректный CIDR: {raw!r}")
    if options.proxy_protocol_enabled:
        if not 1 <= options.proxy_protocol_port <= 65535 or options.proxy_protocol_port in (80, 443):
            raise OptionsValidationError(f"Недопустимый PP-порт: {options.proxy_protocol_port}")
        # IP HAProxy необязателен: пусто = принимать PROXY-заголовок от всех
        # (0.0.0.0/0), тогда PP-порт защищается только файрволом
        if options.haproxy_ip:
            try:
                ipaddress.ip_network(options.haproxy_ip, strict=False)
            except ValueError:
                raise OptionsValidationError(f"Некорректный IP/CIDR HAProxy: {options.haproxy_ip!r}")
    if options.fallback_url and not _TARGET_URL_RE.match(options.fallback_url):
        raise OptionsValidationError(f"Некорректный fallback URL: {options.fallback_url!r}")
    for path in (options.ssl_cert_path, options.ssl_key_path):
        if not _ABS_PATH_RE.match(path or ""):
            raise OptionsValidationError(f"Некорректный путь сертификата: {path!r}")


def _grpc_block(rule: GrpcRule, ip_var: str, has_fallback: bool) -> str:
    # Упавший Xray отдаёт клиенту сайт вместо 502 — маскировка не рвётся
    error_page = "            error_page 502 503 504 = @fallback;\n" if has_fallback else ""
    return f"""        # rule: {rule.name} type=grpc
        location ^~ /{rule.service_path} {{
            grpc_pass grpc://127.0.0.1:{rule.port};
            grpc_set_header Host $host;
            grpc_set_header X-Forwarded-For {ip_var};
            grpc_read_timeout 1h;
            grpc_send_timeout 1h;
{error_page}            access_log off;
        }}"""


def _proxy_headers(ip_var: str, indent: str = "            ", with_drop: bool = False) -> str:
    """HTTP/1.1 + проброс WebSocket: на HTTP/1.0 (дефолт proxy_pass) часть
    сайтов отвечает иначе, keepalive не работает, а Upgrade не проходит.

    proxy_pass_header Server — иначе nginx подменяет Server апстрима своим,
    и ответ через ноду отличался бы от прямого обращения к сайту-заглушке.
    """
    lines = [
        "proxy_http_version 1.1;",
        "proxy_set_header Host $host;",
        f"proxy_set_header X-Real-IP {ip_var};",
        f"proxy_set_header X-Forwarded-For {ip_var};",
        "proxy_set_header X-Forwarded-Proto $scheme;",
        "proxy_set_header Upgrade $http_upgrade;",
        "proxy_set_header Connection $connection_upgrade;",
        "proxy_pass_header Server;",
    ]
    if with_drop:
        # Заглушка недоступна — рвём соединение вместо страницы ошибки nginx:
        # обычные сайты 502 от nginx не отдают, это выдало бы прокси
        lines.append("error_page 502 503 504 = @drop;")
    return "\n".join(f"{indent}{line}" for line in lines)


def _proxy_block(rule: ProxyRule, ip_var: str) -> str:
    return f"""        # rule: {rule.name} type=proxy
        location {rule.path} {{
            proxy_pass {rule.target_url};
{_proxy_headers(ip_var)}
        }}"""


def _render_locations(grpc_rules: list[GrpcRule], proxy_rules: list[ProxyRule],
                      options: "ProfileOptions") -> str:
    ip_var = options.client_ip_var
    has_fallback = bool(options.fallback_url)
    blocks = [_grpc_block(r, ip_var, has_fallback) for r in grpc_rules]
    blocks += [_proxy_block(r, ip_var) for r in proxy_rules]
    return "\n\n".join(blocks)


def _fallback_locations(options: "ProfileOptions") -> str:
    """Живут вне маркеров: весь не попавший в правила трафик и ошибки Xray
    уходят на обычный сайт."""
    headers = _proxy_headers(options.client_ip_var, with_drop=True)
    return f"""
        location / {{
            proxy_pass {options.fallback_url};
{headers}
        }}

        location @fallback {{
            proxy_pass {options.fallback_url};
{headers}
        }}

        location @drop {{
            return 444;
        }}"""


def _realip_maps(options: ProfileOptions) -> str:
    ranges = "\n".join(f"        {r} 1;" for r in options.cdn_ranges)
    return f"""    # Доверенные диапазоны CDN: заголовок с IP клиента принимается
    # только от них, иначе — честный адрес соединения
    geo $remote_addr $from_edge {{
        default 0;
{ranges}
    }}
    map $http_x_forwarded_for $ip_from_xff {{
        default          "";
        "~^(?<f>[^, ]+)" $f;
    }}
    map $http_x_real_ip $ip_from_hdr {{
        ""      $ip_from_xff;
        default $http_x_real_ip;
    }}
    map $http_cf_connecting_ip $edge_ip {{
        ""      $ip_from_hdr;
        default $http_cf_connecting_ip;
    }}
    map "$from_edge:$edge_ip" $client_ip {{
        "~^1:(?<ip>.+)$" $ip;
        default          $remote_addr;
    }}
"""


def generate_full_config(options: ProfileOptions, grpc_rules: list[GrpcRule],
                         proxy_rules: list[ProxyRule]) -> str:
    validate_options(options)
    validate_rules(grpc_rules, proxy_rules)
    if options.fallback_url and any(r.path == "/" for r in proxy_rules):
        raise RuleValidationError(
            "location / уже занята fallback-проксированием из опций — "
            "правило с путём / не нужно"
        )

    http_parts: list[str] = []
    if options.cdn_enabled:
        http_parts.append(_realip_maps(options))

    if options.http_redirect_enabled:
        acme = ("        location /.well-known/acme-challenge/ { root /var/www/html; }\n"
                if options.acme_enabled else "")
        http_parts.append(f"""    server {{
        listen 80;
        server_name {DOMAIN_PLACEHOLDER};
{acme}        location / {{ return 301 https://$host$request_uri; }}
    }}
""")

    if options.reject_default_server:
        http_parts.append("""    server {
        listen 443 ssl default_server;
        ssl_reject_handshake on;
    }
""")

    pp_listen = (f"        listen {options.proxy_protocol_port} ssl proxy_protocol;\n"
                 if options.proxy_protocol_enabled else "")
    pp_realip = (f"        set_real_ip_from {options.haproxy_ip or '0.0.0.0/0'};\n"
                 f"        real_ip_header proxy_protocol;\n\n"
                 if options.proxy_protocol_enabled else "")
    locations = _render_locations(grpc_rules, proxy_rules, options)
    fallback = _fallback_locations(options) if options.fallback_url else ""

    # Своих заголовков в ответ не добавляем: клиент должен получать ровно то,
    # что отдала бы заглушка при прямом обращении — любой лишний или
    # продублированный заголовок выдаёт, что перед сайтом стоит прокси
    http_parts.append(f"""    server {{
        listen 443 ssl;
{pp_listen}        http2 on;
        server_name {DOMAIN_PLACEHOLDER};

        ssl_certificate     {options.ssl_cert_path};
        ssl_certificate_key {options.ssl_key_path};

{pp_realip}        {LOCATIONS_START_MARKER}
{locations}
        {LOCATIONS_END_MARKER}{fallback}
    }}
""")

    http_body = "\n".join(http_parts)
    return f"""# Managed by monitoring panel (Remnawave nginx profile)
worker_processes auto;
worker_rlimit_nofile 65536;  {AUTO_MARKER}
error_log /var/log/nginx/error.log warn;
pid /var/run/nginx.pid;

events {{
    worker_connections 8192;  {AUTO_MARKER}
    multi_accept on;
    use epoll;
}}

http {{
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    server_tokens off;
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 75s;
    keepalive_requests 10000;
    reset_timedout_connection on;

    # gRPC-транспорт Xray — это один бесконечный поток в теле запроса.
    # Любой ненулевой лимит рано или поздно обрывает соединение
    # («client intended to send too large chunked body»).
    client_max_body_size 0;

    map $http_upgrade $connection_upgrade {{
        default upgrade;
        ""      close;
    }}

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ecdh_curve X25519:prime256v1:secp384r1;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:20m;  {AUTO_MARKER}
    ssl_session_timeout 1d;
    ssl_session_tickets off;

{http_body}}}
"""


def splice_rules(config: str, grpc_rules: list[GrpcRule], proxy_rules: list[ProxyRule],
                 options: ProfileOptions) -> str:
    """Заменяет секцию между маркерами, сохраняя ручные правки вне её."""
    validate_rules(grpc_rules, proxy_rules)
    start = config.find(LOCATIONS_START_MARKER)
    end = config.find(LOCATIONS_END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise MissingMarkersError(
            "В конфиге нет маркеров LOCATIONS — воспользуйтесь «Вставить шаблон»"
        )
    locations = _render_locations(grpc_rules, proxy_rules, options)
    head = config[: start + len(LOCATIONS_START_MARKER)]
    tail = config[end:]
    return f"{head}\n{locations}\n        {tail}"


def parse_rules_from_config(config: str) -> tuple[list[GrpcRule], list[ProxyRule]]:
    """Обратный парсер правил из секции LOCATIONS (round-trip c генерацией)."""
    start = config.find(LOCATIONS_START_MARKER)
    end = config.find(LOCATIONS_END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise MissingMarkersError(
            "В конфиге нет маркеров LOCATIONS — структурные правила недоступны"
        )
    section = config[start:end]

    grpc_rules = []
    for match in _GRPC_BLOCK_RE.finditer(section):
        pass_match = _GRPC_PASS_RE.search(match.group("body"))
        if not pass_match:
            continue
        grpc_rules.append(GrpcRule(
            name=match.group("name"),
            service_path=match.group("service_path"),
            port=int(pass_match.group(1)),
        ))

    proxy_rules = []
    for match in _PROXY_BLOCK_RE.finditer(section):
        pass_match = _PROXY_PASS_RE.search(match.group("body"))
        if not pass_match:
            continue
        proxy_rules.append(ProxyRule(
            name=match.group("name"),
            path=match.group("path"),
            target_url=pass_match.group(1),
        ))

    return grpc_rules, proxy_rules


def has_markers(config: str) -> bool:
    start = config.find(LOCATIONS_START_MARKER)
    end = config.find(LOCATIONS_END_MARKER)
    return start != -1 and end != -1 and end > start


def render_for_server(template: str, domain: str) -> str:
    return template.replace(DOMAIN_PLACEHOLDER, domain)


def detect_domain(content: str) -> Optional[str]:
    """Первый осмысленный server_name из конфига (для импорта с ноды)."""
    for match in _SERVER_NAME_RE.finditer(content):
        name = match.group(1)
        if name not in ("_", "localhost", DOMAIN_PLACEHOLDER) and "." in name:
            return name
    return None


def replace_domain_with_placeholder(content: str, domain: str) -> str:
    return content.replace(domain, DOMAIN_PLACEHOLDER)
