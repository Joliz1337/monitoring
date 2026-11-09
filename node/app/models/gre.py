"""Pydantic-схемы GRE-туннелей DDoS-Guard: защищённый адрес приходит на ноду по GRE."""

import ipaddress
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_TUNNELS = 8
# DDoS-Guard выдаёт точка-точка сеть /30: PE — их конец, CE — защищённый адрес
PTP_PREFIX = 30
# Префикс gre — интерфейс вне итогов трафика: тот же трафик уже посчитан на
# физической карте внутри GRE-пакетов
INTERFACE_PREFIX = "gre-ddg"

# ok — туннель поднят как задано; drift — есть, но расходится (служба вернёт);
# missing — нет в ядре; no_local — адреса GRE SRC нет на интерфейсах;
# conflict — чужой GRE-интерфейс с теми же адресами (ядро не даст второй)
GreLinkState = Literal["ok", "drift", "missing", "no_local", "conflict"]


def interface_name(slot: int) -> str:
    return f"{INTERFACE_PREFIX}{slot}"


def _tunnel_ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(str(value).strip())
    except ValueError:
        raise ValueError(f"'{value}' is not an IPv4 address")
    if address.is_multicast or address.is_unspecified or address.is_loopback or address.is_link_local:
        raise ValueError(f"{address} cannot be a tunnel address")
    return str(address)


def ptp_hosts(address: str) -> list[str]:
    """Оба адреса узлов сети /30, в которую входит адрес."""
    network = ipaddress.IPv4Network(f"{address}/{PTP_PREFIX}", strict=False)
    return [str(host) for host in network.hosts()]


class GreTunnelSpec(BaseModel):
    slot: int = Field(..., ge=1, le=MAX_TUNNELS)
    local: str        # GRE SRC — адрес этого сервера
    remote: str       # GRE DST — адрес DDoS-Guard
    pe: str           # их конец сети /30
    ce: str           # защищённый адрес
    health_net: str   # сеть, из которой DDoS-Guard проверяет туннель

    @field_validator("local", "remote", "pe", "ce")
    @classmethod
    def _ipv4(cls, value: str) -> str:
        return _tunnel_ipv4(value)

    @field_validator("health_net")
    @classmethod
    def _network(cls, value: str) -> str:
        try:
            network = ipaddress.IPv4Network(str(value).strip(), strict=False)
        except ValueError:
            raise ValueError(f"'{value}' is not an IPv4 network")
        if network.prefixlen < 8:
            raise ValueError(f"{network} is too wide for a health-check network")
        return str(network)

    @model_validator(mode="after")
    def _addresses_fit(self) -> "GreTunnelSpec":
        hosts = ptp_hosts(self.ce)
        if self.ce not in hosts or self.pe not in hosts or self.pe == self.ce:
            raise ValueError(f"PE and CE must be the two host addresses of one /{PTP_PREFIX} network")
        if self.local in (self.pe, self.ce, self.remote):
            raise ValueError("GRE SRC must be an address of this server, not a tunnel address")
        if self.remote in (self.pe, self.ce):
            raise ValueError("GRE DST must differ from PE and CE")
        return self


class GreConfig(BaseModel):
    tunnels: list[GreTunnelSpec] = Field(default_factory=list, max_length=MAX_TUNNELS)

    @model_validator(mode="after")
    def _unique(self) -> "GreConfig":
        slots = [tunnel.slot for tunnel in self.tunnels]
        if len(set(slots)) != len(slots):
            raise ValueError("tunnel slots must be unique")
        protected = [tunnel.ce for tunnel in self.tunnels]
        if len(set(protected)) != len(protected):
            raise ValueError("every tunnel needs its own CE")
        # Ядро различает GRE-туннели без ключа только по паре адресов
        endpoints = [(tunnel.local, tunnel.remote) for tunnel in self.tunnels]
        if len(set(endpoints)) != len(endpoints):
            raise ValueError("two tunnels cannot share both GRE SRC and GRE DST")
        return self


class GreTunnelState(BaseModel):
    slot: int
    name: str
    local: str
    remote: str
    pe: str
    ce: str
    health_net: str
    link: GreLinkState
    mtu: Optional[int] = None
    # Имя чужого интерфейса при link=conflict
    conflict_with: Optional[str] = None
    rx_bytes: int = 0
    rx_packets: int = 0
    tx_bytes: int = 0
    tx_packets: int = 0


class GreState(BaseModel):
    supported: bool
    message: Optional[str] = None
    # Служба на хосте включена (туннели заданы) и работает
    enabled: bool = False
    active: bool = False
    # Ядро совпадает с заданным; без туннелей — своих правил и интерфейсов не осталось
    in_sync: bool = True
    tunnels: list[GreTunnelState] = Field(default_factory=list)
    # Что не прошло на последнем применении (stderr скрипта)
    last_error: Optional[str] = None


class GreCheckRequest(BaseModel):
    slot: int = Field(..., ge=1, le=MAX_TUNNELS)


class GreCheckResult(BaseModel):
    slot: int
    ping_sent: int = 0
    ping_received: int = 0
    rtt_ms: Optional[float] = None
    https_ok: bool = False
    # Адрес, с которым запрос с CE виден снаружи; должен совпасть с CE
    external_ip: Optional[str] = None
    https_error: Optional[str] = None
