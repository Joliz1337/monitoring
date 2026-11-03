"""GRE-туннели DDoS-Guard: защищённый адрес (CE) приходит на ноду по GRE, ответы
с него уходят обратно в туннель.

Основной маршрут не трогается — по policy routing направляется только трафик с
CE, поэтому применение не может отрезать панель и таймер отката не нужен. Вся
работа с ядром — в host-скрипте (`host_gre.sh` → `/opt/monitoring/scripts/gre-tunnels.sh`)
и его службе `mon-gre.service`: она поднимает туннели после загрузки, держит их без
контейнера агента и возвращает через секунду после сброса сети. Заданные туннели
лежат на хосте (`/opt/monitoring/gre/tunnels.list`); здесь — проверка адресов,
запись списка, включение службы и разбор состояния. Служба включена ровно тогда,
когда список не пуст.
"""

import asyncio
import hashlib
import ipaddress
import logging
import re
from pathlib import Path
from typing import Optional, get_args

from app.models.gre import (
    INTERFACE_PREFIX,
    GreCheckResult,
    GreConfig,
    GreLinkState,
    GreState,
    GreTunnelState,
    interface_name,
)
from app.services.host_executor import HostExecutor
from app.services.host_files import write_host_file
from app.services.rate_sampler import read_net_dev
from app.services.time_sync import parse_key_values

logger = logging.getLogger(__name__)

HOST_SCRIPT = Path(__file__).with_name("host_gre.sh").read_text(encoding="utf-8")
HOST_SCRIPT_PATH = "/opt/monitoring/scripts/gre-tunnels.sh"
CONFIG_PATH = "/opt/monitoring/gre/tunnels.list"
UNIT_NAME = "mon-gre.service"
UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}"

