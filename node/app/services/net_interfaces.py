"""Перечисление сетевых интерфейсов хоста: физические карты для тюнинга NIC и
интерфейсы, способные нести адреса (физические не-слейвы, bond, VLAN, bridge).
"""

from dataclasses import dataclass

# Реальные карты: есть device/, это не bridge и не слейв bond'а. Loopback,
# veth и туннели отсеиваются отсутствием device/.
_LIST_COMMAND = (
    "for dev in /sys/class/net/*/device; do "
    "iface=$(basename $(dirname $dev)); "
    "[ -d /sys/class/net/$iface/bridge ] && continue; "
    "[ -f /sys/class/net/$iface/bonding/slaves ] && continue; "
    "state=$(cat /sys/class/net/$iface/operstate 2>/dev/null); "
    "echo \"$iface $state\"; done 2>/dev/null"
)


# Интерфейс может нести адрес, если он не подчинён другому (у слейва bond'а и
# порта bridge'а есть symlink master) и не служебный (docker/veth/туннели):
# на дедике с bond0 адреса живут на бонде, а не на его физических слейвах.
_ADDRESS_LIST_COMMAND = (
    'for d in /sys/class/net/*; do iface=$(basename "$d"); [ "$iface" = lo ] && continue; '
    'kind=other; [ -d "$d/device" ] && kind=physical; [ -d "$d/bonding" ] && kind=bond; '
    '[ -d "$d/bridge" ] && kind=bridge; [ -f "/proc/net/vlan/$iface" ] && kind=vlan; '
    'enslaved=no; [ -e "$d/master" ] && enslaved=yes; '
    'echo "$iface $(cat "$d/operstate" 2>/dev/null) $kind $enslaved"; done 2>/dev/null'
)
ADDRESS_KINDS = ("physical", "bond", "vlan", "bridge")
VIRTUAL_PREFIXES = (
    "veth", "docker", "br-", "virbr", "flannel", "cni", "cali",
    "wg", "tun", "tap", "warp", "gre", "sit", "ip6tnl",
)


@dataclass(frozen=True)
class InterfaceInfo:
    name: str
    is_up: bool
    kind: str


def parse_interface_listing(text: str) -> list[InterfaceInfo]:
    """Строки `<имя> <operstate> <вид> <подчинён>` → интерфейсы, способные нести адреса."""
    result: list[InterfaceInfo] = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        name, state, kind, enslaved = parts[0], parts[1], parts[2], parts[3]
        if kind not in ADDRESS_KINDS or enslaved == "yes" or name.startswith(VIRTUAL_PREFIXES):
            continue
        result.append(InterfaceInfo(name=name, is_up=state == "up", kind=kind))
    return result


async def list_address_interfaces(executor) -> list[InterfaceInfo]:
    result = await executor.execute(_ADDRESS_LIST_COMMAND, timeout=5, shell="bash")
    if not result.success:
        return []
    return parse_interface_listing(result.stdout)


async def list_physical_interfaces(executor, include_down: bool = False) -> list[tuple[str, bool]]:
    """[(имя, поднят ли линк)] — с DOWN-картами только по запросу: трафика они не несут."""
    result = await executor.execute(_LIST_COMMAND, timeout=5, shell="bash")
    if not (result.success and result.stdout.strip()):
        return []
    interfaces: list[tuple[str, bool]] = []
    for line in result.stdout.strip().splitlines():
        name, _, state = line.strip().partition(" ")
        if not name:
            continue
        is_up = state.strip() == "up"
        if is_up or include_down:
            interfaces.append((name, is_up))
    return interfaces
