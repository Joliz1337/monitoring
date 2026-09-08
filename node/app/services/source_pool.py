"""Пул исходящих адресов: метки fwmark раскладываются по IP ноды через policy routing.

Зачем. Пространство исходящих TCP-соединений ограничено четвёркой
(src ip, src port, dst ip, dst port): на одну пару «наш адрес → адрес:порт цели»
приходится только диапазон эфемерных портов, 64512 штук. Нода, через которую
десятки тысяч клиентов ходят на один популярный адрес, упирается в этот потолок,
и каждый новый connect() заставляет ядро перебирать почти весь диапазон под
спинлоком — процессор уходит в system time, а трафик не растёт. Потолок кратен
числу исходящих адресов, поэтому лечится он только их добавлением.

Как. Xray помечает исходящие соединения фиксированным набором меток (одинаковым
для всего парка, потому что конфиг Remnawave один на все ноды), а нода решает,
какой метке какой адрес соответствует. Метки раскладываются по адресам по кругу:
30 меток на 3 адреса — по 10 на каждый, на 2 адреса — по 15. Балансировщик Xray
делит трафик поровну между метками, значит и между адресами — на любой ноде, без
правки конфига под неё.

Выбор источника адреса определяется маршрутом, а маршрут — меткой на сокете, и
всё это происходит при connect(), до подбора локального порта. Тем и отличается
от SNAT, который переписывает адрес уже после подбора и потолок не двигает.

Правила и таблицы маршрутизации живут только в памяти ядра. Переживание ребута
и чужих вмешательств обеспечивает цикл самолечения, как у dnat_manager.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.models.source_pool import (
    MARK_BASE,
    MARK_COUNT,
    RULE_PRIORITY_BASE,
    TABLE_BASE,
    MarkBinding,
    SourcePoolConfig,
    SourcePoolState,
)
from app.services.host_executor import HostExecutor

logger = logging.getLogger(__name__)

STATE_FILE_NAME = "source_pool.json"
SELF_HEAL_INTERVAL_SEC = 30
IP_TIMEOUT_SEC = 15

MARK_RULES = "#RULES"
MARK_TABLES = "#TABLES"
MARK_DEFAULT = "#DEFAULT"

# Один адрес — раскладывать нечего: любая метка и так уйдёт по основной таблице
# ровно с него. Правила в этом случае не ставятся, нода остаётся нетронутой.
MIN_ADDRESSES = 2

EXIT_PROXY_CONFLICT = "exit-прокси включён: он сам выбирает исходящий адрес"


# ---------------------------------------------------------------------------
# Чистые функции: раскладка, разбор вывода `ip`, генерация команд
# ---------------------------------------------------------------------------

def build_bindings(addresses: list[str]) -> list[MarkBinding]:
    """Метки по кругу: mark i → адрес i % N. Ровное деление при любом N."""
    if len(addresses) < MIN_ADDRESSES:
        return []
    return [
        MarkBinding(
            mark=MARK_BASE + index,
            address=addresses[index % len(addresses)],
            table=TABLE_BASE + (index % len(addresses)),
        )
        for index in range(MARK_COUNT)
    ]


def _as_int(value) -> Optional[int]:
    """`ip -j` печатает fwmark строкой ("0x65", иногда с маской), таблицу — строкой
    или числом. Имена таблиц (main/local) числом не являются и отсеиваются."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.split("/", 1)[0].strip(), 0)
        except ValueError:
            return None
    return None


def parse_rules(text: str) -> dict[int, tuple[int, int]]:
    """`ip -j rule show` → {метка: (приоритет, таблица)} только для наших меток."""
    try:
        entries = json.loads(text or "[]")
    except ValueError:
        return {}
    rules: dict[int, tuple[int, int]] = {}
    for entry in entries if isinstance(entries, list) else []:
        mark = _as_int(entry.get("fwmark"))
        table = _as_int(entry.get("table"))
        priority = _as_int(entry.get("priority"))
        if mark is None or table is None or priority is None:
            continue
        if MARK_BASE <= mark < MARK_BASE + MARK_COUNT:
            rules[mark] = (priority, table)
    return rules


