"""Грамматика ввода дополнительных IP-адресов и её разворачивание в список.

Хостеры выдают адреса по-разному: одиночный IP, IP с маской, диапазон
`a-b`, целая подсеть `x.x.x.0/29`. Всё это принимается одним текстовым полем
и превращается в плоский список (адрес, префикс), который уходит на ноду.
Необязательный шлюз задаётся на всю пачку: адреса из другой сети хостер
обычно выдаёт блоком с общим шлюзом.
"""

import ipaddress
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional, Union

MAX_ADDRESSES = 256
# /31 — оба адреса (RFC 3021), /32 — один; ниже /30 разворачиваются hosts()
MAX_IPV4_EXPAND_PREFIX = 30
IPV4_HOST_PREFIX = 32
IPV6_HOST_PREFIX = 128

# Не публичные диапазоны из net_utils: RFC1918/CGNAT/ULA здесь разрешены —
# приватная VLAN хостера (vSwitch, vRack) это обычный доп. адрес на втором
# интерфейсе. Запрещено только то, что интерфейсу назначать бессмысленно.
UNASSIGNABLE_NETS = tuple(ipaddress.ip_network(net) for net in (
    "0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4",
    "::/128", "::1/128", "fe80::/10", "ff00::/8",
))
# Шлюзу link-local можно: у IPv6 это обычное дело (`fe80::1` у Hetzner)
UNUSABLE_GATEWAY_NETS = tuple(ipaddress.ip_network(net) for net in (
    "0.0.0.0/8", "127.0.0.0/8", "224.0.0.0/4", "240.0.0.0/4", "::/128", "::1/128", "ff00::/8",
))

_SPLIT_RE = re.compile(r"[\s,;]+")

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


class AddressFamily(str, Enum):
    IPV4 = "ipv4"
    IPV6 = "ipv6"


@dataclass(frozen=True)
class AddressSpec:
    address: str  # канонический вид (IPv6 сжат)
    prefix: int
    gateway: Optional[str] = None

    @property
    def family(self) -> AddressFamily:
        return AddressFamily.IPV6 if ":" in self.address else AddressFamily.IPV4

    @property
    def cidr(self) -> str:
        return f"{self.address}/{self.prefix}"

    def payload(self) -> dict:
        payload: dict = {"address": self.address, "prefix": self.prefix}
        if self.gateway:
            payload["gateway"] = self.gateway
        return payload


class AddressInputError(ValueError):
    def __init__(self, entry: str, reason: str):
        self.entry = entry
        self.reason = reason
        super().__init__(f"«{entry}»: {reason}" if entry else reason)


def split_entries(text: str) -> list[str]:
    return [entry for entry in _SPLIT_RE.split((text or "").strip()) if entry]


def _parse_ip(text: str, entry: str) -> IPAddress:
    try:
        return ipaddress.ip_address(text.strip())
    except ValueError:
        raise AddressInputError(entry, "не похоже на IP-адрес")


def _host_prefix(ip: IPAddress) -> int:
    return IPV6_HOST_PREFIX if ip.version == 6 else IPV4_HOST_PREFIX


def _check_assignable(ip: IPAddress, entry: str) -> None:
    for net in UNASSIGNABLE_NETS:
        if ip.version == net.version and ip in net:
            raise AddressInputError(
                entry, "этот адрес нельзя назначить интерфейсу (loopback, link-local, multicast или служебный диапазон)"
            )


def _expand_range(entry: str) -> list[AddressSpec]:
    start_text, _, end_text = entry.partition("-")
    start = _parse_ip(start_text, entry)
    # Сокращённый конец диапазона: `1.2.3.10-15` — только последний октет
    if start.version == 4 and end_text.strip().isdigit():
        end_text = start_text.strip().rsplit(".", 1)[0] + "." + end_text.strip()
    end = _parse_ip(end_text, entry)
    if start.version != end.version:
        raise AddressInputError(entry, "начало и конец диапазона из разных семейств адресов")
    if int(end) < int(start):
        raise AddressInputError(entry, "конец диапазона меньше начала")
    count = int(end) - int(start) + 1
    if count > MAX_ADDRESSES:
        raise AddressInputError(entry, f"в диапазоне {count} адресов, максимум {MAX_ADDRESSES} за одно применение")
    prefix = _host_prefix(start)
    specs: list[AddressSpec] = []
    for offset in range(count):
        ip = start + offset
        _check_assignable(ip, entry)
        specs.append(AddressSpec(str(ip), prefix))
    return specs


