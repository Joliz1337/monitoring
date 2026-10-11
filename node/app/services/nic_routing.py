"""Ответы через свою карту: адрес дополнительной карты отвечает через её шлюз.

Облако пропускает с порта ВМ только пакеты с адресом этого порта. При одном
default в main ответ с адреса второй карты уходит через первую, и облако его
отбрасывает: адрес «не отвечает», хотя запрос до ВМ доходит. Каждой карте,
кроме карты основного default, — своя таблица, её адресам — правила
`from <адрес>`. Карта основного default не трогается: через неё панель ходит
на ноду, поэтому включение не может отрезать панель и таймер отката не нужен.

Вся логика — в host-скрипте (`host_nic_routing.sh` →
`/opt/monitoring/scripts/nic-routing.sh`) и его службе `mon-nic-routing.service`:
она следит за событиями ядра и возвращает правила через секунду после
перезапуска networkd и после загрузки, даже когда контейнера агента нет. Здесь —
установка, включение и разбор состояния. Включено = служба включена, отдельного
файла состояния нет.
"""

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Optional, get_args

from app.models.network import NicRoutingInterface, NicRoutingRole, NicRoutingState
from app.services.host_executor import HostExecutor
from app.services.host_files import write_host_file
from app.services.time_sync import parse_key_values

logger = logging.getLogger(__name__)

HOST_SCRIPT = Path(__file__).with_name("host_nic_routing.sh").read_text(encoding="utf-8")
HOST_SCRIPT_PATH = "/opt/monitoring/scripts/nic-routing.sh"
UNIT_NAME = "mon-nic-routing.service"
UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}"

UNIT = """[Unit]
Description=Monitoring node: replies from an extra NIC's address leave through that NIC

[Service]
Type=simple
ExecStart=/opt/monitoring/scripts/nic-routing.sh watch
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

COMMAND_TIMEOUT_SEC = 30
NIC_ROLES = frozenset(get_args(NicRoutingRole))
STATUS_COMMAND = (
    f"systemctl is-enabled {UNIT_NAME} 2>/dev/null | sed 's/^/ENABLED=/'; "
    f"systemctl is-active {UNIT_NAME} 2>/dev/null | sed 's/^/ACTIVE=/'; "
    f"{HOST_SCRIPT_PATH} status"
)


class NicRoutingError(Exception):
    pass


def parse_status(text: str) -> tuple[list[NicRoutingInterface], bool, int]:
    """Вывод `nic-routing.sh status` → (карты, совпадает ли ядро с желаемым, сколько своих правил)."""
    interfaces: list[NicRoutingInterface] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 6 or parts[0] != "NIC" or parts[2] not in NIC_ROLES or not parts[3].isdigit():
            continue
        interfaces.append(NicRoutingInterface(
            name=parts[1],
            role=parts[2],
            table=int(parts[3]),
            gateway=None if parts[4] == "-" else parts[4],
            addresses=[cidr for cidr in parts[5].split(",") if cidr],
        ))
    values = parse_key_values(text)
    rules = values.get("RULES", "0")
    return interfaces, values.get("IN_SYNC") == "yes", int(rules) if rules.isdigit() else 0


class NicRoutingManager:
    def __init__(self, executor: HostExecutor):
        self._executor = executor
        self._lock = asyncio.Lock()
        # путь → хэш того, что уже лежит на хосте: состояние поллится, не сверять каждый раз
        self._installed: dict[str, str] = {}

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
            raise NicRoutingError(f"cannot write {path} on the host")
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
            raise NicRoutingError(result.stderr.strip() or result.error or f"systemctl {args[0]} failed")

    async def _run_script(self, verb: str) -> None:
        result = await self._executor.execute(f"{HOST_SCRIPT_PATH} {verb}", timeout=COMMAND_TIMEOUT_SEC, shell="bash")
        if result.stderr.strip():
            # Отдельная команда ip не прошла — служба повторит на следующем событии
            logger.warning("nic routing: %s reported: %s", verb, result.stderr.strip())

    async def state(self) -> NicRoutingState:
        try:
            # Только скрипт: юнит ложится на хост при включении, а не от просмотра страницы
            await self._ensure_file(HOST_SCRIPT_PATH, HOST_SCRIPT, "755")
        except NicRoutingError as exc:
            return NicRoutingState(supported=False, message=str(exc))
        result = await self._executor.execute(STATUS_COMMAND, timeout=COMMAND_TIMEOUT_SEC, shell="bash")
        values = parse_key_values(result.stdout)
        if "IN_SYNC" not in values:
            reason = result.stderr.strip() or result.error or "nic-routing.sh status printed nothing"
            return NicRoutingState(supported=False, message=reason)
        interfaces, in_sync, rules = parse_status(result.stdout)
        enabled = values.get("ENABLED") == "enabled"
        return NicRoutingState(
            supported=True,
            enabled=enabled,
            active=values.get("ACTIVE") == "active",
            in_sync=in_sync if enabled else rules == 0,
            interfaces=interfaces,
        )

    async def set_enabled(self, enabled: bool) -> NicRoutingState:
        async with self._lock:
            await self._ensure_installed()
            if enabled:
                # Первый проход сразу, а не в фоне службы: ответ панели уже показывает итог
                await self._run_script("sync")
                await self._systemctl("enable", "--now")
            else:
                await self._systemctl("disable", "--now")
                await self._run_script("clear")
        logger.info("nic routing: %s", "enabled" if enabled else "disabled")
        return await self.state()

    async def start(self) -> None:
        """Обновлённый агент привозит новый скрипт — включённая служба переезжает на него."""
        if not await self._is_enabled():
            return
        if await self._ensure_installed():
            await self._systemctl("restart")
            logger.info("nic routing: service restarted on the updated script")


_manager: Optional[NicRoutingManager] = None


def get_nic_routing_manager() -> NicRoutingManager:
    global _manager
    if _manager is None:
        from app.services.host_executor import get_host_executor
        _manager = NicRoutingManager(get_host_executor())
    return _manager
