"""Дополнительные IP-адреса интерфейса — транзакция с таймером отката на хосте.

Хостер выдал ноде ещё один адрес (или блок адресов), и его нужно повесить на
интерфейс так, чтобы он пережил ребут. Формат сетевого конфига у каждого
хостера свой (netplan, systemd-networkd, NetworkManager, ifupdown), а ошибка в
нём означает потерю сервера. Поэтому изменение идёт транзакцией: бэкап →
запись конфига → живое применение → проверка → таймер отката. Панель
подтверждает транзакцию, заново достучавшись до ноды; нет подтверждения к
дедлайну — хост сам восстанавливает бэкап, а незавершённую транзакцию при
перезагрузке откатывает boot-guard ещё до старта сети.

Разделение труда: всё, что обязано работать без контейнера (таймер, откат,
boot-guard), живёт в bash-скрипте на хосте (`host_extra_ips.sh` →
`/opt/monitoring/scripts/extra-ips.sh`); здесь — валидация, детект бэкенда,
рендер конфигов и разбор состояния. Скрипт конфиги не рендерит: он получает
готовый план (файлы в base64 + списки адресов).

Состояние — строчные файлы в `/opt/monitoring/network/` (каталог примонтирован
в контейнер только на чтение, пишет их скрипт): `managed.list` (наши адреса),
`transaction.env` (текущая транзакция), `history.log`, `backups/<tx>/`.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from app.models.network import (
    AddressSpec,
    InterfaceState,
    LiveAddress,
    ManagedAddress,
    NetworkActionResponse,
    NetworkApplyRequest,
    NetworkApplyResponse,
    NetworkStateResponse,
    TransactionInfo,
)
from app.services.cpu_affinity import default_interface
from app.services.host_executor import ExecuteResult, HostExecutor
from app.services.host_files import read_host_file_exact, write_host_file
from app.services.net_interfaces import InterfaceInfo, list_address_interfaces
from app.services.time_sync import parse_key_values

logger = logging.getLogger(__name__)

HOST_SCRIPT = Path(__file__).with_name("host_extra_ips.sh").read_text(encoding="utf-8")
HOST_SCRIPT_PATH = "/opt/monitoring/scripts/extra-ips.sh"
GUARD_UNIT_NAME = "mon-extra-ips-guard.service"
PERSIST_UNIT_NAME = "mon-extra-ips.service"
GUARD_UNIT_PATH = f"/etc/systemd/system/{GUARD_UNIT_NAME}"
PERSIST_UNIT_PATH = f"/etc/systemd/system/{PERSIST_UNIT_NAME}"

# Wants=network-pre.target обязателен: target пассивный, без него Before= не
# упорядочивает. DefaultDependencies=no — иначе юнит сам встал бы после
# sysinit/basic и опоздал к старту сети.
GUARD_UNIT = """[Unit]
Description=Monitoring node: roll back an unconfirmed extra-IP transaction before the network starts
DefaultDependencies=no
After=local-fs.target
Before=network-pre.target
Wants=network-pre.target
ConditionPathExists=/opt/monitoring/network/transaction.env

[Service]
Type=oneshot
ExecStart=/opt/monitoring/scripts/extra-ips.sh boot-guard

[Install]
WantedBy=sysinit.target
"""

PERSIST_UNIT = """[Unit]
Description=Monitoring node: re-add panel-managed extra IP addresses (hosts without a config backend)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/opt/monitoring/scripts/extra-ips.sh restore-runtime

