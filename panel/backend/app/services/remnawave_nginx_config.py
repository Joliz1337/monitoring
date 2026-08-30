"""Генератор nginx.conf для Remnawave-нод.

Вся генерация — на панели, нода только применяет готовый конфиг.
Профиль хранит шаблон полного nginx.conf с плейсхолдером {{DOMAIN}}
(server_name и пути сертификатов); домен подставляется per-node при синке.

Схемы передачи реального IP клиента в Xray (Xray за nginx):
- напрямую: `grpc_set_header X-Forwarded-For $remote_addr` (перезапись,
  никогда $proxy_add_x_forwarded_for — иначе клиент подделает IP);
- CDN: geo по доверенным диапазонам + цепочка map
  (CF-Connecting-IP → X-Real-IP → первый из XFF) → $client_ip;
- HAProxy: отдельный listen-порт с proxy_protocol + set_real_ip_from;
- универсальная: композиция обеих, всё стекается в $client_ip.

Правила (location-блоки) живут между маркерами и парсятся обратно из
конфига; опции схемы (CDN, PP и т.д.) хранятся в JSON-колонке профиля
и из конфига не парсятся.

Три типа правил: gRPC-инбаунд Xray, XHTTP-инбаунд Xray и обычное
проксирование. XHTTP разводит трафик по Content-Type внутри одной
локации — его режимы ходят по-разному (см. `_xhttp_block`).

Политика ошибок: наружу уходит либо ответ сайта-заглушки, либо ничего.
Всё, что подделать нельзя, — страницы ошибок самого nginx — превращается
в обрыв соединения (444), а всё, что заглушка может отдать за настоящий
сайт (чужой путь, не-gRPC запрос по gRPC-локации, упавший Xray), уходит
на неё проксированием.

Проксирование заглушки по имени: если target цели — домен без пути (типичный
случай маскировочного сайта за CDN), proxy_pass идёт через переменную
`$rw_upstream`, чтобы http-resolver перечитывал DNS и смена IP не убивала
маскировку до перезапуска; для https-доменов добавляется SNI
(proxy_ssl_server_name/proxy_ssl_name с именем цели, не $host). Для целей-IP
и доменов с путём (где переменная сломала бы подстановку URI) proxy_pass
остаётся литеральным — поведение не меняется.

Осознанно не добавляем:
- limit_conn/limit_req — отдача 429 наружу сама по себе подпись прокси;
  скорость на клиентском порту не режем;
- HTTP/3 — только http2 on;
- ssl_ciphers не трогаем — набор фиксирован ниже.
"""

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

DOMAIN_PLACEHOLDER = "{{DOMAIN}}"
LOCATIONS_START_MARKER = "# === LOCATIONS START ==="
LOCATIONS_END_MARKER = "# === LOCATIONS END ==="
# Секция в http-контексте для keepalive-пулов XHTTP; в конфигах, собранных
# до её появления, отсутствует — тогда XHTTP-правило проксирует напрямую
UPSTREAMS_START_MARKER = "# === UPSTREAMS START ==="
UPSTREAMS_END_MARKER = "# === UPSTREAMS END ==="

# Строки с этим маркером нода пересчитывает под свой хост при применении
# (потолок дескрипторов контейнера и RAM у нод разные). Значения ниже —
# безопасный минимум, который работает даже на самом маленьком сервере
# и на нодах со старым агентом, который подстановки ещё не умеет.
AUTO_MARKER = "# auto: node"

# Коды, которые nginx генерирует сам (битый запрос, HTTP на TLS-порт, упавший
# апстрим и т.п.). Отдать их клиенту — значит показать страницу с подписью
# nginx там, где обычный сайт отдал бы свою; воспроизвести чужую страницу
# ошибки нечем, поэтому соединение рвётся.
# Ошибки самого сайта-заглушки при этом доходят до клиента нетронутыми:
# proxy_intercept_errors по умолчанию выключен, и его нельзя включать —
# иначе 404 заглушки превратился бы в обрыв и маскировка сломалась бы.
NGINX_OWN_ERROR_CODES = (
    "400 403 404 405 408 411 413 414 416 421 429 "
    "494 495 496 497 500 501 502 503 504 505"
)