def _expand_network(entry: str) -> list[AddressSpec]:
    try:
        interface = ipaddress.ip_interface(entry)
    except ValueError:
        raise AddressInputError(entry, "не похоже на адрес с маской (например 1.2.3.4/24 или 1.2.3.0/29)")
    network = interface.network
    prefix = network.prefixlen
    whole_subnet = interface.ip == network.network_address
    if interface.version == 6:
        if whole_subnet and prefix < IPV6_HOST_PREFIX:
            raise AddressInputError(
                entry, "подсеть IPv6 не разворачивается — укажите конкретный адрес, например 2001:db8::2/64"
            )
        _check_assignable(interface.ip, entry)
        return [AddressSpec(str(interface.ip), prefix)]
    if not whole_subnet or prefix == IPV4_HOST_PREFIX:
        _check_assignable(interface.ip, entry)
        return [AddressSpec(str(interface.ip), prefix)]
    # Host-биты нулевые — это подсеть целиком: адрес сети как host на интерфейсе
    # бесполезен, а блоки хостеры выдают именно в такой записи
    if prefix > MAX_IPV4_EXPAND_PREFIX:
        hosts = list(network)
    else:
        usable = network.num_addresses - 2
        if usable > MAX_ADDRESSES:
            raise AddressInputError(entry, f"в подсети {usable} адресов, максимум {MAX_ADDRESSES} за одно применение")
        hosts = list(network.hosts())
    for ip in hosts:
        _check_assignable(ip, entry)
    return [AddressSpec(str(ip), prefix) for ip in hosts]


def expand_entry(entry: str) -> list[AddressSpec]:
    entry = entry.strip()
    if "-" in entry:
        return _expand_range(entry)
    if "/" in entry:
        return _expand_network(entry)
    ip = _parse_ip(entry, entry)
    _check_assignable(ip, entry)
    return [AddressSpec(str(ip), _host_prefix(ip))]


def expand_entries(text: str) -> list[AddressSpec]:
    """Весь ввод → список без дублей, в порядке первого появления."""
    entries = split_entries(text)
    if not entries:
        raise AddressInputError("", "список адресов пуст")
    prefixes: dict[str, int] = {}
    result: list[AddressSpec] = []
    for entry in entries:
        for spec in expand_entry(entry):
            known = prefixes.get(spec.address)
            if known is None:
                prefixes[spec.address] = spec.prefix
                result.append(spec)
            elif known != spec.prefix:
                raise AddressInputError(entry, f"адрес {spec.address} указан дважды с разными масками")
    if len(result) > MAX_ADDRESSES:
        raise AddressInputError("", f"слишком много адресов: {len(result)}, максимум {MAX_ADDRESSES} за одно применение")
    return result


def parse_gateway(text: str) -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        raise AddressInputError(text, "шлюз не похож на IP-адрес")
    if any(ip.version == net.version and ip in net for net in UNUSABLE_GATEWAY_NETS):
        raise AddressInputError(text, "этот адрес не может быть шлюзом")
    return str(ip)


def with_gateway(specs: list[AddressSpec], gateway: Optional[str]) -> list[AddressSpec]:
    """Шлюз одного семейства на всю пачку: смешанный список с шлюзом — ошибка,
    а не молчаливый шлюз только для части адресов."""
    if not gateway:
        return specs
    family = AddressFamily.IPV6 if ":" in gateway else AddressFamily.IPV4
    if any(spec.family != family for spec in specs):
        other = "IPv4" if family == AddressFamily.IPV6 else "IPv6"
        raise AddressInputError(
            gateway, f"в списке есть адреса {other}, а шлюз — для другого семейства; добавьте их отдельно"
        )
    if any(spec.address == gateway for spec in specs):
        raise AddressInputError(gateway, "шлюз совпадает с одним из добавляемых адресов")
    return [replace(spec, gateway=gateway) for spec in specs]


def normalize_ref(address: str, prefix: int) -> AddressSpec:
    """Адрес из UI (удаление) в канонический вид — тот же, что у ноды."""
    try:
        interface = ipaddress.ip_interface(f"{address}/{prefix}")
    except ValueError:
        raise AddressInputError(f"{address}/{prefix}", "не похоже на адрес с маской")
    return AddressSpec(str(interface.ip), interface.network.prefixlen)


def preview(text: str, gateway_text: str = "") -> dict:
    specs = with_gateway(expand_entries(text), parse_gateway(gateway_text))
    return {
        "count": len(specs),
        "ipv4": sum(1 for spec in specs if spec.family == AddressFamily.IPV4),
        "ipv6": sum(1 for spec in specs if spec.family == AddressFamily.IPV6),
        "addresses": [{"address": spec.address, "prefix": spec.prefix, "family": spec.family.value} for spec in specs],
    }
