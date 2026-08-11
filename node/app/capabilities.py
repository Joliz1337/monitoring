"""Права панели на этой ноде: переменная NODE_CAPABILITIES в .env.

Пустая строка (или отсутствие переменной) означает полный доступ — так работают
все ноды, поставленные до появления механизма.

Модуль намеренно не импортирует ничего из `app.*` на верхнем уровне: панель
загружает этот файл напрямую через importlib, чтобы сверить свою карту путей
с этой. Общий пакет `app` в обоих проектах сделал бы такой импорт невозможным.
"""

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class Domain(str, Enum):
    """Разделы API, которые владелец ноды может закрывать по отдельности."""

    TRAFFIC = "traffic"
    HAPROXY = "haproxy"
    FIREWALL = "firewall"
    IPSET = "ipset"
    SSH = "ssh"
    SSL = "ssl"
    ANTIDDOS = "antiddos"
    REMNAWAVE = "remnawave"
    SYSTEM = "system"
    EXEC = "exec"


class Access(str, Enum):
    NONE = "no"
    READ = "ro"
    WRITE = "rw"


_ACCESS_RANK = {Access.NONE: 0, Access.READ: 1, Access.WRITE: 2}

# Путь → домен. Разбор идёт посегментно справа налево, поэтому длинный префикс
# перебивает короткий: /api/haproxy/firewall уходит в firewall, а не в haproxy.
DOMAIN_PREFIXES: dict[str, Domain] = {
    "/api/traffic": Domain.TRAFFIC,
    "/api/haproxy": Domain.HAPROXY,
    "/api/haproxy/firewall": Domain.FIREWALL,
    "/api/firewall": Domain.FIREWALL,
    "/api/ipset": Domain.IPSET,
    "/api/ssh": Domain.SSH,
    "/api/ssl": Domain.SSL,
    "/api/antiddos": Domain.ANTIDDOS,
    "/api/remnawave": Domain.REMNAWAVE,
    "/api/system": Domain.SYSTEM,
    "/api/system/execute": Domain.EXEC,
    "/api/system/execute-stream": Domain.EXEC,
}

# Эти адреса не закрываются ничем. Без метрик панель перестаёт обновлять
# last_seen, считает ноду мёртвой и шлёт ложный critical; без /health не
# поднимается nginx (depends_on: service_healthy) и ddos-watchdog снимает
# защитную цепочку прямо во время атаки; без update и замены сертификата ноду
# нельзя ни починить, ни продлить удалённо.
ALWAYS_ALLOWED: frozenset[str] = frozenset({
    "/health",
    "/api/version",
    "/api/metrics",
    "/api/system/versions",
    "/api/system/update",
    "/api/system/update/status",
    "/api/system/replace-node-cert",
})

# POST, которые ничего не меняют: под :ro они обязаны проходить.
READ_ONLY_WRITES: frozenset[str] = frozenset({
    "/api/haproxy/validate",
    "/api/ssh/config/test",
})

READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

PRESETS: dict[str, dict[Domain, Access]] = {
    "full": {domain: Access.WRITE for domain in Domain},
    "readonly": {domain: Access.READ for domain in Domain},
    "monitoring": {Domain.TRAFFIC: Access.READ, Domain.SYSTEM: Access.READ},
}

# Метрики отдаются всегда, отдельного домена под них нет. Но слово в строке
# прав — не опечатка оператора: принимаем молча, просто ничего не меняем.
IGNORED_TOKENS: frozenset[str] = frozenset({"metrics"})

READ_ONLY_SUFFIX = "ro"
DENIAL_ERROR = "capability_denied"
DENIAL_HEADER = "X-Capability-Denied"


def normalize_path(path: str) -> str:
    """Путь в том виде, в каком его сверяют с картой: без query и хвостового слэша."""
    probe = path.split("?", 1)[0]
    return probe.rstrip("/") or "/"


def domain_for_path(path: str) -> Optional[Domain]:
    """Домен по нормализованному пути; None — путь вне карты, решает роутер."""
    probe = path
    while probe:
        domain = DOMAIN_PREFIXES.get(probe)
        if domain is not None:
            return domain
        cut = probe.rfind("/")
        if cut <= 0:
            return None
        probe = probe[:cut]
    return None


@dataclass(frozen=True)
class Denial:
    domain: Domain
    required: Access


def denial_message(denial: Denial) -> str:
    action = "write" if denial.required is Access.WRITE else "read"
    return f"Node capability '{denial.domain.value}' denies {action} access (NODE_CAPABILITIES)"