# Внутренний код для «пришёл не gRPC» — error_page уводит такой запрос
# на заглушку, наружу он никогда не выходит
NOT_GRPC_CODE = 418

# Коды, которыми Xray на XHTTP-пути отвечает своему же клиенту: рассинхрон
# сессии, коллизия сессий, слишком большой пост. Подменять их заглушкой
# нельзя — клиент получил бы 200 с HTML вместо причины, и диагностика
# шла бы вслепую. Пробер по угаданному пути получает 404/405, а не эти коды,
# так что маскировка не страдает.
XRAY_CLIENT_ERROR_CODES = ("400", "409", "413")
# Что получает пробер на угаданном пути и что даёт упавший Xray — это
# уходит на заглушку: путь неотличим от несуществующей страницы сайта
XHTTP_FALLBACK_CODES = "404 405 502 503 504"
# Остальные собственные ошибки nginx на XHTTP-пути — обрыв соединения
XHTTP_DROP_CODES = " ".join(
    code for code in NGINX_OWN_ERROR_CODES.split()
    if code not in XRAY_CLIENT_ERROR_CODES and code not in XHTTP_FALLBACK_CODES.split()
)

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
# Путь XHTTP-инбаунда — многосегментный (`/api/v2/upload/<hex>`), в отличие
# от serviceName gRPC; пустой хвост запрещён, иначе локация перехватила бы всё
_XHTTP_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,128}$")
_PROXY_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")
_TARGET_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(?::\d{1,5})?(?:/[^\s]*)?$")
_ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9.{}_/-]+$")
# Формат so_keepalive у nginx: idle:intvl:cnt (число с суффиксом s/m, число с
# суффиксом s/m, целое); cnt ограничен 1..100 отдельно
_SO_KEEPALIVE_RE = re.compile(r"^(\d{1,4}[sm]):(\d{1,4}[sm]):(\d{1,3})$")
_SERVER_NAME_RE = re.compile(r"^\s*server_name\s+([^\s;]+)", re.MULTILINE)
DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$", re.IGNORECASE
)

