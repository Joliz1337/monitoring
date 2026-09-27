"""IP-адреса сервера в профиле HAProxy: на каких слушать и через какие выходить.

Профиль — один текст на все привязанные серверы, адреса — свойство сервера.
Перед раскаткой конфиг собирается под сервер: `bind *:PORT` получает входные IP,
каждая строка `server` — копию на каждый выходной IP с `source`. Копии делят
соединения по кругу, и потолок в ~64k исходящих портов считается на каждый
выходной адрес отдельно. Без адресов собранный текст байт-в-байт равен профилю.
"""

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from app.services.haproxy_config import RULES_END_MARKER, RULES_START_MARKER

MAX_ADDRESSES = 16

LISTEN_SECTIONS = frozenset({"frontend", "listen"})
BACKEND_SECTIONS = frozenset({"backend", "listen"})
WILDCARD_HOSTS = frozenset({"", "*", "0.0.0.0"})

_BIND_LINE = re.compile(r"^(\s*bind\s+)(\S+)(.*)$")
_SERVER_LINE = re.compile(r"^(\s*server\s+)(\S+)(\s+\S+)(.*)$")
_SECTION_SOURCE = re.compile(r"^\s+source\s")
_SERVER_SOURCE_OPTION = re.compile(r"\ssource\s")
_COOKIE_OPTION = re.compile(r"(\scookie\s+)(\S+)")


class InvalidAddressError(ValueError):
    """Адрес нельзя сохранить в настройки сервера."""


@dataclass(frozen=True)
class ServerAddresses:
    listen: tuple[str, ...] = ()
    source: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.listen and not self.source

    def all_ips(self) -> list[str]:
        return list(dict.fromkeys(self.listen + self.source))


def normalize_ips(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for raw in values:
        value = raw.strip()
        try:
            address = str(ipaddress.IPv4Address(value))
        except ValueError:
            raise InvalidAddressError(f"Не IPv4-адрес: {value or '(пусто)'}") from None
        if address not in result:
            result.append(address)
    if len(result) > MAX_ADDRESSES:
        raise InvalidAddressError(f"Не больше {MAX_ADDRESSES} адресов в списке")
    return tuple(result)


def load_ips(raw: Optional[str]) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        values = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        return ()
    return tuple(values)


def dump_ips(ips: tuple[str, ...]) -> Optional[str]:
    return json.dumps(list(ips)) if ips else None


def missing_on_node(addresses: ServerAddresses, network_state: dict) -> list[str]:
    """Выбранные IP, которых нет среди IPv4-адресов интерфейсов ноды."""
    present = {
        address.get("address")
        for interface in network_state.get("interfaces") or []
        for address in interface.get("addresses") or []
        if address.get("family") == "ipv4"
    }
    return [ip for ip in addresses.all_ips() if ip not in present]


def render_for_server(config: str, addresses: ServerAddresses) -> str:
    if addresses.is_empty:
        return config
    start, end = _rules_bounds(config)
    rules = "".join(
        "".join(_render_section(section, addresses))
        for section in _split_sections(config[start:end].splitlines(keepends=True))
    )
    return config[:start] + rules + config[end:]


def _rules_bounds(config: str) -> tuple[int, int]:
    """Область правил между маркерами; без маркеров (конфиг правили руками) — весь текст."""
    start = config.find(RULES_START_MARKER)
    end = config.find(RULES_END_MARKER)
    if start == -1 or end < start:
        return 0, len(config)
    return start + len(RULES_START_MARKER), end


def _split_sections(lines: list[str]) -> list[list[str]]:
    sections: list[list[str]] = [[]]
    for line in lines:
        if line.strip() and not line[0].isspace() and not line.startswith("#"):
            sections.append([])
        sections[-1].append(line)
    return sections


def _render_section(lines: list[str], addresses: ServerAddresses) -> list[str]:
    keyword = lines[0].split()[0] if lines and lines[0].strip() else ""
    if addresses.listen and keyword in LISTEN_SECTIONS:
        lines = [_with_line_ending(line, lambda text: _bind_on(text, addresses.listen)) for line in lines]
    # Свой source у бэкенда — явный выбор автора конфига, серверные копии его бы перебили
    has_own_source = any(_SECTION_SOURCE.match(line) for line in lines)
    if addresses.source and keyword in BACKEND_SECTIONS and not has_own_source:
        lines = [copy for line in lines for copy in _server_copies(line, addresses.source)]
    return lines


def _with_line_ending(line: str, transform: Callable[[str], str]) -> str:
    text = line.rstrip("\r\n")
    return transform(text) + line[len(text):]


def _bind_on(text: str, ips: tuple[str, ...]) -> str:
    match = _BIND_LINE.match(text)
    if not match:
        return text
    prefix, listen_spec, params = match.groups()
    ports: list[str] = []
    for part in listen_spec.split(","):
        host, separator, port = part.rpartition(":")
        # Явный адрес, unix-сокет, ipv6 — автор конфига выбрал сам
        if not separator or host not in WILDCARD_HOSTS:
            return text
        ports.append(port)
    return prefix + ",".join(f"{ip}:{port}" for port in ports for ip in ips) + params


def _server_copies(line: str, ips: tuple[str, ...]) -> list[str]:
    text = line.rstrip("\r\n")
    ending = line[len(text):]
    match = _SERVER_LINE.match(text)
    if not match or _SERVER_SOURCE_OPTION.search(match.group(4)):
        return [line]
    prefix, name, target, options = match.groups()
    copies = []
    for index, ip in enumerate(ips):
        suffix = f"_o{index + 1}" if index else ""
        # Одинаковое значение cookie у копий склеило бы sticky-сессии на первой из них
        copy_options = _COOKIE_OPTION.sub(lambda m: m.group(1) + m.group(2) + suffix, options)
        copies.append(f"{prefix}{name}{suffix}{target}{copy_options} source {ip}{ending}")
    return copies