UNIT = """[Unit]
Description=Monitoring node: DDoS-Guard GRE tunnels

[Service]
Type=simple
ExecStart=/opt/monitoring/scripts/gre-tunnels.sh watch
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

COMMAND_TIMEOUT_SEC = 30
# Ниже 25-секундного таймаута панели: пинг ~3 с плюс два запроса по 6 с
CHECK_TIMEOUT_SEC = 20
PING_COUNT = 3
EXTERNAL_IP_URLS = ("https://api.ipify.org", "https://ipv4.icanhazip.com")
HTTPS_MARKER = "@@HTTPS"
LINK_STATES = frozenset(get_args(GreLinkState))
STATUS_COMMAND = (
    f"systemctl is-enabled {UNIT_NAME} 2>/dev/null | sed 's/^/ENABLED=/'; "
    f"systemctl is-active {UNIT_NAME} 2>/dev/null | sed 's/^/ACTIVE=/'; "
    f"{HOST_SCRIPT_PATH} status"
)
PING_SUMMARY = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
PING_RTT = re.compile(r"= [\d.]+/([\d.]+)/")


class GreError(Exception):
    pass


class GreValidationError(GreError):
    pass


def render_config(config: GreConfig) -> str:
    """Строки `tunnels.list`: `<slot> <local> <remote> <pe> <ce> <health_net>`."""
    return "".join(
        f"{t.slot} {t.local} {t.remote} {t.pe} {t.ce} {t.health_net}\n"
        for t in sorted(config.tunnels, key=lambda tunnel: tunnel.slot)
    )


def parse_addresses(text: str) -> dict[str, str]:
    """Вывод `ip -4 -o addr show` → {адрес: интерфейс}."""
    addresses: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        addresses[parts[3].split("/")[0]] = parts[1].split("@")[0]
    return addresses


def host_problems(config: GreConfig, addresses: dict[str, str]) -> list[str]:
    """Что помешает туннелям на этом хосте — до записи списка, чтобы панель
    получила понятный отказ, а не молча неработающий туннель."""
    problems: list[str] = []
    for tunnel in config.tunnels:
        owner = addresses.get(tunnel.local)
        if owner is None or owner.startswith(INTERFACE_PREFIX):
            problems.append(
                f"GRE SRC {tunnel.local} is not on any interface of this server "
                "(a cloud NAT address cannot carry GRE)"
            )
        owner = addresses.get(tunnel.ce)
        if owner is not None and not owner.startswith(INTERFACE_PREFIX):
            problems.append(f"CE {tunnel.ce} is already assigned to {owner}")
    return problems


def parse_status(text: str, counters: dict[str, dict[str, int]]) -> tuple[list[GreTunnelState], bool]:
    """Вывод `gre-tunnels.sh status` и счётчики интерфейсов → (туннели, совпадает ли ядро)."""
    tunnels: list[GreTunnelState] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 10 or parts[0] != "TUN" or not parts[1].isdigit() or parts[7] not in LINK_STATES:
            continue
        slot = int(parts[1])
        name = interface_name(slot)
        stats = counters.get(name, {})
        tunnels.append(GreTunnelState(
            slot=slot,
            name=name,
            local=parts[2],
            remote=parts[3],
            pe=parts[4],
            ce=parts[5],
            health_net=parts[6],
            link=parts[7],
            mtu=int(parts[8]) if parts[8].isdigit() else None,
            conflict_with=None if parts[9] == "-" else parts[9],
            rx_bytes=stats.get("rx_bytes", 0),
            rx_packets=stats.get("rx_packets", 0),
            tx_bytes=stats.get("tx_bytes", 0),
            tx_packets=stats.get("tx_packets", 0),
        ))
    return tunnels, parse_key_values(text).get("IN_SYNC") == "yes"


def check_command(ce: str, pe: str) -> str:
    """Пинг PE с адреса CE и запрос наружу с CE: снаружи он должен быть виден как CE."""
    first, second = EXTERNAL_IP_URLS
    curl = f"curl -4 -sS --noproxy '*' --interface {ce} --connect-timeout 4 --max-time 6"
    return (
        f"ping -n -c {PING_COUNT} -i 0.5 -W 2 -I {ce} {pe} 2>&1; "
        f"echo '{HTTPS_MARKER}'; "
        f"{curl} {first} 2>/dev/null || {curl} {second} 2>&1"
    )


def parse_check(slot: int, text: str) -> GreCheckResult:
    ping_part, _, https_part = text.partition(HTTPS_MARKER)
    result = GreCheckResult(slot=slot)
    summary = PING_SUMMARY.search(ping_part)
    if summary:
        result.ping_sent, result.ping_received = int(summary.group(1)), int(summary.group(2))
    rtt = PING_RTT.search(ping_part)
    if rtt:
        result.rtt_ms = float(rtt.group(1))
    answer = https_part.strip()
    try:
        result.external_ip = str(ipaddress.IPv4Address(answer))
        result.https_ok = True
    except ValueError:
        result.https_error = answer.splitlines()[0][:200] if answer else "no answer"
    return result


class GreManager:
    def __init__(self, executor: HostExecutor):
        self._executor = executor
        self._lock = asyncio.Lock()
        # путь → хэш того, что уже лежит на хосте: состояние поллится, не сверять каждый раз
        self._installed: dict[str, str] = {}
        self._last_error: Optional[str] = None

    async def _ensure_file(self, path: str, content: str, mode: str) -> bool:
        """Файл уезжает на хост при расхождении хэша; True — переписан."""
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if self._installed.get(path) == digest:
            return False
        on_host = await self._executor.execute(
            f"sha256sum {path} 2>/dev/null | cut -d' ' -f1", timeout=10, shell="bash"
        )
        written = on_host.stdout.strip() != digest
        if written and not await write_host_file(path, content, mode=mode):
            raise GreError(f"cannot write {path} on the host")
        self._installed[path] = digest
        return written

    async def _ensure_installed(self) -> bool:
        """Скрипт и юнит; True — что-то поменялось и работающую службу надо перезапустить."""
        script_changed = await self._ensure_file(HOST_SCRIPT_PATH, HOST_SCRIPT, "755")
        unit_changed = await self._ensure_file(UNIT_PATH, UNIT, "644")
        if unit_changed:
            await self._executor.execute("systemctl daemon-reload", timeout=15)
        return script_changed or unit_changed

    async def _is_enabled(self) -> bool:
        result = await self._executor.execute(f"systemctl is-enabled {UNIT_NAME} 2>/dev/null", timeout=10)
        return result.stdout.strip() == "enabled"

    async def _systemctl(self, *args: str) -> None:
        result = await self._executor.execute(f"systemctl {' '.join(args)} {UNIT_NAME}", timeout=COMMAND_TIMEOUT_SEC)
        if not result.success:
            raise GreError(result.stderr.strip() or result.error or f"systemctl {args[0]} failed")

    async def _run_script(self, verb: str) -> Optional[str]:
        """Проход скрипта; текст ошибок отдельных команд ip или None."""
        result = await self._executor.execute(f"{HOST_SCRIPT_PATH} {verb}", timeout=COMMAND_TIMEOUT_SEC, shell="bash")
        errors = result.stderr.strip()
        if errors:
            # Служба повторит на следующем событии ядра; панель увидит причину в состоянии
            logger.warning("gre tunnels: %s reported: %s", verb, errors)
        return errors or None

    async def _host_addresses(self) -> dict[str, str]:
        result = await self._executor.execute("ip -4 -o addr show", timeout=10)
        if not result.success:
            raise GreError(result.stderr.strip() or "ip -4 -o addr show failed")
        return parse_addresses(result.stdout)

    async def state(self) -> GreState:
        try:
            # Только скрипт: юнит ложится на хост вместе с первым туннелем, а не от просмотра
            await self._ensure_file(HOST_SCRIPT_PATH, HOST_SCRIPT, "755")
        except GreError as exc:
            return GreState(supported=False, message=str(exc))
        result = await self._executor.execute(STATUS_COMMAND, timeout=COMMAND_TIMEOUT_SEC, shell="bash")
        values = parse_key_values(result.stdout)
        if "IN_SYNC" not in values:
            reason = result.stderr.strip() or result.error or "gre-tunnels.sh status printed nothing"
            return GreState(supported=False, message=reason)
        tunnels, in_sync = parse_status(result.stdout, read_net_dev())
        return GreState(
            supported=True,
            enabled=values.get("ENABLED") == "enabled",
            active=values.get("ACTIVE") == "active",
            in_sync=in_sync,
            tunnels=tunnels,
            last_error=self._last_error,
        )

    async def apply(self, config: GreConfig) -> GreState:
        async with self._lock:
            problems = host_problems(config, await self._host_addresses())
            if problems:
                raise GreValidationError("; ".join(problems))
            await self._ensure_installed()
            if not await write_host_file(CONFIG_PATH, render_config(config), mode="644"):
                raise GreError(f"cannot write {CONFIG_PATH} on the host")
            if config.tunnels:
                # Первый проход сразу, а не в фоне службы: ответ панели уже показывает итог
                self._last_error = await self._run_script("sync")
                await self._systemctl("enable", "--now")
            else:
                # Сначала служба, иначе она вернула бы снятое
                await self._systemctl("disable", "--now")
                self._last_error = await self._run_script("clear")
        logger.info("gre tunnels: %d configured", len(config.tunnels))
        return await self.state()

    async def check(self, slot: int) -> GreCheckResult:
        tunnel = next((t for t in (await self.state()).tunnels if t.slot == slot), None)
        if tunnel is None:
            raise GreValidationError(f"tunnel {slot} is not configured")
        result = await self._executor.execute(
            check_command(tunnel.ce, tunnel.pe), timeout=CHECK_TIMEOUT_SEC, shell="bash"
        )
        return parse_check(slot, result.stdout)

    async def start(self) -> None:
        """Обновлённый агент привозит новый скрипт — включённая служба переезжает на него."""
        if not await self._is_enabled():
            return
        if await self._ensure_installed():
            await self._systemctl("restart")
            logger.info("gre tunnels: service restarted on the updated script")


_manager: Optional[GreManager] = None


def get_gre_manager() -> GreManager:
    global _manager
    if _manager is None:
        from app.services.host_executor import get_host_executor
        _manager = GreManager(get_host_executor())
    return _manager