_GRPC_BLOCK_RE = re.compile(
    r"# rule: (?P<name>\S+) type=grpc\n"
    r"\s*location \^~ /(?P<service_path>[^\s{]+) \{\n"
    r"(?P<body>.*?)\n\s*\}",
    re.DOTALL,
)
_XHTTP_BLOCK_RE = re.compile(
    r"# rule: (?P<name>\S+) type=xhttp\n"
    r"\s*location \^~ (?P<path>[^\s{]+) \{\n"
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
# Доменная цель проксируется через set $rw_upstream <url>; исходный target
# восстанавливается отсюда, а не из proxy_pass (там переменная)
_PROXY_SET_RE = re.compile(r"set\s+\$rw_upstream\s+(\S+?)\s*;")

# Каталог статической заглушки внутри контейнера remnawave-nginx: индекс отсюда
# отдаётся при недоступности fallback, если опция включена (см. _fallback_locations)
LOCAL_STUB_ROOT = "/usr/share/nginx/html"


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
class XhttpRule:
    """XHTTP-локация → Xray-инбаунд на 127.0.0.1 (path = путь инбаунда)."""
    name: str
    path: str
    port: int
    rule_type: str = "xhttp"


@dataclass
class ProxyRule:
    """Обычное проксирование (fallback-сайт, панель и т.п.)."""
    name: str
    path: str
    target_url: str
    rule_type: str = "proxy"


Rule = GrpcRule | XhttpRule | ProxyRule


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
    # Один базовый домен на весь профиль: server_name принимает его и все
    # поддомены, {{DOMAIN}} в путях сертификатов — это он же. Домен у ноды
    # тогда не нужен, но работает только с wildcard-сертификатом *.домен
    wildcard_domain: str = ""
    # Куда проксировать «мусорный» трафик (всё, что не попало в правила)
    # и ошибки Xray (502/503/504) — снаружи сервер выглядит как обычный сайт
    fallback_url: str = ""
    # В TLS 1.3 возобновление сессии работает только через тикеты: без них
    # каждое переподключение мобильного клиента — полное рукопожатие,
    # самая дорогая по CPU операция nginx
    tls_session_tickets: bool = True
    # so_keepalive на клиентских listen: сокеты клиентов держит nginx, а не
    # Xray (keepalive в sockopt инбаунда действует только на loopback), и без
    # него мёртвый клиент занимает worker_connection до grpc_read_timeout 1h.
    # Пусто = выключено
    client_tcp_keepalive: str = "30s:10s:3"
    access_log_enabled: bool = False
    # Упавший fallback обычно даёт 502 → @drop → 444, и снаружи сервер выглядит
    # как молчащий порт. С этой опцией 502/503/504 от заглушки уходят на
    # статическую страницу из LOCAL_STUB_ROOT в контейнере. По умолчанию выкл —
    # поведение прежнее
    local_stub_enabled: bool = False

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ProfileOptions":
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        options = cls(**{k: v for k, v in data.items() if k in known})
        options.wildcard_domain = (options.wildcard_domain or "").strip().lower()
        return options

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
            "wildcard_domain": self.wildcard_domain,
            "fallback_url": self.fallback_url,
            "tls_session_tickets": self.tls_session_tickets,
            "client_tcp_keepalive": self.client_tcp_keepalive,
            "access_log_enabled": self.access_log_enabled,
            "local_stub_enabled": self.local_stub_enabled,
        }

    @property
    def client_ip_var(self) -> str:
        # При PP без CDN realip уже переписал $remote_addr адресом из
        # PROXY-заголовка, отдельная переменная не нужна
        return "$client_ip" if self.cdn_enabled else "$remote_addr"

    @property
    def server_names(self) -> str:
        if self.wildcard_domain:
            return f"{self.wildcard_domain} *.{self.wildcard_domain}"
        return DOMAIN_PLACEHOLDER

    def cert_path(self, path: str) -> str:
        if self.wildcard_domain:
            return path.replace(DOMAIN_PLACEHOLDER, self.wildcard_domain)
        return path


def validate_rule(rule) -> None:
    if not _NAME_RE.match(rule.name):
        raise RuleValidationError(f"Недопустимое имя правила: {rule.name!r}")
    if isinstance(rule, GrpcRule):
        if not _SERVICE_PATH_RE.match(rule.service_path):
            raise RuleValidationError(f"Недопустимый serviceName: {rule.service_path!r}")
        if not 1 <= rule.port <= 65535:
            raise RuleValidationError(f"Недопустимый порт: {rule.port}")
    elif isinstance(rule, XhttpRule):
        if not _XHTTP_PATH_RE.match(rule.path):
            raise RuleValidationError(f"Недопустимый путь XHTTP: {rule.path!r}")
        if not 1 <= rule.port <= 65535:
            raise RuleValidationError(f"Недопустимый порт: {rule.port}")
    elif isinstance(rule, ProxyRule):
        if not _PROXY_PATH_RE.match(rule.path):
            raise RuleValidationError(f"Недопустимый путь локации: {rule.path!r}")
        if not _TARGET_URL_RE.match(rule.target_url):
            raise RuleValidationError(f"Недопустимый target URL: {rule.target_url!r}")
    else:
        raise RuleValidationError(f"Неизвестный тип правила: {type(rule).__name__}")


def rule_location_path(rule: Rule) -> str:
    return f"/{rule.service_path}" if isinstance(rule, GrpcRule) else rule.path


