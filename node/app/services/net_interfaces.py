"""Перечисление физических сетевых интерфейсов хоста."""

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