TableRoute = tuple[Optional[str], str]


def parse_table_routes(text: str) -> dict[int, TableRoute]:
    """`ip -j route show table all` → {таблица: (шлюз, исходящий адрес)} по нашим
    таблицам. Шлюз сравнивается наравне с адресом: маршрут без `via` с виду
    рабочий, а на деле шлёт пакеты напрямую в линк, и соединения по метке гибнут."""
    try:
        entries = json.loads(text or "[]")
    except ValueError:
        return {}
    routes: dict[int, TableRoute] = {}
    for entry in entries if isinstance(entries, list) else []:
        table = _as_int(entry.get("table"))
        if table is None or not TABLE_BASE <= table < TABLE_BASE + MARK_COUNT:
            continue
        if entry.get("dst") != "default":
            continue
        source = entry.get("prefsrc") or entry.get("src")
        if source:
            gateway = entry.get("gateway")
            routes[table] = (str(gateway) if gateway else None, str(source))
    return routes


def parse_default_gateway(text: str, interface: str) -> Optional[str]:
    """Шлюз дефолтного маршрута. Его может не быть — на point-to-point линках
    маршрут выглядит как `default dev eth0`, и это законно."""
    try:
        entries = json.loads(text or "[]")
    except ValueError:
        return None
    candidates = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    for entry in candidates:
        if entry.get("dev") == interface and entry.get("gateway"):
            return str(entry["gateway"])
    for entry in candidates:
        if entry.get("gateway"):
            return str(entry["gateway"])
    return None


def split_probe(text: str) -> tuple[str, str, str]:
    """Разрезать один ответ хоста на три куска по маркерам."""
    rules, tables, default = "", "", ""
    current = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped == MARK_RULES:
            current = "rules"
            continue
        if stripped == MARK_TABLES:
            current = "tables"
            continue
        if stripped == MARK_DEFAULT:
            current = "default"
            continue
        if current == "rules":
            rules += line
        elif current == "tables":
            tables += line
        elif current == "default":
            default += line
    return rules, tables, default


def probe_command() -> str:
    """Маркеры в кавычках: голый `#` bash читает как начало комментария и
    обрезал бы всю остальную строку — опрос возвращал бы пустоту, а нода
    считала бы, что правил нет, и не знала бы шлюз."""
    return (
        f"echo '{MARK_RULES}'; ip -j -4 rule show; "
        f"echo '{MARK_TABLES}'; ip -j -4 route show table all; "
        f"echo '{MARK_DEFAULT}'; ip -j -4 route show default"
    )


def route_command(table: int, address: str, interface: str, gateway: Optional[str]) -> str:
    via = f"via {gateway} " if gateway else ""
    return f"ip route replace default {via}dev {interface} src {address} table {table}"


def rule_commands(binding: MarkBinding, index: int) -> list[str]:
    """Снять прежнюю привязку метки и поставить свою. Удаление без совпадения —
    не ошибка сценария, поэтому его вывод глушится."""
    priority = RULE_PRIORITY_BASE + index
    return [
        f"ip rule del fwmark {binding.mark} priority {priority} 2>/dev/null || true",
        f"ip rule add fwmark {binding.mark} lookup {binding.table} priority {priority}",
    ]


def plan_commands(
    bindings: list[MarkBinding],
    current_rules: dict[int, tuple[int, int]],
    current_routes: dict[int, TableRoute],
    interface: str,
    gateway: Optional[str],
) -> list[str]:
    """Только расхождения: в устоявшемся состоянии цикл самолечения ничего не пишет."""
    commands: list[str] = []
    wanted_routes = {b.table: b.address for b in bindings}
    for table, address in sorted(wanted_routes.items()):
        if current_routes.get(table) != (gateway, address):
            commands.append(route_command(table, address, interface, gateway))
    for index, binding in enumerate(bindings):
        expected = (RULE_PRIORITY_BASE + index, binding.table)
        if current_rules.get(binding.mark) != expected:
            commands.extend(rule_commands(binding, index))
    return commands