def xhttp_upstream_name(rule: XhttpRule) -> str:
    return f"xhttp_{rule.name}"


def validate_rules(rules: list[Rule]) -> None:
    for rule in rules:
        validate_rule(rule)
    names = [r.name for r in rules]
    if len(names) != len(set(names)):
        raise RuleValidationError("Имена правил должны быть уникальны")
    paths = [rule_location_path(r) for r in rules]
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
    if options.wildcard_domain:
        if options.wildcard_domain.startswith("*."):
            raise OptionsValidationError(
                "Wildcard-домен указывается без «*.» — поддомены подхватываются сами"
            )
        if not DOMAIN_RE.match(options.wildcard_domain):
            raise OptionsValidationError(f"Некорректный wildcard-домен: {options.wildcard_domain!r}")
    if options.client_tcp_keepalive:
        match = _SO_KEEPALIVE_RE.match(options.client_tcp_keepalive)
        if not match or not 1 <= int(match.group(3)) <= 100:
            raise OptionsValidationError(
                f"Некорректный TCP keepalive: {options.client_tcp_keepalive!r} — "
                "ожидается idle:intvl:cnt, например 30s:10s:3 (cnt от 1 до 100)"
            )


def _grpc_block(rule: GrpcRule, ip_var: str, has_fallback: bool) -> str:
    """Блок живёт между маркерами и попадает в том числе в конфиги с ручными
    правками, поэтому ссылаться отсюда можно только на @fallback — её наличие
    известно по опциям, а @drop может не существовать.

    Проверка Content-Type однострочная намеренно: закрывающая скобка на своей
    строке обрезала бы тело location при обратном разборе правил.
    """
    not_grpc = 'if ($content_type !~* "^application/grpc")'
    if has_fallback:
        # Случайный браузер по этому пути и упавший Xray одинаково получают
        # заглушку: снаружи путь неотличим от несуществующей страницы сайта
        guard = (
            f"            error_page {NOT_GRPC_CODE} 502 503 504 = @fallback;\n"
            f"            {not_grpc} {{ return {NOT_GRPC_CODE}; }}\n"
        )
    else:
        guard = f"            {not_grpc} {{ return 444; }}\n"
    return f"""        # rule: {rule.name} type=grpc
        location ^~ /{rule.service_path} {{
{guard}            grpc_pass grpc://127.0.0.1:{rule.port};
            grpc_set_header Host $host;
            grpc_set_header X-Forwarded-For {ip_var};
            grpc_read_timeout 1h;
            grpc_send_timeout 1h;
            access_log off;
        }}"""


