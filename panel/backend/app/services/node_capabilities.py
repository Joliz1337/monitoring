"""Права, которые нода выдала панели (её NODE_CAPABILITIES).

Нода присылает готовую карту в каждом ответе `/api/metrics`, панель хранит её в
`servers.node_capabilities` и спрашивает отсюда **до** сетевого вызова: запрос,
который заведомо получит 403, не отправляется вовсе — иначе за него платили бы
ожиданием, ошибкой в интерфейсе и вечным долгом в очереди синхронизации.

Карта путей продублирована с `node/app/capabilities.py` намеренно: решение
принимается на этой стороне, до похода. Расхождение ловит MirrorTest, а любая
неоднозначность трактуется в сторону «разрешено» — лишний отказ от ноды не
страшен, ложная блокировка рабочей функции страшна.
"""

import json
import logging
from enum import Enum
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Optional

logger = logging.getLogger(__name__)


class Capability(str, Enum):
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


ACCESS_NONE = "no"
ACCESS_READ = "ro"
ACCESS_WRITE = "rw"
_ACCESS_VALUES = frozenset({ACCESS_NONE, ACCESS_READ, ACCESS_WRITE})
_CAPABILITY_NAMES = frozenset(c.value for c in Capability)

# Зеркало node/app/capabilities.py — правится только вместе с ним.
DOMAIN_PREFIXES: dict[str, Capability] = {
    "/api/traffic": Capability.TRAFFIC,
    "/api/haproxy": Capability.HAPROXY,
    "/api/haproxy/firewall": Capability.FIREWALL,
    "/api/firewall": Capability.FIREWALL,
    "/api/ipset": Capability.IPSET,
    "/api/ssh": Capability.SSH,
    "/api/ssl": Capability.SSL,
    "/api/antiddos": Capability.ANTIDDOS,
    "/api/remnawave": Capability.REMNAWAVE,
    "/api/system": Capability.SYSTEM,
    "/api/system/execute": Capability.EXEC,
    "/api/system/execute-stream": Capability.EXEC,
}

ALWAYS_ALLOWED: frozenset[str] = frozenset({
    "/health",
    "/api/version",
    "/api/metrics",
    "/api/system/versions",
    "/api/system/update",
    "/api/system/update/status",
    "/api/system/replace-node-cert",
})

READ_ONLY_WRITES: frozenset[str] = frozenset({
    "/api/haproxy/validate",
    "/api/ssh/config/test",
})

READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

DENIAL_ERROR = "capability_denied"
DENIAL_HEADER = "X-Capability-Denied"


class NodeCapabilityError(Exception):
    """Нода закрыла раздел. Не сбой связи: повторять бессмысленно."""

    def __init__(self, capability: Capability, write: bool):
        self.capability = capability
        self.write = write
        super().__init__(denied_message(capability, write))


def denied_message(capability: Capability, write: bool) -> str:
    action = "write" if write else "read"
    return f"Node capability '{capability.value}' denies {action} access (NODE_CAPABILITIES)"


def denial_headers(capability: Capability, write: bool) -> dict[str, str]:
    return {DENIAL_HEADER: f"{capability.value}:{ACCESS_WRITE if write else ACCESS_READ}"}


def normalize_path(path: str) -> str:
    probe = path.split("?", 1)[0]
    return probe.rstrip("/") or "/"


def domain_for_path(path: str) -> Optional[Capability]:
    probe = path
    while probe:
        capability = DOMAIN_PREFIXES.get(probe)
        if capability is not None:
            return capability
        cut = probe.rfind("/")
        if cut <= 0:
            return None
        probe = probe[:cut]
    return None


def resolve_route(path: str, method: str = "GET") -> tuple[Optional[Capability], bool]:
    """(домен, нужна ли запись). Домен None — путь вне ограничений."""
    normalized = normalize_path(path)
    if normalized in ALWAYS_ALLOWED:
        return None, False
    capability = domain_for_path(normalized)
    if capability is None:
        return None, False
    writes = method.upper() not in READ_METHODS and normalized not in READ_ONLY_WRITES
    return capability, writes