@dataclass(frozen=True)
class CapabilityPolicy:
    grants: dict[Domain, Access]
    unknown_tokens: tuple[str, ...]
    unrestricted: bool

    def check(self, method: str, path: str) -> Optional[Denial]:
        if self.unrestricted:
            return None

        normalized = normalize_path(path)
        if normalized in ALWAYS_ALLOWED:
            return None

        domain = domain_for_path(normalized)
        if domain is None:
            return None

        writes = method.upper() not in READ_METHODS and normalized not in READ_ONLY_WRITES
        granted = self.grants.get(domain, Access.NONE)
        if granted is Access.WRITE or (granted is Access.READ and not writes):
            return None
        return Denial(domain=domain, required=Access.WRITE if writes else Access.READ)

    def published(self) -> Optional[dict[str, str]]:
        """Карта прав для панели: либо все домены разом, либо None.

        Частичная карта не годится: панель трактует отсутствующий ключ как
        разрешение, и «публикуем только разрешённое» обнулило бы весь механизм.
        """
        if self.unrestricted:
            return None
        return {
            domain.value: self.grants.get(domain, Access.NONE).value
            for domain in Domain
        }

    def denial_body(self, denial: Denial) -> dict:
        # detail строкой, а не объектом: панель и фронт кладут его прямо в текст
        # ошибки, словарь превратился бы в пустое уведомление.
        return {
            "detail": denial_message(denial),
            "error": DENIAL_ERROR,
            "domain": denial.domain.value,
            "access": denial.required.value,
            "capabilities": self.published(),
        }


UNRESTRICTED = CapabilityPolicy(
    grants={domain: Access.WRITE for domain in Domain},
    unknown_tokens=(),
    unrestricted=True,
)


def _grant(grants: dict[Domain, Access], domain: Domain, access: Access) -> None:
    """Уровень только повышается — иначе результат зависел бы от порядка слов."""
    if _ACCESS_RANK[access] > _ACCESS_RANK[grants.get(domain, Access.NONE)]:
        grants[domain] = access


def _apply_token(token: str, grants: dict[Domain, Access], unknown: list[str]) -> None:
    name, separator, suffix = token.partition(":")
    if separator and suffix != READ_ONLY_SUFFIX:
        unknown.append(token)
        return
    read_only = bool(separator)

    if name in IGNORED_TOKENS:
        return

    preset = PRESETS.get(name)
    if preset is not None:
        # readonly:ro и full:ro — правдоподобная опечатка, а не «ещё строже»
        if read_only:
            unknown.append(token)
            return
        for domain, access in preset.items():
            _grant(grants, domain, access)
        return

    try:
        domain = Domain(name)
    except ValueError:
        unknown.append(token)
        return
    _grant(grants, domain, Access.READ if read_only else Access.WRITE)


def parse_capabilities(raw: str) -> CapabilityPolicy:
    """Разбор строки NODE_CAPABILITIES.

    Незнакомое слово не роняет ноду: недоступный агент чинится только по SSH,
    а неверно понятая строка — правкой .env. Отказ всегда в закрытую сторону.
    """
    cleaned = (raw or "").strip().strip("\"'").strip()
    if not cleaned:
        return UNRESTRICTED

    grants: dict[Domain, Access] = {}
    unknown: list[str] = []
    for token in cleaned.replace(",", " ").split():
        _apply_token(token.lower(), grants, unknown)

    return CapabilityPolicy(grants=grants, unknown_tokens=tuple(unknown), unrestricted=False)


class DenyLog:
    """Один WARNING на пару (домен, доступ) в интервал.

    Панель опрашивает закрытые эндпоинты по циклам от 10 секунд, поэтому строка
    на каждый отказ забила бы журнал ноды за сутки.
    """

    INTERVAL_SECONDS = 300

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._last_logged: dict[tuple[str, str], float] = {}
        self._suppressed: dict[tuple[str, str], int] = {}

    def record(self, denial: Denial) -> None:
        key = (denial.domain.value, denial.required.value)
        now = self._clock()
        last = self._last_logged.get(key)
        if last is not None and now - last < self.INTERVAL_SECONDS:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return

        self._last_logged[key] = now
        # Тело запроса не попадает в журнал никогда: в exec произвольная
        # команда, в ssh — пароль root.
        logger.warning(
            "capability_denied domain=%s access=%s suppressed=%d",
            key[0], key[1], self._suppressed.pop(key, 0),
        )


class CapabilityMiddleware:
    """Отказ до роутера, чистым ASGI.

    BaseHTTPMiddleware здесь не годится: он оборачивает ответ в task group и
    ломает потоковый вывод /api/system/execute-stream.
    """

    def __init__(self, app, policy: CapabilityPolicy):
        self.app = app
        self.policy = policy
        self._deny_log = DenyLog()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        denial = self.policy.check(scope.get("method", "GET"), scope.get("path", "/"))
        if denial is None:
            await self.app(scope, receive, send)
            return

        self._deny_log.record(denial)
        body = json.dumps(self.policy.denial_body(denial)).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (DENIAL_HEADER.lower().encode(),
                 f"{denial.domain.value}:{denial.required.value}".encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


@lru_cache(maxsize=1)
def get_policy() -> CapabilityPolicy:
    try:
        from app.config import get_settings

        return parse_capabilities(get_settings().node_capabilities)
    except Exception:
        # Исключение отсюда не дало бы приложению подняться, а нода без API
        # чинится только по SSH — открытый агент лучше недоступного.
        logger.error("NODE_CAPABILITIES unreadable, agent stays fully open", exc_info=True)
        return UNRESTRICTED