def _xhttp_block(rule: XhttpRule, ip_var: str, has_fallback: bool, keepalive: bool) -> str:
    """Одно правило обслуживает все режимы XHTTP, разводя их по Content-Type.

    `stream-one` и аплоад `stream-up` приходят с `application/grpc` (Xray сам
    ставит этот тип, чтобы прокси и CDN пропускали поток) и требуют полного
    дуплекса — их берёт `grpc_pass`. `packet-up` и даунлоад-стримы ходят
    обычными GET/POST: им нужен `proxy_pass` без буферизации запроса и ответа,
    иначе аплоад копится в nginx, а даунлоад встаёт. Разводка сделана через
    error_page на именованную локацию, а не вторым `if`: `if` внутри location
    безопасен только с `return`.

    Блок самодостаточен — его вставляют и в чужие конфиги: `client_max_body_size`
    переопределён в обеих локациях (унаследованный дефолт 1m убил бы аплоад
    stream-up на первом мегабайте), а обрыв идёт в свою drop-локацию, потому
    что серверной @drop в чужом конфиге может не быть, а собственный
    error_page в location отменяет наследование серверного.

    Перехват ошибок Xray выборочный: 404/405 (пробер на верном пути) и
    502/503/504 (Xray упал) уходят на заглушку, а 400/409/413 — это ответы
    Xray своему клиенту, они проходят как есть.
    """
    plain = f"@xhttp_{rule.name}"
    drop = f"@xhttp_{rule.name}_drop"
    on_error = "@fallback" if has_fallback else drop
    if keepalive:
        proxy_pass = (f"proxy_pass http://{xhttp_upstream_name(rule)};\n"
                      f"            proxy_http_version 1.1;\n"
                      f'            proxy_set_header Connection "";')
    else:
        proxy_pass = (f"proxy_pass http://127.0.0.1:{rule.port};\n"
                      f"            proxy_http_version 1.1;")
    return f"""        # rule: {rule.name} type=xhttp
        location ^~ {rule.path} {{
            client_max_body_size 0;
            error_page {NOT_GRPC_CODE} = {plain};
            error_page {XHTTP_FALLBACK_CODES} = {on_error};
            error_page {XHTTP_DROP_CODES} = {drop};
            if ($content_type !~* "^application/grpc") {{ return {NOT_GRPC_CODE}; }}
            grpc_pass grpc://127.0.0.1:{rule.port};
            grpc_intercept_errors on;
            grpc_set_header Host $host;
            grpc_set_header X-Forwarded-For {ip_var};
            grpc_read_timeout 1h;
            grpc_send_timeout 1h;
            access_log off;
        }}

        location {plain} {{
            client_max_body_size 0;
            {proxy_pass}
            proxy_request_buffering off;
            proxy_buffering off;
            proxy_socket_keepalive on;
            proxy_intercept_errors on;
            error_page {XHTTP_FALLBACK_CODES} = {on_error};
            error_page {XHTTP_DROP_CODES} = {drop};
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For {ip_var};
            proxy_read_timeout 1h;
            proxy_send_timeout 1h;
            access_log off;
        }}

        location {drop} {{
            return 444;
        }}"""


def _render_upstreams(rules: list[Rule]) -> str:
    """Keepalive-пул к XHTTP-инбаунду. packet-up шлёт отдельный POST на каждый
    чанк, и без пула каждый из них — новый TCP-коннект к 127.0.0.1 с TIME_WAIT
    на стороне nginx. gRPC-ветке пул не нужен: там одно долгое h2c-соединение.
    """
    blocks = [f"""    upstream {xhttp_upstream_name(r)} {{
        server 127.0.0.1:{r.port};
        keepalive 64;  {AUTO_MARKER}
        keepalive_requests 100000;
    }}""" for r in rules if isinstance(r, XhttpRule)]
    return "\n\n".join(blocks)


def _proxy_headers(ip_var: str, indent: str = "            ") -> str:
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
    return "\n".join(f"{indent}{line}" for line in lines)


def _is_ip_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _proxy_pass_lines(target_url: str) -> list[str]:
    """proxy_pass (+ set/proxy_ssl_*) для цели.

    Домен без пути идёт через переменную `$rw_upstream`, чтобы http-resolver
    перечитывал DNS (смена IP заглушки за CDN не убивает маскировку до
    рестарта). Для https-домена добавляется SNI с именем цели — иначе за CDN
    уйдёт запрос без SNI и вернётся не тот сайт. Цель-IP и домен с путём
    (переменная сломала бы подстановку URI локации) остаются на литеральном
    proxy_pass — поведение не меняется.
    """
    parts = urlsplit(target_url)
    host = parts.hostname or ""
    is_ip = _is_ip_host(host)
    lines: list[str] = []
    if not is_ip and parts.path == "":
        lines.append(f"set $rw_upstream {target_url};")
        lines.append("proxy_pass $rw_upstream$request_uri;")
    else:
        lines.append(f"proxy_pass {target_url};")
    if parts.scheme == "https" and not is_ip:
        lines.append("proxy_ssl_server_name on;")
        lines.append(f"proxy_ssl_name {host};")
    return lines