def clear_commands(current_rules: dict[int, tuple[int, int]], current_routes: dict[int, TableRoute]) -> list[str]:
    """Снимаем только своё: правила с нашими метками и маршруты в наших таблицах."""
    commands = [
        f"ip rule del fwmark {mark} priority {priority} 2>/dev/null || true"
        for mark, (priority, _) in sorted(current_rules.items())
    ]
    commands += [f"ip route flush table {table} 2>/dev/null || true" for table in sorted(current_routes)]
    return commands


# ---------------------------------------------------------------------------
# Менеджер
# ---------------------------------------------------------------------------

@dataclass
class _Discovery:
    interface: Optional[str]
    addresses: list[str]
    reason: Optional[str]


async def discover_addresses() -> _Discovery:
    """IPv4 default-интерфейса — тот же источник, что у карточки «Сетевые адреса»."""
    from app.services.extra_ips import get_extra_ip_manager

    state = await get_extra_ip_manager().state()
    if not state.supported:
        return _Discovery(None, [], state.message or "network state unavailable")
    interface = next((item for item in state.interfaces if item.is_default), None)
    if interface is None and state.interfaces:
        interface = state.interfaces[0]
    if interface is None:
        return _Discovery(None, [], "no interface capable of carrying addresses")
    addresses = [
        addr.address
        for addr in interface.addresses
        if addr.family == "ipv4" and addr.scope == "global"
    ]
    return _Discovery(interface.name, addresses, None)


def source_pool_enabled() -> bool:
    """Для встречной проверки из exit-прокси: оба управляют исходящим адресом."""
    return bool(_manager and _manager.is_enabled())


def exit_proxy_enabled() -> bool:
    from app.services.exit_proxy.manager import get_exit_proxy_manager

    try:
        return bool(get_exit_proxy_manager().status().enabled)
    except Exception:
        return False