[Install]
WantedBy=multi-user.target
"""

STATE_DIR = Path("/opt/monitoring/network")
MANAGED_FILE_NAME = "managed.list"
TRANSACTION_FILE_NAME = "transaction.env"
HISTORY_FILE_NAME = "history.log"
SYS_CLASS_NET = Path("/sys/class/net")

NETPLAN_FILE = "/etc/netplan/60-monitoring-extra-ips.yaml"
NETPLAN_SECTIONS = ("ethernets", "bonds", "vlans", "bridges")
NETWORKD_DROPIN_NAME = "monitoring-extra-ips.conf"
IFUPDOWN_FILE = "/etc/network/interfaces"
IFUPDOWN_DROPIN = "/etc/network/interfaces.d/monitoring-extra-ips"
IFUPDOWN_BLOCK_BEGIN = "# --- monitoring-extra-ips begin (managed by monitoring node, do not edit) ---"
IFUPDOWN_BLOCK_END = "# --- monitoring-extra-ips end ---"
MANAGED_HEADER = "# Managed by the monitoring node (extra IP addresses). Do not edit: the panel rewrites this file."

# `netplan apply` на медленном хосте — до ~30 с, проверка DAD — до 10 с, плюс бэкап и таймер
APPLY_TIMEOUT_SEC = 150
CONTROL_TIMEOUT_SEC = 60
DETECT_TIMEOUT_SEC = 20
DETECT_CACHE_SEC = 60
HISTORY_LIMIT = 20
TX_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")

EXIT_BUSY = 3
EXIT_ROLLED_BACK = 4
EXIT_ROLLBACK_FAILED = 5


class ExtraIpValidationError(Exception):
    pass


class ExtraIpUnsupportedError(Exception):
    pass


class ExtraIpBusyError(Exception):
    def __init__(self, transaction_id: str):
        super().__init__(f"transaction {transaction_id} awaits confirmation")
        self.transaction_id = transaction_id


class BackendKind(str, Enum):
    NETPLAN = "netplan"
    NETWORKD = "networkd"
    NETWORKMANAGER = "networkmanager"
    IFUPDOWN = "ifupdown"
    FALLBACK = "fallback"


@dataclass
class Backend:
    kind: BackendKind
    detail: str = ""
    netplan_definitions: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    networkd_file: str = ""
    nm_connection: str = ""
    nm_keyfile: str = ""
    nm_ipv6_method: str = ""
    ifupdown_sourced: bool = False


@dataclass
class LiveAddr:
    address: str
    prefix: int
    family: str
    scope: str
    dynamic: bool = False

    @property
    def cidr(self) -> str:
        return f"{self.address}/{self.prefix}"


@dataclass
class LiveInterface:
    name: str
    addresses: list[LiveAddr] = field(default_factory=list)


@dataclass
class Transaction:
    id: str
    status: str
    interface: str = ""
    backend: str = ""
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    started_at: Optional[int] = None
    deadline_at: Optional[int] = None
    finished_at: Optional[int] = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_info(self) -> TransactionInfo:
        return TransactionInfo(
            id=self.id,
            status=self.status,
            interface=self.interface,
            backend=self.backend,
            added=list(self.added),
            removed=list(self.removed),
            started_at=iso_utc(self.started_at),
            deadline_at=iso_utc(self.deadline_at),
            finished_at=iso_utc(self.finished_at),
            message=self.message,
            warnings=list(self.warnings),
        )


@dataclass
class PlanFile:
    path: str
    mode: str
    content: Optional[str]  # None — файла быть не должно


# ------------------------------------------------------------- разбор `ip -j`


def iso_utc(timestamp: Optional[int]) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _to_int(value: str) -> Optional[int]:
    return int(value) if value.isdigit() else None


def parse_ip_addr(text: str) -> dict[str, LiveInterface]:
    """`ip -j addr show` → интерфейсы с адресами; loopback- и link-local-адреса не показываются."""
    try:
        entries = json.loads(text or "[]")
    except ValueError:
        return {}
    interfaces: dict[str, LiveInterface] = {}
    for entry in entries:
        name = entry.get("ifname")
        if not name:
            continue
        addresses: list[LiveAddr] = []
        for info in entry.get("addr_info") or []:
            scope = str(info.get("scope", ""))
            if scope in ("host", "link") or not info.get("local"):
                continue
            addresses.append(LiveAddr(
                address=str(info["local"]),
                prefix=int(info.get("prefixlen", 32)),
                family="ipv6" if info.get("family") == "inet6" else "ipv4",
                scope=scope,
                dynamic=bool(info.get("dynamic")),
            ))
        interfaces[name] = LiveInterface(name=name, addresses=addresses)
    return interfaces


def parse_default_routes(json4: str, json6: str) -> dict[str, tuple[str, str]]:
    """family → (интерфейс default-маршрута, prefsrc или '')."""
    routes: dict[str, tuple[str, str]] = {}
    for family, text in (("ipv4", json4), ("ipv6", json6)):
        try:
            entries = json.loads(text or "[]")
        except ValueError:
            continue
        for entry in entries:
            # Multipath-маршрут (два шлюза на bond0) держит dev внутри nexthops
            nexthops = entry.get("nexthops") or [{}]
            dev = entry.get("dev") or nexthops[0].get("dev")
            if dev:
                routes[family] = (str(dev), str(entry.get("prefsrc") or ""))
                break
    return routes


def primary_addresses(iface: LiveInterface, routes: dict[str, tuple[str, str]], managed: set[str]) -> set[str]:
    """Основной адрес каждого семейства: prefsrc default-маршрута, иначе первый
    global-адрес, который не добавляла панель."""
    primary: set[str] = set()
    for family in ("ipv4", "ipv6"):
        candidates = [addr for addr in iface.addresses if addr.family == family]
        route = routes.get(family)
        if route and route[0] == iface.name and route[1]:
            matched = [addr for addr in candidates if addr.address == route[1]]
            if matched:
                primary.add(matched[0].cidr)
                continue
        for addr in candidates:
            if addr.scope == "global" and addr.cidr not in managed:
                primary.add(addr.cidr)
                break
    return primary


# ----------------------------------------------------------- файлы состояния


def parse_managed(text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        iface, _, cidr = line.strip().partition(" ")
        if iface and cidr and (iface, cidr.strip()) not in entries:
            entries.append((iface, cidr.strip()))
    return entries


def render_managed(entries: list[tuple[str, str]]) -> str:
    return "".join(f"{iface} {cidr}\n" for iface, cidr in entries)


def _split_list(value: str, separator: str = " ") -> list[str]:
    return [item for item in value.split(separator) if item and item != "-"]


def parse_transaction(text: str) -> Optional[Transaction]:
    values = parse_key_values(text or "")
    tx_id = values.get("TX_ID", "")
    if not tx_id:
        return None
    return Transaction(
        id=tx_id,
        status=values.get("TX_STATUS", ""),
        interface=values.get("TX_IFACE", ""),
        backend=values.get("TX_BACKEND", ""),
        added=_split_list(values.get("TX_ADD", "")),
        removed=_split_list(values.get("TX_REMOVE", "")),
        started_at=_to_int(values.get("TX_STARTED_AT", "")),
        deadline_at=_to_int(values.get("TX_DEADLINE_AT", "")),
        finished_at=_to_int(values.get("TX_FINISHED_AT", "")),
        message=values.get("TX_MESSAGE", ""),
        warnings=_split_list(values.get("TX_WARNINGS", ""), "; "),
    )


def parse_history(text: str, limit: int = HISTORY_LIMIT) -> list[Transaction]:
    """TSV-строки history.log, новые сверху; битые строки пропускаются."""
    history: list[Transaction] = []
    for line in reversed((text or "").splitlines()):
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 9 or not parts[2]:
            continue
        history.append(Transaction(
            id=parts[2],
            status=parts[3],
            interface=parts[4],
            backend=parts[5],
            added=_split_list(parts[6], ","),
            removed=_split_list(parts[7], ","),
            started_at=_to_int(parts[0]),
            finished_at=_to_int(parts[1]),
            message="\t".join(parts[8:]),
        ))
        if len(history) >= limit:
            break
    return history


# ------------------------------------------------------------ детект бэкенда


def parse_netplan_definitions(text: str) -> dict[str, dict[str, dict[str, str]]]:
    """Минимальный разбор `netplan get network` по отступам: раздел → id →
    {set-name, macaddress}. PyYAML в образе нет, а больше ничего и не нужно."""
    definitions: dict[str, dict[str, dict[str, str]]] = {}
    stack: list[tuple[int, str]] = []
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("- "):
            continue
        key, separator, value = stripped.partition(":")
        if not separator:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key.strip().strip("\"'")))
        path = [name for _, name in stack]
        if path and path[0] == "network":
            path = path[1:]
        if len(path) < 2 or path[0] not in NETPLAN_SECTIONS:
            continue
        props = definitions.setdefault(path[0], {}).setdefault(path[1], {})
        value = value.strip().strip("\"'")
        if len(path) == 3 and path[2] == "set-name":
            props["set-name"] = value
        elif len(path) == 4 and path[2] == "match" and path[3] == "macaddress":
            props["macaddress"] = value.lower()
    return definitions


def resolve_netplan_definition(
    definitions: dict[str, dict[str, dict[str, str]]], iface: str, mac: str
) -> Optional[tuple[str, str]]:
    """(раздел, id) определения, которое описывает интерфейс. Ключ определения не
    обязан совпадать с именем (cloud-init пишет `id0:` + `match.macaddress` +
    `set-name`), а файл с ключом `eth0:` создал бы второе определение того же
    устройства — networkd применил бы только одно из них."""
    for section, ids in definitions.items():
        for ident, props in ids.items():
            if props.get("set-name") == iface:
                return section, ident
    for section, ids in definitions.items():
        if iface in ids:
            return section, iface
    if mac:
        for section, ids in definitions.items():
            for ident, props in ids.items():
                if props.get("macaddress") == mac.lower():
                    return section, ident
    return None


def choose_backend(facts: dict[str, str], iface: str, mac: str) -> Backend:
    """netplan → networkd → NetworkManager → ifupdown → fallback. netplan первым:
    если он описывает интерфейс, нижние слои он же и перегенерирует."""
    if facts.get("NETPLAN") == "yes":
        try:
            merged = base64.b64decode(facts.get("NETPLAN_GET_B64", "")).decode("utf-8", errors="replace")
        except ValueError:
            merged = ""
        definitions = parse_netplan_definitions(merged)
        found = resolve_netplan_definition(definitions, iface, mac)
        if found:
            return Backend(BackendKind.NETPLAN, detail=f"{found[0]}/{found[1]}", netplan_definitions=definitions)
    network_file = facts.get("NETWORKD_FILE", "")
    if network_file.startswith("/etc/systemd/network/"):
        return Backend(BackendKind.NETWORKD, detail=network_file, networkd_file=network_file)
    if facts.get("NM_CONNECTION") and facts.get("NM_KEYFILE"):
        return Backend(
            BackendKind.NETWORKMANAGER,
            detail=facts["NM_CONNECTION"],
            nm_connection=facts["NM_CONNECTION"],
            nm_keyfile=facts["NM_KEYFILE"],
            nm_ipv6_method=facts.get("NM_IPV6_METHOD", ""),
        )
    if facts.get("IFUPDOWN") == "yes":
        sourced = facts.get("IFUPDOWN_SOURCED") == "yes"
        return Backend(
            BackendKind.IFUPDOWN,
            detail=IFUPDOWN_DROPIN if sourced else IFUPDOWN_FILE,
            ifupdown_sourced=sourced,
        )
    return Backend(BackendKind.FALLBACK, detail=PERSIST_UNIT_NAME)


# ------------------------------------------------------------ рендер конфигов


def render_netplan(addresses: dict[tuple[str, str], list[str]]) -> str:
    """Отдельный файл только с нашими адресами: netplan конкатенирует списки
    `addresses` из разных файлов, а cloud-init перезаписывает лишь свой."""
    lines = [MANAGED_HEADER, "network:", "  version: 2"]
    for section in NETPLAN_SECTIONS:
        entries = {ident: addrs for (sec, ident), addrs in addresses.items() if sec == section and addrs}
        if not entries:
            continue
        lines.append(f"  {section}:")
        for ident in sorted(entries):
            lines.append(f"    {ident}:")
            lines.append("      addresses:")
            lines.extend(f'        - "{cidr}"' for cidr in entries[ident])
    return "\n".join(lines) + "\n"


def networkd_dropin_path(network_file: str) -> str:
    return f"{network_file}.d/{NETWORKD_DROPIN_NAME}"


def render_networkd_dropin(addresses: list[str]) -> str:
    lines = [MANAGED_HEADER, "[Network]"]
    lines.extend(f"Address={cidr}" for cidr in addresses)
    return "\n".join(lines) + "\n"


def render_ifupdown_stanzas(addresses_by_iface: dict[str, list[str]]) -> str:
    """Стансы `iface X inet/inet6 static` по адресу: ifupdown применяет все стансы
    интерфейса, а alias-имена `eth0:N` — наследие ifconfig."""
    lines: list[str] = []
    for iface in sorted(addresses_by_iface):
        for cidr in addresses_by_iface[iface]:
            family = "inet6" if ":" in cidr else "inet"
            lines.append(f"iface {iface} {family} static")
            lines.append(f"    address {cidr}")
    return "\n".join(lines) + ("\n" if lines else "")


def splice_ifupdown_block(interfaces_text: str, stanzas: str) -> str:
    """Заменить/добавить/убрать огороженный блок в конце /etc/network/interfaces,
    не трогая ни байта вне него."""
    pattern = re.compile(
        r"\n*" + re.escape(IFUPDOWN_BLOCK_BEGIN) + r"\n.*?" + re.escape(IFUPDOWN_BLOCK_END) + r"\n?",
        re.DOTALL,
    )
    base = pattern.sub("", interfaces_text or "", count=1).rstrip("\n")
    if not stanzas:
        return base + "\n" if base else ""
    block = f"{IFUPDOWN_BLOCK_BEGIN}\n{stanzas}{IFUPDOWN_BLOCK_END}\n"
    return f"{base}\n\n{block}" if base else block


# ---------------------------------------------------------- проверки и план


def check_request(
    request: NetworkApplyRequest,
    interfaces: dict[str, LiveInterface],
    physical: dict[str, bool],
    managed: list[tuple[str, str]],
    primary: set[str],
) -> tuple[list[AddressSpec], list[AddressSpec]]:
    """Guard'ы до любого касания хоста. Возвращает (add, remove) без адресов,
    которые уже стоят и наши."""
    iface = request.interface
    if iface not in physical:
        raise ExtraIpValidationError(
            f"'{iface}' cannot carry addresses on this host (needs a physical NIC that is not enslaved, a bond, a VLAN or a bridge)"
        )
    if not physical[iface]:
        raise ExtraIpValidationError(f"interface '{iface}' is down")
    live = interfaces.get(iface) or LiveInterface(name=iface)
    present = {addr.cidr for addr in live.addresses}
    present_ips = {addr.address for addr in live.addresses}
    managed_here = {cidr for name, cidr in managed if name == iface}
    protected = set(request.protected)

    for spec in request.remove:
        if spec.cidr not in managed_here:
            raise ExtraIpValidationError(
                f"{spec.cidr} is not managed by the panel (configured by the hoster or the system)"
            )
        if spec.address in protected:
            raise ExtraIpValidationError(f"{spec.cidr} is the address the panel uses to reach this node")
        if spec.cidr in primary:
            raise ExtraIpValidationError(f"{spec.cidr} is the primary address of {iface}")

    add: list[AddressSpec] = []
    for spec in request.add:
        if spec.cidr in managed_here and spec.cidr in present:
            continue
        if spec.address in present_ips:
            raise ExtraIpValidationError(f"{spec.address} is already configured on {iface} (not by the panel)")
        for name, other in interfaces.items():
            if name != iface and any(addr.address == spec.address for addr in other.addresses):
                raise ExtraIpValidationError(f"{spec.address} is already configured on {name}")
        add.append(spec)

    if not add and not request.remove:
        raise ExtraIpValidationError("nothing to do: all addresses are already configured")
    return add, list(request.remove)


def new_transaction_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{secrets.token_hex(2)}"


def build_plan(
    tx_id: str,
    iface: str,
    backend: Backend,
    add: list[AddressSpec],
    remove: list[AddressSpec],
    protected: list[str],
    timeout_sec: int,
    managed_text: str,
    files: list[PlanFile],
) -> str:
    """Текст плана для `extra-ips.sh apply` — KEY=value, файлы в base64."""
    lines = [
        f"TX_ID={tx_id}",
        f"IFACE={iface}",
        f"BACKEND={backend.kind.value}",
        f"DETAIL={backend.detail}",
        f"TIMEOUT={timeout_sec}",
        f"ADD={' '.join(spec.cidr for spec in add)}",
        f"REMOVE={' '.join(spec.cidr for spec in remove)}",
        f"PROTECTED={' '.join(protected)}",
        f"MANAGED_B64={_b64(managed_text)}",
    ]
    if backend.kind == BackendKind.NETWORKMANAGER:
        lines.append(f"NM_CONNECTION={backend.nm_connection}")
        lines.append(f"NM_KEYFILE={backend.nm_keyfile}")
    for plan_file in files:
        if plan_file.content is None:
            lines.append(f"ABSENT={plan_file.path}")
        else:
            lines.append(f"FILE={plan_file.mode} {plan_file.path} {_b64(plan_file.content or chr(10))}")
    return "\n".join(lines) + "\n"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def parse_apply_output(result: ExecuteResult, backend: BackendKind) -> NetworkApplyResponse:
    values = parse_key_values(result.stdout)
    message = values.get("TX_MESSAGE", "")
    warnings = _split_list(values.get("TX_WARNINGS", ""), "; ")
    deadline = iso_utc(_to_int(values.get("TX_DEADLINE_AT", "")))
    common = {
        "transaction_id": values.get("TX_ID") or None,
        "status": values.get("TX_STATUS") or None,
        "backend": backend.value,
        "error_log": result.stderr,
        "warnings": warnings,
    }
    if result.error:
        return NetworkApplyResponse(success=False, message=result.error, **common)
    if result.exit_code == 0:
        return NetworkApplyResponse(
            success=True, message=message or "applied, awaiting confirmation", deadline_at=deadline, **common
        )
    stderr_tail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
    if result.exit_code == EXIT_ROLLED_BACK:
        return NetworkApplyResponse(success=False, rolled_back=True, message=message or stderr_tail or "apply failed, rolled back", **common)
    if result.exit_code == EXIT_ROLLBACK_FAILED:
        return NetworkApplyResponse(success=False, message=message or stderr_tail or "apply failed and rollback is incomplete", **common)
    return NetworkApplyResponse(success=False, message=stderr_tail or message or f"exit code {result.exit_code}", **common)


# ------------------------------------------------------------------ менеджер


class ExtraIpManager:
    def __init__(self, executor: HostExecutor, state_dir: Path = STATE_DIR):
        self._executor = executor
        self._state_dir = state_dir
        self._lock = asyncio.Lock()
        self._installed_hash: Optional[str] = None
        self._support: tuple[bool, str] = (True, "")
        self._detect_cache: dict[str, tuple[float, Optional[Backend]]] = {}

    # ── состояние с диска (ro-mount) ──

    def _read(self, name: str) -> str:
        try:
            return (self._state_dir / name).read_text(encoding="utf-8")
        except OSError:
            return ""

    def read_managed(self) -> list[tuple[str, str]]:
        return parse_managed(self._read(MANAGED_FILE_NAME))

    def read_transaction(self) -> Optional[Transaction]:
        return parse_transaction(self._read(TRANSACTION_FILE_NAME))

    def read_history(self) -> list[Transaction]:
        return parse_history(self._read(HISTORY_FILE_NAME))

    # ── установка скрипта и юнитов ──

    async def ensure_installed(self) -> None:
        """Скрипт и юниты уезжают на хост при расхождении хэша/содержимого —
        обновлённый образ не работает со старым скриптом."""
        digest = hashlib.sha256(HOST_SCRIPT.encode("utf-8")).hexdigest()
        if self._installed_hash == digest:
            return
        on_host = await self._executor.execute(
            f"sha256sum {HOST_SCRIPT_PATH} 2>/dev/null | cut -d' ' -f1", timeout=10, shell="bash"
        )
        if on_host.stdout.strip() != digest:
            if not await write_host_file(HOST_SCRIPT_PATH, HOST_SCRIPT, mode="755"):
                raise ExtraIpUnsupportedError("cannot install extra-ips.sh on the host")
        units_changed = False
        for path, content in ((GUARD_UNIT_PATH, GUARD_UNIT), (PERSIST_UNIT_PATH, PERSIST_UNIT)):
            if await read_host_file_exact(path) != content:
                if not await write_host_file(path, content, mode="644"):
                    raise ExtraIpUnsupportedError(f"cannot write {path}")
                units_changed = True
        if units_changed:
            await self._executor.execute("systemctl daemon-reload", timeout=15)
        await self._executor.execute(f"systemctl enable {GUARD_UNIT_NAME}", timeout=15)
        self_test = await self._run("self-test", timeout=15)
        if not (self_test.success and parse_key_values(self_test.stdout).get("SELFTEST") == "ok"):
            reason = self_test.stderr.strip() or self_test.error or "self-test failed"
            self._support = (False, reason)
            raise ExtraIpUnsupportedError(reason)
        self._support = (True, "")
        self._installed_hash = digest

    async def _run(self, verb: str, *args: str, timeout: int = CONTROL_TIMEOUT_SEC) -> ExecuteResult:
        command = " ".join([HOST_SCRIPT_PATH, verb, *(shlex.quote(arg) for arg in args)])
        return await self._executor.execute(command, timeout=timeout, shell="bash")

    # ── живые данные ──

    async def _live(self) -> tuple[dict[str, LiveInterface], dict[str, tuple[str, str]], list[InterfaceInfo]]:
        """Адреса и default-маршруты хоста. Маршруты читаются отдельно и без
        влияния на код выхода: на хосте с `ipv6.disable=1` команда `ip -6 route`
        падает, и раньше это молча обнуляло весь список адресов."""
        addr = await self._executor.execute("ip -j addr show", timeout=10)
        if not (addr.success and addr.stdout.strip()):
            reason = addr.stderr.strip() or addr.error or "empty output"
            raise ExtraIpUnsupportedError(f"ip -j addr show failed on the host: {reason}")
        interfaces = parse_ip_addr(addr.stdout)
        if not interfaces:
            raise ExtraIpUnsupportedError("ip -j addr show returned no interfaces (iproute2 without JSON support?)")
        routes_raw = await self._executor.execute(
            "ip -j -4 route show default 2>/dev/null; echo '@@'; ip -j -6 route show default 2>/dev/null; true",
            timeout=10, shell="bash",
        )
        parts = routes_raw.stdout.split("@@")
        routes = parse_default_routes(parts[0] if parts else "", parts[1] if len(parts) > 1 else "")
        candidates = await list_address_interfaces(self._executor)
        return interfaces, routes, candidates

    @staticmethod
    def _mac(iface: str) -> str:
        try:
            return (SYS_CLASS_NET / iface / "address").read_text().strip()
        except OSError:
            return ""

    async def _detect(self, iface: str) -> Optional[Backend]:
        """Бэкенд интерфейса; None — скрипт ещё не установлен или детект упал.
        Кэш на минуту: панель поллит состояние каждые 3 с во время транзакции."""
        cached = self._detect_cache.get(iface)
        if cached and time.monotonic() - cached[0] < DETECT_CACHE_SEC:
            return cached[1]
        result = await self._run("detect", iface, timeout=DETECT_TIMEOUT_SEC)
        backend = choose_backend(parse_key_values(result.stdout), iface, self._mac(iface)) if result.success else None
        self._detect_cache[iface] = (time.monotonic(), backend)
        return backend

    async def state(self) -> NetworkStateResponse:
        try:
            interfaces, routes, candidates = await self._live()
        except ExtraIpUnsupportedError as exc:
            logger.warning("extra ips: cannot read host addresses: %s", exc)
            return NetworkStateResponse(
                supported=False, message=str(exc), default_interface=default_interface(),
                interfaces=[], managed=[], transaction=None, history=[],
            )
        managed = self.read_managed()
        default = default_interface()
        states: list[InterfaceState] = []
        for candidate in sorted(candidates, key=lambda item: (item.name != default, item.name)):
            name, is_up = candidate.name, candidate.is_up
            live = interfaces.get(name) or LiveInterface(name=name)
            managed_here = {cidr for owner, cidr in managed if owner == name}
            primary = primary_addresses(live, routes, managed_here)
            states.append(InterfaceState(
                name=name,
                is_up=is_up,
                is_default=name == default,
                kind=candidate.kind,
                addresses=[
                    LiveAddress(
                        address=addr.address, prefix=addr.prefix, family=addr.family, scope=addr.scope,
                        managed=addr.cidr in managed_here, primary=addr.cidr in primary, dynamic=addr.dynamic,
                    )
                    for addr in live.addresses
                ],
            ))
        backend = await self._detect(default) if default else None
        transaction = self.read_transaction()
        supported, message = self._support
        return NetworkStateResponse(
            supported=supported,
            message=message or None,
            backend=backend.kind.value if backend else None,
            backend_detail=backend.detail if backend else "",
            default_interface=default,
            interfaces=states,
            managed=[ManagedAddress(interface=owner, address=cidr.split("/")[0], prefix=int(cidr.split("/")[1]))
                     for owner, cidr in managed],
            transaction=transaction.to_info() if transaction else None,
            history=[entry.to_info() for entry in self.read_history()],
        )

    # ── транзакция ──

    async def _render_files(self, backend: Backend, iface: str, managed_after: list[tuple[str, str]]) -> list[PlanFile]:
        by_iface: dict[str, list[str]] = {}
        for owner, cidr in managed_after:
            by_iface.setdefault(owner, []).append(cidr)
        if backend.kind == BackendKind.NETPLAN:
            grouped: dict[tuple[str, str], list[str]] = {}
            for name, addrs in by_iface.items():
                found = resolve_netplan_definition(backend.netplan_definitions, name, self._mac(name))
                if found:
                    grouped[found] = addrs
            return [PlanFile(NETPLAN_FILE, "600", render_netplan(grouped) if grouped else None)]
        if backend.kind == BackendKind.NETWORKD:
            addrs = by_iface.get(iface, [])
            return [PlanFile(networkd_dropin_path(backend.networkd_file), "644",
                             render_networkd_dropin(addrs) if addrs else None)]
        if backend.kind == BackendKind.IFUPDOWN:
            stanzas = render_ifupdown_stanzas(by_iface)
            if backend.ifupdown_sourced:
                return [PlanFile(IFUPDOWN_DROPIN, "644", f"{MANAGED_HEADER}\n{stanzas}" if stanzas else None)]
            current = await read_host_file_exact(IFUPDOWN_FILE) or ""
            return [PlanFile(IFUPDOWN_FILE, "644", splice_ifupdown_block(current, stanzas))]
        return []

    async def apply(self, request: NetworkApplyRequest) -> NetworkApplyResponse:
        async with self._lock:
            await self.ensure_installed()
            current = self.read_transaction()
            if current and current.status in ("pending", "applying"):
                raise ExtraIpBusyError(current.id)

            interfaces, routes, candidates = await self._live()
            physical = {candidate.name: candidate.is_up for candidate in candidates}
            backend = await self._detect(request.interface)
            if backend is None:
                raise ExtraIpValidationError(f"cannot detect the network backend of {request.interface}")
            managed = self.read_managed()
            managed_here = {cidr for owner, cidr in managed if owner == request.interface}
            live = interfaces.get(request.interface) or LiveInterface(name=request.interface)
            primary = primary_addresses(live, routes, managed_here)
            add, remove = check_request(request, interfaces, physical, managed, primary)
            if (backend.kind == BackendKind.NETWORKMANAGER and backend.nm_ipv6_method in ("disabled", "ignore")
                    and any(spec.family == "ipv6" for spec in add)):
                raise ExtraIpValidationError(
                    "IPv6 is disabled in the NetworkManager connection; enable it before adding IPv6 addresses"
                )

            removed = {spec.cidr for spec in remove}
            managed_after = [(owner, cidr) for owner, cidr in managed
                             if not (owner == request.interface and cidr in removed)]
            managed_after.extend((request.interface, spec.cidr) for spec in add)
            files = await self._render_files(backend, request.interface, managed_after)
            plan = build_plan(
                new_transaction_id(), request.interface, backend, add, remove, request.protected,
                request.rollback_timeout_sec, render_managed(managed_after), files,
            )
            result = await self._executor.execute(
                f"printf '%s' '{_b64(plan)}' | base64 -d | {HOST_SCRIPT_PATH} apply",
                timeout=APPLY_TIMEOUT_SEC, shell="bash",
            )
            self._detect_cache.pop(request.interface, None)
            if result.exit_code == EXIT_BUSY:
                busy = self.read_transaction()
                raise ExtraIpBusyError(busy.id if busy else "unknown")
            response = parse_apply_output(result, backend.kind)
            logger.info(
                "extra ips apply: iface=%s backend=%s add=%s remove=%s status=%s",
                request.interface, backend.kind.value, [s.cidr for s in add], [s.cidr for s in remove], response.status,
            )
            return response

    async def confirm(self, transaction_id: str) -> NetworkActionResponse:
        async with self._lock:
            result = await self._run("confirm", transaction_id)
            values = parse_key_values(result.stdout)
            if result.success:
                return NetworkActionResponse(success=True, status=values.get("TX_STATUS"), message=values.get("TX_MESSAGE", ""))
            raise ExtraIpValidationError(result.stderr.strip() or result.error or "confirm failed")

    async def rollback(self, transaction_id: str) -> NetworkActionResponse:
        async with self._lock:
            result = await self._run("rollback", transaction_id, timeout=APPLY_TIMEOUT_SEC)
            values = parse_key_values(result.stdout)
            if result.exit_code == EXIT_ROLLBACK_FAILED:
                return NetworkActionResponse(
                    success=False, status=values.get("TX_STATUS"),
                    message=values.get("TX_MESSAGE") or "rollback is incomplete, check the interface by hand",
                )
            if not result.success:
                raise ExtraIpValidationError(result.stderr.strip() or result.error or "rollback failed")
            return NetworkActionResponse(success=True, status=values.get("TX_STATUS"), message=values.get("TX_MESSAGE", ""))

    async def start(self) -> None:
        """Страховка на старте агента: транзакция, зависшая в `applying` (контейнер
        убили посреди apply) или просроченная `pending` (nohup-таймер погиб вместе
        с контейнером), откатывается здесь."""
        transaction = self.read_transaction()
        if not transaction:
            return
        expired = transaction.status == "pending" and transaction.deadline_at is not None \
            and time.time() > transaction.deadline_at + 5
        if transaction.status != "applying" and not expired:
            return
        await self.ensure_installed()
        result = await self._run("rollback-unconfirmed", transaction.id, timeout=APPLY_TIMEOUT_SEC)
        logger.warning(
            "extra ips: transaction %s was %s at agent start — rollback %s",
            transaction.id, transaction.status, "done" if result.success else f"failed: {result.stderr}",
        )


_manager: Optional[ExtraIpManager] = None


def get_extra_ip_manager() -> ExtraIpManager:
    global _manager
    if _manager is None:
        from app.services.host_executor import get_host_executor
        _manager = ExtraIpManager(get_host_executor())
    return _manager