def _proxy_pass_body(target_url: str, ip_var: str, prefix_lines: tuple[str, ...] = ()) -> str:
    """Тело локации-проксирования (12 пробелов отступа): служебные строки +
    proxy_pass + заголовки."""
    lines = [*prefix_lines, *_proxy_pass_lines(target_url)]
    passed = "\n".join(f"            {line}" for line in lines)
    return f"{passed}\n{_proxy_headers(ip_var)}"


def _proxy_block(rule: ProxyRule, ip_var: str) -> str:
    return f"""        # rule: {rule.name} type=proxy
        location {rule.path} {{
{_proxy_pass_body(rule.target_url, ip_var)}
        }}"""


def _render_rule(rule: Rule, ip_var: str, has_fallback: bool, keepalive: bool) -> str:
    if isinstance(rule, GrpcRule):
        return _grpc_block(rule, ip_var, has_fallback)
    if isinstance(rule, XhttpRule):
        return _xhttp_block(rule, ip_var, has_fallback, keepalive)
    return _proxy_block(rule, ip_var)


def _render_locations(rules: list[Rule], options: "ProfileOptions", keepalive: bool) -> str:
    ip_var = options.client_ip_var
    has_fallback = bool(options.fallback_url)
    return "\n\n".join(_render_rule(r, ip_var, has_fallback, keepalive) for r in rules)


def _replace_section(config: str, start_marker: str, end_marker: str,
                     body: str, indent: str) -> str:
    start = config.find(start_marker)
    end = config.find(end_marker)
    head = config[: start + len(start_marker)]
    tail = config[end:]
    return f"{head}\n{body}\n{indent}{tail}"


def _fallback_locations(options: "ProfileOptions") -> str:
    """Живут вне маркеров: весь не попавший в правила трафик и ошибки Xray
    уходят на обычный сайт.

    С local_stub_enabled недоступность самой заглушки (nginx-генерируемые
    502/503/504 при неудачном коннекте — intercept_errors тут выключен, но на
    свои ошибки коннекта он не влияет) уводит запрос на статическую страницу
    из контейнера, а не в 444: снаружи сервер продолжает выглядеть сайтом.
    Собственный 5xx, отданный заглушкой, по-прежнему проходит как есть.
    """
    prefix = ("error_page 502 503 504 = @stub;",) if options.local_stub_enabled else ()
    body = _proxy_pass_body(options.fallback_url, options.client_ip_var, prefix)
    stub = f"""

        location @stub {{
            root {LOCAL_STUB_ROOT};
            rewrite ^ /index.html break;
        }}""" if options.local_stub_enabled else ""
    return f"""
        location / {{
{body}
        }}

        location @fallback {{
{body}
        }}{stub}"""


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


