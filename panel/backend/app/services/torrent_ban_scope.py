"""Где блокировщик торрентов банит IP: режим выбора серверов и бан по портам.

Режим exclude — бан на всех нодах, кроме списка; include — только на нодах из
списка. У сервера в списке могут быть порты: тогда клиенту на нём закрываются
только они (мост, где порт ведёт на ноду с запретом торрентов), остальное работает.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Server, TorrentBlockerSettings
from app.services.reserved_ports_sync import _version_tuple

logger = logging.getLogger(__name__)

MIN_NODE_VERSION_PORT_BAN = "10.35.0"
# Тот же лимит, что у ноды: ядро раскладывает диапазон на отдельные элементы сета
MAX_PORT_BAN_PORTS = 64
_PORT_SPEC_RE = re.compile(r"^(\d{1,5})(?:-(\d{1,5}))?$")

ServerRules = dict[int, tuple[str, ...]]


class ServerMode(str, Enum):
    EXCLUDE = "exclude"
    INCLUDE = "include"


class InvalidPortSpecError(ValueError):
    pass


@dataclass(frozen=True)
class NodeBan:
    ports: tuple[str, ...] = ()

    @property
    def whole_ip(self) -> bool:
        return not self.ports


@dataclass(frozen=True)
class BanScope:
    mode: ServerMode = ServerMode.EXCLUDE
    excluded: ServerRules = field(default_factory=dict)
    included: ServerRules = field(default_factory=dict)

    def ban_for(self, server_id: int) -> Optional[NodeBan]:
        """None — на этой ноде не банить; пустые порты — бан всего IP."""
        if self.mode == ServerMode.INCLUDE:
            if server_id not in self.included:
                return None
            return NodeBan(self.included[server_id])
        ports = self.excluded.get(server_id)
        if ports is None:
            return NodeBan()
        return NodeBan(ports) if ports else None

    @classmethod
    def from_settings(cls, settings: TorrentBlockerSettings) -> "BanScope":
        return cls(
            mode=parse_mode(settings.server_mode),
            excluded=parse_rules(settings.excluded_servers),
            included=parse_rules(settings.included_servers),
        )


def parse_mode(raw: Optional[str]) -> ServerMode:
    try:
        return ServerMode(raw or ServerMode.EXCLUDE.value)
    except ValueError:
        logger.error(f"Unknown torrent blocker server mode {raw!r}, using exclude")
        return ServerMode.EXCLUDE


def normalize_port_specs(specs: list[str]) -> list[str]:
    """«8443», «10000-10100» → отсортированные уникальные записи."""
    ranges: set[tuple[int, int]] = set()
    for spec in specs:
        match = _PORT_SPEC_RE.match(str(spec).strip())
        if not match:
            raise InvalidPortSpecError(f"Invalid port: {spec!r}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if not 1 <= start <= end <= 65535:
            raise InvalidPortSpecError(f"Invalid port range: {spec!r}")
        ranges.add((start, end))
    total = sum(end - start + 1 for start, end in ranges)
    if total > MAX_PORT_BAN_PORTS:
        raise InvalidPortSpecError(f"Too many ports: {total} > {MAX_PORT_BAN_PORTS}")
    return [str(start) if start == end else f"{start}-{end}" for start, end in sorted(ranges)]


def parse_rules(raw: Optional[str]) -> ServerRules:
    """JSON `[{"id": 5, "ports": ["8443"]}]` → {5: ("8443",)}; битые записи пропускаются."""
    if not raw:
        return {}
    try:
        items = json.loads(raw)
    except ValueError as e:
        logger.error(f"Invalid torrent blocker server list, ignoring it: {e}")
        return {}
    if not isinstance(items, list):
        return {}
    rules: ServerRules = {}
    for item in items:
        try:
            ports = item.get("ports") or []
            rules[int(item["id"])] = tuple(normalize_port_specs(ports))
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            logger.error(f"Skipping invalid torrent blocker server entry {item!r}: {e}")
    return rules


def dump_rules(rules: ServerRules) -> str:
    return json.dumps([{"id": server_id, "ports": list(ports)} for server_id, ports in rules.items()])


def node_supports_port_ban(node_version: Optional[str]) -> bool:
    if not node_version:
        return False
    return _version_tuple(node_version) >= _version_tuple(MIN_NODE_VERSION_PORT_BAN)


async def port_ban_unsupported_server_ids(db: AsyncSession) -> list[int]:
    """Активные серверы, чья нода ещё не умеет банить по портам."""
    rows = (await db.execute(
        select(Server.id, Server.node_version).where(Server.is_active == True)  # noqa: E712
    )).all()
    return sorted(server_id for server_id, version in rows if not node_supports_port_ban(version))
