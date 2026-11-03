"""Чистые функции GRE-туннелей DDoS-Guard: схема туннеля, номера туннелей, конфиг
для ноды и его хэш, адреса DDoS-Guard для белого списка анти-DDoS.

Проверки туннеля повторяют ноду (`node/app/models/gre.py`): туннель, который
нода отвергнет, не должен попасть в базу и вечно висеть в failed.
"""

import hashlib
import ipaddress
import json
from typing import Iterable, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.models import GreNode
from app.services.exit_proxy.settings import load_json

MAX_TUNNELS = 8
# DDoS-Guard выдаёт точка-точка сеть /30: PE — их конец, CE — защищённый адрес
PTP_PREFIX = 30
# Со страницы «Параметры подключения» DDoS-Guard: адрес их конца туннеля и сеть,
# из которой они проверяют туннель пингом
DEFAULT_REMOTE = "185.129.101.255"
DEFAULT_HEALTH_NET = "198.18.100.0/24"
INTERFACE_PREFIX = "gre-ddg"


def interface_name(slot: int) -> str:
    return f"{INTERFACE_PREFIX}{slot}"


def ptp_hosts(address: str) -> list[str]:
    network = ipaddress.IPv4Network(f"{address}/{PTP_PREFIX}", strict=False)
    return [str(host) for host in network.hosts()]


def ptp_peer(ce: str) -> Optional[str]:
    """Второй узел сети /30 — PE. Обычно это CE − 1, но у CE на первом узле — CE + 1."""
    hosts = ptp_hosts(ce)
    if ce not in hosts:
        return None
    return next(host for host in hosts if host != ce)


def _tunnel_ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(str(value).strip())
    except ValueError:
        raise ValueError(f"'{value}' — не IPv4-адрес")
    if address.is_multicast or address.is_unspecified or address.is_loopback or address.is_link_local:
        raise ValueError(f"{address} не может быть адресом туннеля")
    return str(address)


class GreTunnel(BaseModel):
    # Номер туннеля на ноде (интерфейс gre-ddg<slot>); у нового туннеля его даёт панель
    slot: Optional[int] = Field(None, ge=1, le=MAX_TUNNELS)
    local: str                       # GRE SRC — адрес сервера
    remote: str = DEFAULT_REMOTE     # GRE DST — адрес DDoS-Guard
    pe: Optional[str] = None         # их конец /30; не задан — второй узел сети CE
    ce: str                          # защищённый адрес
    health_net: str = DEFAULT_HEALTH_NET

    @field_validator("local", "remote", "ce")
    @classmethod
    def _ipv4(cls, value: str) -> str:
        return _tunnel_ipv4(value)

    @field_validator("pe")
    @classmethod
    def _optional_ipv4(cls, value: Optional[str]) -> Optional[str]:
        return _tunnel_ipv4(value) if value else None

    @field_validator("health_net")
    @classmethod
    def _network(cls, value: str) -> str:
        try:
            network = ipaddress.IPv4Network(str(value).strip(), strict=False)
        except ValueError:
            raise ValueError(f"'{value}' — не IPv4-сеть")
        if network.prefixlen < 8:
            raise ValueError(f"{network} — слишком широкая сеть проверок")
        return str(network)

    @model_validator(mode="after")
    def _addresses_fit(self) -> "GreTunnel":
        if self.pe is None:
            self.pe = ptp_peer(self.ce)
        hosts = ptp_hosts(self.ce)
        if self.ce not in hosts or self.pe not in hosts or self.pe == self.ce:
            raise ValueError(f"PE и CE должны быть двумя адресами узлов одной сети /{PTP_PREFIX}")
        if self.local in (self.pe, self.ce, self.remote):
            raise ValueError("GRE SRC — адрес самого сервера, а не адрес туннеля")
        if self.remote in (self.pe, self.ce):
            raise ValueError("GRE DST должен отличаться от PE и CE")
        return self


def check_tunnel_set(tunnels: list[GreTunnel]) -> None:
    """Набор туннелей одного сервера; ValueError с причиной."""
    if len(tunnels) > MAX_TUNNELS:
        raise ValueError(f"не больше {MAX_TUNNELS} туннелей на сервер")
    slots = [t.slot for t in tunnels if t.slot is not None]
    if len(set(slots)) != len(slots):
        raise ValueError("номера туннелей повторяются")
    protected = [t.ce for t in tunnels]
    if len(set(protected)) != len(protected):
        raise ValueError("у каждого туннеля свой CE")
    # Ядро различает GRE-туннели без ключа только по паре адресов
    endpoints = [(t.local, t.remote) for t in tunnels]
    if len(set(endpoints)) != len(endpoints):
        raise ValueError("у двух туннелей одинаковые GRE SRC и GRE DST — ядро не поднимет второй")


def assign_slots(tunnels: list[GreTunnel]) -> list[GreTunnel]:
    """Свободные номера новым туннелям; у существующих номер не меняется —
    иначе нода пересоздала бы интерфейс и оборвала соединения."""
    used = {t.slot for t in tunnels if t.slot is not None}
    free = (slot for slot in range(1, MAX_TUNNELS + 1) if slot not in used)
    assigned = [t if t.slot is not None else t.model_copy(update={"slot": next(free)}) for t in tunnels]
    return sorted(assigned, key=lambda t: t.slot)


def load_tunnels(row: Optional[GreNode]) -> list[GreTunnel]:
    """Туннели из JSON строки; битые записи отбрасываются."""
    raw = load_json(row.tunnels, []) if row is not None else []
    tunnels: list[GreTunnel] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            tunnel = GreTunnel.model_validate(item)
        except ValidationError:
            continue
        if tunnel.slot is not None:
            tunnels.append(tunnel)
    return sorted(tunnels, key=lambda t: t.slot)


def dump_tunnels(tunnels: list[GreTunnel]) -> str:
    return json.dumps([t.model_dump() for t in tunnels])


def build_node_config(row: Optional[GreNode]) -> dict:
    """Схема GreConfig агента."""
    return {"tunnels": [t.model_dump() for t in load_tunnels(row)]}


def config_hash(config: dict) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def whitelist_entries(rows: Iterable[GreNode]) -> set[str]:
    """Адрес DDoS-Guard и сеть его проверок: в аварийном режиме анти-DDoS
    они не должны попасть под ограничения новых соединений."""
    entries: set[str] = set()
    for row in rows:
        for tunnel in load_tunnels(row):
            entries.update((tunnel.remote, tunnel.health_net))
    return entries