def generate_full_config(options: ProfileOptions, rules: list[Rule]) -> str:
    validate_options(options)
    validate_rules(rules)
    if options.fallback_url and any(isinstance(r, ProxyRule) and r.path == "/" for r in rules):
        raise RuleValidationError(
            "location / уже занята fallback-проксированием из опций — "
            "правило с путём / не нужно"
        )

    http_parts: list[str] = []
    if options.cdn_enabled:
        http_parts.append(_realip_maps(options))

    http_parts.append(f"""    {UPSTREAMS_START_MARKER}
{_render_upstreams(rules)}
    {UPSTREAMS_END_MARKER}
""")

    if options.http_redirect_enabled:
        acme = ("        location /.well-known/acme-challenge/ { root /var/www/html; }\n"
                if options.acme_enabled else "")
        http_parts.append(f"""    server {{
        listen 80;
        server_name {options.server_names};
{acme}        location / {{ return 301 https://$host$request_uri; }}
    }}
""")

    if options.reject_default_server:
        # ssl_reject_handshake рвёт только TLS-рукопожатие. Обычный HTTP на 443
        # рукопожатия не начинает и доходит до обработки запроса — без своего
        # error_page этот блок отдал бы фирменную 400-ю страницу nginx
        http_parts.append(f"""    server {{
        listen 443 ssl default_server;
        ssl_reject_handshake on;

        error_page {NGINX_OWN_ERROR_CODES} = @drop;

        location @drop {{
            return 444;
        }}
    }}
""")

    so_keepalive = (f" so_keepalive={options.client_tcp_keepalive}"
                    if options.client_tcp_keepalive else "")
    pp_listen = (f"        listen {options.proxy_protocol_port} ssl proxy_protocol{so_keepalive};\n"
                 if options.proxy_protocol_enabled else "")
    pp_realip = (f"        set_real_ip_from {options.haproxy_ip or '0.0.0.0/0'};\n"
                 f"        real_ip_header proxy_protocol;\n\n"
                 if options.proxy_protocol_enabled else "")
    locations = _render_locations(rules, options, keepalive=True)
    fallback = _fallback_locations(options) if options.fallback_url else ""

    # Своих заголовков в ответ не добавляем: клиент должен получать ровно то,
    # что отдала бы заглушка при прямом обращении — любой лишний или
    # продублированный заголовок выдаёт, что перед сайтом стоит прокси
    http_parts.append(f"""    server {{
        listen 443 ssl{so_keepalive};
{pp_listen}        http2 on;
        server_name {options.server_names};

        ssl_certificate     {options.cert_path(options.ssl_cert_path)};
        ssl_certificate_key {options.cert_path(options.ssl_key_path)};

        error_page {NGINX_OWN_ERROR_CODES} = @drop;

        location @drop {{
            return 444;
        }}

{pp_realip}        {LOCATIONS_START_MARKER}
{locations}
        {LOCATIONS_END_MARKER}{fallback}
    }}
""")

    http_body = "\n".join(http_parts)
    session_tickets = "on" if options.tls_session_tickets else "off"
    # На VPN-ноде access_log — только бесполезная запись на диск в контейнере
    access_log = "" if options.access_log_enabled else "    access_log off;\n"
    return f"""# Managed by monitoring panel (Remnawave nginx profile)
worker_processes auto;
worker_rlimit_nofile 65536;  {AUTO_MARKER}
# grpc_read_timeout/proxy_read_timeout 1h держат соединения старых воркеров
# после каждого reload; панель синкает конфиг часто, и без потолка поколения
# воркеров копились бы до часа, удерживая память и коннекты
worker_shutdown_timeout 60s;
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
{access_log}    server_tokens off;
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;
    keepalive_timeout 75s;
    # XHTTP packet-up шлёт запрос на каждый чанк (десятки в секунду на
    # соединение); при 10000 лимит выбирается за минуты, nginx рвёт соединение,
    # клиент переустанавливает TLS — шторм рукопожатий палит поведенческий DPI
    # и выедает эфемерные порты промежуточных прокси. Для gRPC/WS это одно
    # долгое соединение — им лимит безразличен
    keepalive_requests 1000000;
    reset_timedout_connection on;

    # Имя в proxy_pass через переменную ($rw_upstream) резолвится этим
    # резолвером на каждом запросе — смена IP заглушки за CDN подхватывается
    # без рестарта. ipv6=off: заглушки за CDN адресуются по A-записям
    resolver 1.1.1.1 8.8.8.8 9.9.9.9 valid=300s ipv6=off;
    resolver_timeout 5s;

    # gRPC-транспорт Xray — это один бесконечный поток в теле запроса.
    # Любой ненулевой лимит рано или поздно обрывает соединение
    # («client intended to send too large chunked body»).
    client_max_body_size 0;

    # XHTTP в режиме packet-up умеет нести данные в заголовке запроса,
    # и на больших uplinkChunkSize дефолтных буферов не хватает
    large_client_header_buffers 8 32k;

    map $http_upgrade $connection_upgrade {{
        default upgrade;
        ""      close;
    }}

    # Ошибка, возникшая уже при обработке error_page, тоже должна доходить
    # до @drop: иначе мёртвая заглушка на gRPC-пути отдала бы 502 от nginx
    recursive_error_pages on;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:20m;  {AUTO_MARKER}
    ssl_session_timeout 1d;
    ssl_session_tickets {session_tickets};

{http_body}}}
"""


