"""Pydantic-схемы управления дополнительными IP-адресами интерфейса."""

import ipaddress
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

INTERFACE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,14}$"
TX_ID_PATTERN = r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$"

MAX_ADDRESSES_PER_TRANSACTION = 256
DEFAULT_ROLLBACK_TIMEOUT_SEC = 120
MIN_ROLLBACK_TIMEOUT_SEC = 30
MAX_ROLLBACK_TIMEOUT_SEC = 600

AddressFamily = Literal["ipv4", "ipv6"]
IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
TransactionStatus = Literal["applying", "pending", "confirmed", "rolled_back", "failed"]
BackendName = Literal["netplan", "networkd", "networkmanager", "ifupdown", "fallback"]


class AddressSpec(BaseModel):
    address: str
    prefix: int = Field(..., ge=0, le=128)
    # Свой шлюз адреса: хостер выдал адрес из другой сети. None — как у основного
    gateway: Optional[str] = Field(None, max_length=64)

    @model_validator(mode="after")
    def _normalize(self) -> "AddressSpec":
        try:
            interface = ipaddress.ip_interface(f"{self.address}/{self.prefix}")
        except ValueError:
            raise ValueError(f"'{self.address}/{self.prefix}' is not a valid address")
        ip = interface.ip
        if ip.is_multicast or ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"'{ip}' cannot be assigned to an interface")
        self.address = str(ip)
        if self.gateway:
            self.gateway = _normalize_gateway(self.gateway, ip)
        else:
            self.gateway = None
        return self

    @property
    def family(self) -> AddressFamily:
        return "ipv6" if ":" in self.address else "ipv4"

    @property
    def cidr(self) -> str:
        return f"{self.address}/{self.prefix}"


class NetworkApplyRequest(BaseModel):
    interface: str = Field(..., pattern=INTERFACE_PATTERN)
    add: list[AddressSpec] = Field(default_factory=list)
    # Свои адреса удаляются из конфига, адреса хостера — снимаются поверх него
    remove: list[AddressSpec] = Field(default_factory=list)
    # Снятые раньше адреса хостера, которые вернуть на интерфейс
    restore: list[AddressSpec] = Field(default_factory=list)
    # Адреса, по которым панель ходит на ноду: удалять их нельзя ни при каких условиях
    protected: list[str] = Field(default_factory=list)
    rollback_timeout_sec: int = Field(
        DEFAULT_ROLLBACK_TIMEOUT_SEC, ge=MIN_ROLLBACK_TIMEOUT_SEC, le=MAX_ROLLBACK_TIMEOUT_SEC
    )

    @model_validator(mode="after")
    def _check_sets(self) -> "NetworkApplyRequest":
        self.add = _dedupe(self.add)
        self.remove = _dedupe(self.remove)
        self.restore = _dedupe(self.restore)
        if not self.add and not self.remove and not self.restore:
            raise ValueError("nothing to apply: add, remove and restore are empty")
        if len(self.add) + len(self.remove) + len(self.restore) > MAX_ADDRESSES_PER_TRANSACTION:
            raise ValueError(f"at most {MAX_ADDRESSES_PER_TRANSACTION} addresses per transaction")
        appearing = {spec.cidr for spec in self.add} | {spec.cidr for spec in self.restore}
        overlap = appearing & {spec.cidr for spec in self.remove}
        if overlap:
            raise ValueError(f"addresses both added and removed: {', '.join(sorted(overlap))}")
        if any(spec.gateway for spec in self.restore):
            raise ValueError("a restored hoster address keeps the hoster routing and cannot take a gateway")
        added = {spec.address for spec in self.add}
        looped = sorted({spec.gateway for spec in self.add if spec.gateway in added})
        if looped:
            raise ValueError(f"gateway is one of the added addresses: {', '.join(looped)}")
        self.protected = [ip for ip in self.protected if _is_ip(ip)]
        return self


def _normalize_gateway(value: str, address: IPAddress) -> str:
    """Link-local шлюз разрешён: у IPv6 он обычное дело (`fe80::1` у Hetzner)."""
    try:
        gateway = ipaddress.ip_address(value.strip())
    except ValueError:
        raise ValueError(f"gateway '{value}' is not an IP address")
    if gateway.version != address.version:
        raise ValueError(f"gateway {gateway} and address {address} are of different families")
    if gateway.is_multicast or gateway.is_loopback or gateway.is_unspecified or gateway.is_reserved:
        raise ValueError(f"'{gateway}' cannot be a gateway")
    if gateway == address:
        raise ValueError(f"{address} cannot be its own gateway")
    return str(gateway)


def _dedupe(specs: list[AddressSpec]) -> list[AddressSpec]:
    seen: set[str] = set()
    unique: list[AddressSpec] = []
    for spec in specs:
        if spec.cidr not in seen:
            seen.add(spec.cidr)
            unique.append(spec)
    return unique


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


class TransactionRequest(BaseModel):
    transaction_id: str = Field(..., pattern=TX_ID_PATTERN)


class LiveAddress(BaseModel):
    address: str
    prefix: int
    family: AddressFamily
    scope: str
    managed: bool
    primary: bool
    dynamic: bool
    gateway: Optional[str] = None


class InterfaceState(BaseModel):
    name: str
    is_up: bool
    is_default: bool
    kind: str  # physical | bond | vlan | bridge
    addresses: list[LiveAddress]
    # Опущенную карту подняла панель вместе с адресами (links.list): после
    # загрузки её поднимает нода, с последним адресом панель её опускает
    brought_up: bool = False


class ManagedAddress(BaseModel):
    interface: str
    address: str
    prefix: int
    gateway: Optional[str] = None


class SuppressedAddress(BaseModel):
    """Адрес хостера, снятый панелью: в конфиге хостера он остаётся."""
    interface: str
    address: str
    prefix: int


class TransactionInfo(BaseModel):
    id: str
    status: TransactionStatus
    interface: str
    backend: str
    added: list[str]
    removed: list[str]
    started_at: Optional[str]
    deadline_at: Optional[str]
    finished_at: Optional[str]
    message: str
    warnings: list[str]


class NetworkStateResponse(BaseModel):
    supported: bool
    message: Optional[str] = None
    backend: Optional[BackendName] = None
    backend_detail: str = ""
    default_interface: Optional[str] = None
    # family → шлюз default-маршрута: адрес с таким же шлюзом своих маршрутов не получает
    default_gateway: dict[AddressFamily, str] = Field(default_factory=dict)
    interfaces: list[InterfaceState]
    managed: list[ManagedAddress]
    suppressed: list[SuppressedAddress] = Field(default_factory=list)
    transaction: Optional[TransactionInfo]
    history: list[TransactionInfo]
    rollback_timeout_sec: int = DEFAULT_ROLLBACK_TIMEOUT_SEC


class NetworkApplyResponse(BaseModel):
    success: bool
    transaction_id: Optional[str]
    status: Optional[str]
    message: str
    rolled_back: bool = False
    error_log: str = ""
    backend: Optional[BackendName] = None
    deadline_at: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


class NetworkActionResponse(BaseModel):
    success: bool
    status: Optional[str]
    message: str