def normalize_map(value: Any) -> Optional[str]:
    """Карта ноды → канонический JSON для хранения, либо None если её нет."""
    if not isinstance(value, dict) or not value:
        return None
    known = {
        name: level for name, level in value.items()
        if name in _CAPABILITY_NAMES and level in _ACCESS_VALUES
    }
    if not known:
        return None
    return json.dumps(known, sort_keys=True, separators=(",", ":"))


@lru_cache(maxsize=512)
def _parse_cached(raw: str) -> Optional[MappingProxyType]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or not value:
        return None
    return MappingProxyType(dict(value))


def parse(raw: Optional[str]) -> Optional[MappingProxyType]:
    """Карта прав или None (нода без ограничений).

    Кэш обязателен: предикат зовут из циклов с конкурентностью до 30, разбор
    JSON на каждый вызов уходил бы сотнями за проход в тот же event loop.
    """
    if not raw:
        return None
    return _parse_cached(raw)


def allows(raw: Optional[str], capability: Capability, *, write: bool) -> bool:
    caps = parse(raw)
    if caps is None:
        return True
    # Ключа нет — панель знает домен, которого не знает нода: решает нода.
    level = caps.get(capability.value, ACCESS_WRITE)
    if level == ACCESS_NONE:
        return False
    if level == ACCESS_READ:
        return not write
    return True


def server_allows(server, capability: Capability, *, write: bool) -> bool:
    return allows(getattr(server, "node_capabilities", None), capability, write=write)


def server_allows_path(server, path: str, method: str = "GET") -> tuple[bool, Optional[Capability], bool]:
    """(можно ли идти, домен, нужна ли была запись) для конкретного маршрута ноды."""
    capability, write = resolve_route(path, method)
    if capability is None:
        return True, None, False
    return server_allows(server, capability, write=write), capability, write


def is_restricted(raw: Optional[str]) -> bool:
    caps = parse(raw)
    if caps is None:
        return False
    return any(level != ACCESS_WRITE for level in caps.values())


def allowed_capabilities(raw: Optional[str]) -> list[str]:
    caps = parse(raw)
    if caps is None:
        return [c.value for c in Capability]
    return [c.value for c in Capability if caps.get(c.value, ACCESS_WRITE) != ACCESS_NONE]


def capabilities_from_denial(body: Any) -> Optional[str]:
    """Карта из тела 403 самой ноды.

    Требуем наш маркер: 403 от чужого прокси перед нодой не должен переписывать
    сохранённые права.
    """
    if not isinstance(body, dict) or body.get("error") != DENIAL_ERROR:
        return None
    return normalize_map(body.get("capabilities"))


def capabilities_from_version(body: Any) -> tuple[bool, Optional[str]]:
    """(поле присутствовало, карта) из ответа /api/version или /api/system/versions.

    Отсутствие поля и `null` в нём — разные вещи: первое значит «нода старая, не
    трогаем сохранённое», второе — «ограничения сняли».
    """
    if not isinstance(body, dict) or "capabilities" not in body:
        return False, None
    return True, normalize_map(body.get("capabilities"))


async def remember_capabilities(server_id: int, caps_json: Optional[str]) -> bool:
    """Сохранить карту, если она изменилась. True — изменилась."""
    from sqlalchemy import select, update

    from app.database import async_session
    from app.models import Server

    try:
        async with async_session() as db:
            current = await db.scalar(
                select(Server.node_capabilities).where(Server.id == server_id)
            )
            if current == caps_json:
                return False
            await db.execute(
                update(Server).where(Server.id == server_id).values(node_capabilities=caps_json)
            )
            await db.commit()
    except Exception as e:
        logger.error(f"Failed to store capabilities for server {server_id}: {e}")
        return False

    logger.info(f"node_capabilities_changed server_id={server_id} value={caps_json}")
    return True


async def learn_from_denial(server_id: int, status_code: int, body: Any) -> None:
    """Уточнить права по факту отказа.

    Между сменой прав на ноде и следующим циклом метрик проходит до полуминуты —
    без этого панель всё это время ходила бы в закрытое и копила долги.
    """
    if status_code != 403:
        return
    caps_json = capabilities_from_denial(body)
    if caps_json is None:
        return
    if await remember_capabilities(server_id, caps_json):
        from app.services.node_sync_queue import drop_forbidden

        await drop_forbidden([server_id])