def splice_rules(config: str, rules: list[Rule], options: ProfileOptions) -> str:
    """Заменяет секции между маркерами, сохраняя ручные правки вне их.

    Секция UPSTREAMS появилась позже LOCATIONS: в конфиге без неё XHTTP-правила
    проксируют напрямую, без keepalive-пула — пул придёт после «Вставить шаблон».
    """
    validate_rules(rules)
    if not has_markers(config):
        raise MissingMarkersError(
            "В конфиге нет маркеров LOCATIONS — воспользуйтесь «Вставить шаблон»"
        )
    keepalive = has_upstream_markers(config)
    result = _replace_section(
        config, LOCATIONS_START_MARKER, LOCATIONS_END_MARKER,
        _render_locations(rules, options, keepalive), indent="        ",
    )
    if keepalive:
        result = _replace_section(
            result, UPSTREAMS_START_MARKER, UPSTREAMS_END_MARKER,
            _render_upstreams(rules), indent="    ",
        )
    return result


def parse_rules_from_config(config: str) -> list[Rule]:
    """Обратный парсер правил из секции LOCATIONS (round-trip c генерацией).

    Правила возвращаются в порядке появления в конфиге — иначе повторная
    генерация переставляла бы локации и каждый CRUD давал бы новый хэш.
    """
    start = config.find(LOCATIONS_START_MARKER)
    end = config.find(LOCATIONS_END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise MissingMarkersError(
            "В конфиге нет маркеров LOCATIONS — структурные правила недоступны"
        )
    section = config[start:end]
    found: list[tuple[int, Rule]] = []

    for match in _GRPC_BLOCK_RE.finditer(section):
        pass_match = _GRPC_PASS_RE.search(match.group("body"))
        if not pass_match:
            continue
        found.append((match.start(), GrpcRule(
            name=match.group("name"),
            service_path=match.group("service_path"),
            port=int(pass_match.group(1)),
        )))

    for match in _XHTTP_BLOCK_RE.finditer(section):
        pass_match = _GRPC_PASS_RE.search(match.group("body"))
        if not pass_match:
            continue
        found.append((match.start(), XhttpRule(
            name=match.group("name"),
            path=match.group("path"),
            port=int(pass_match.group(1)),
        )))

    for match in _PROXY_BLOCK_RE.finditer(section):
        body = match.group("body")
        # Доменная цель хранит исходный URL в set-строке, proxy_pass там —
        # переменная; цель-IP и домен с путём — прямо в proxy_pass
        set_match = _PROXY_SET_RE.search(body)
        if set_match:
            target_url = set_match.group(1)
        else:
            pass_match = _PROXY_PASS_RE.search(body)
            if not pass_match:
                continue
            target_url = pass_match.group(1)
        found.append((match.start(), ProxyRule(
            name=match.group("name"),
            path=match.group("path"),
            target_url=target_url,
        )))

    return [rule for _, rule in sorted(found, key=lambda item: item[0])]


def _has_section(config: str, start_marker: str, end_marker: str) -> bool:
    start = config.find(start_marker)
    end = config.find(end_marker)
    return start != -1 and end != -1 and end > start


def has_markers(config: str) -> bool:
    return _has_section(config, LOCATIONS_START_MARKER, LOCATIONS_END_MARKER)


def has_upstream_markers(config: str) -> bool:
    return _has_section(config, UPSTREAMS_START_MARKER, UPSTREAMS_END_MARKER)


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