class SourcePoolManager:
    def __init__(self, executor: HostExecutor, state_path: Optional[Path] = None):
        self._executor = executor
        self._state_path = state_path or Path(get_settings().traffic_db_path).parent / STATE_FILE_NAME
        self._config = SourcePoolConfig()
        self._last_error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ── состояние на диске ──

    def load_state(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        try:
            self._config = SourcePoolConfig(**raw.get("config", {}))
        except Exception as exc:
            logger.warning("source pool: broken state file, ignoring config: %s", exc)

    def _save_state(self) -> None:
        payload = {"config": self._config.model_dump()}
        tmp = self._state_path.with_suffix(".tmp")
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError as exc:
            logger.error("source pool: cannot persist state: %s", exc)

    def is_enabled(self) -> bool:
        return bool(self._config.enabled)

    # ── чтение хоста ──

    async def _probe(self) -> tuple[dict[int, tuple[int, int]], dict[int, TableRoute], str]:
        result = await self._executor.execute(probe_command(), timeout=IP_TIMEOUT_SEC, shell="bash")
        if not (result.success and result.exit_code == 0):
            raise RuntimeError(result.stderr or result.error or "ip command failed")
        # Без маркеров вывод нельзя трактовать как «правил нет»: так мы бы
        # переписали таблицы маршрутами без шлюза и сломали трафик по меткам
        if MARK_DEFAULT not in (result.stdout or ""):
            raise RuntimeError("probe output malformed: section markers missing")
        rules_text, tables_text, default_text = split_probe(result.stdout)
        return parse_rules(rules_text), parse_table_routes(tables_text), default_text

    def _active_addresses(self, addresses: list[str]) -> list[str]:
        excluded = set(self._config.excluded)
        return [address for address in addresses if address not in excluded]

    async def state(self) -> SourcePoolState:
        discovery = await discover_addresses()
        active = self._active_addresses(discovery.addresses)
        bindings = build_bindings(active)
        state = SourcePoolState(
            enabled=self._config.enabled,
            supported=discovery.interface is not None,
            reason=discovery.reason,
            interface=discovery.interface,
            addresses=discovery.addresses,
            excluded=list(self._config.excluded),
            active_addresses=active,
            bindings=bindings,
            last_error=self._last_error,
            conflict=EXIT_PROXY_CONFLICT if exit_proxy_enabled() else None,
        )
        if not state.supported:
            return state
        try:
            current_rules, current_routes, default_text = await self._probe()
        except RuntimeError as exc:
            state.last_error = str(exc)
            return state
        state.gateway = parse_default_gateway(default_text, discovery.interface or "")
        if not self._config.enabled or not bindings:
            state.in_sync = not current_rules and not current_routes
            state.missing_marks = []
            return state
        expected = {b.mark: (RULE_PRIORITY_BASE + i, b.table) for i, b in enumerate(bindings)}
        state.missing_marks = sorted(mark for mark, value in expected.items() if current_rules.get(mark) != value)
        routes_ok = all(current_routes.get(b.table) == (state.gateway, b.address) for b in bindings)
        state.in_sync = not state.missing_marks and routes_ok
        return state

    # ── запись ──

    async def apply_config(self, config: SourcePoolConfig) -> SourcePoolState:
        async with self._lock:
            if config.enabled and exit_proxy_enabled():
                self._last_error = EXIT_PROXY_CONFLICT
                return await self.state()
            self._config = config
            self._save_state()
            self._last_error = None
            await self._reconcile()
        return await self.state()

    async def ensure(self) -> Optional[str]:
        """Вернуть раскладку в ядро, если она разъехалась. None — чинить нечего."""
        async with self._lock:
            return await self._reconcile()

    async def _reconcile(self) -> Optional[str]:
        discovery = await discover_addresses()
        if discovery.interface is None:
            return None
        try:
            current_rules, current_routes, default_text = await self._probe()
        except RuntimeError as exc:
            self._last_error = str(exc)
            return None

        active = self._active_addresses(discovery.addresses)
        bindings = build_bindings(active) if self._config.enabled else []
        if not bindings:
            if not current_rules and not current_routes:
                return None
            await self._run(clear_commands(current_rules, current_routes))
            return "source pool cleared"

        gateway = parse_default_gateway(default_text, discovery.interface)
        commands = plan_commands(bindings, current_rules, current_routes, discovery.interface, gateway)
        # Метки, оставшиеся от прежней раскладки с бо́льшим числом адресов
        stale = {mark: value for mark, value in current_rules.items() if mark not in {b.mark for b in bindings}}
        stale_tables = {
            table: address
            for table, address in current_routes.items()
            if table not in {b.table for b in bindings}
        }
        commands = clear_commands(stale, stale_tables) + commands
        if not commands:
            return None
        error = await self._run(commands)
        if error:
            self._last_error = error
            return f"failed: {error}"
        self._last_error = None
        return f"applied {len(bindings)} marks over {len(active)} addresses"

    async def _run(self, commands: list[str]) -> Optional[str]:
        if not commands:
            return None
        result = await self._executor.execute("\n".join(commands), timeout=IP_TIMEOUT_SEC, shell="bash")
        if result.success and result.exit_code == 0:
            return None
        return result.stderr or result.error or "ip command failed"

    # ── фон ──

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self.load_state()
        try:
            action = await self.ensure()
            if action:
                logger.info("source pool at startup: %s", action)
        except Exception as exc:
            logger.error("source pool startup failed: %s", exc, exc_info=True)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(SELF_HEAL_INTERVAL_SEC)
            try:
                action = await self.ensure()
                if action:
                    logger.warning("source pool: %s", action)
            except Exception as exc:
                logger.error("source pool self-heal failed: %s", exc, exc_info=True)


_manager: Optional[SourcePoolManager] = None


def get_source_pool_manager() -> SourcePoolManager:
    global _manager
    if _manager is None:
        from app.services.host_executor import get_host_executor

        _manager = SourcePoolManager(get_host_executor())
    return _manager
