"""Кумулятивные счётчики трафика из собственных цепочек iptables: по портам и по IP.

Счётчики снимает фоновая задача, а не сам запрос метрик: у `/api/metrics`
жёсткий бюджет (nginx ноды — `proxy_read_timeout 10s`, панель — read timeout
10s), а дамп iptables под конкурентной блокировкой xtables съедает его
целиком — медленно отвечающая нода для панели неотличима от упавшей. Поэтому
`snapshot()`/`ip_snapshot()` синхронные и только копируют уже собранное.

Учёт по IP разделён надвое, адрес берётся из conntrack, а не из заголовка пакета.
Клиенты — соединения, которые открыли К адресу (`--ctorigdst`): по ним видно,
ходят ли ещё клиенты на заблокированный у них адрес. Свои — соединения, которые
сервер открыл С адреса (`--ctrepldst`): Xray ходит в интернет, релей пересылает
клиента на бэкенд с маскарадом. Сумма двух — весь трафик адреса, как у
интерфейса. Адреса из conntrack — до DNAT и после SNAT, поэтому транзит релея
ложится на его публичный адрес, хотя в FORWARD у пакета адреса другие.

Цепочки учёта по IP живут в mangle, а не в filter: в filter первым правилом
INPUT и OUTPUT стоит ACCEPT белого списка (`ipset_manager` держит его наверху
нарочно), и клиенты, пришедшие через релей из белого списка, уходили по нему мимо
счётчика. mangle проходится раньше filter, но и раньше его DROP, поэтому
считаются только установленные соединения: флуд и сканы, которые filter
отбросит, соединением так и не становятся.

Отсюда уходят сырые кумулятивные значения: дельты, историю и арифметику ведёт
панель.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

PORT_TABLE = "filter"
IP_TABLE = "mangle"

CHAIN_IN = "TRAFFIC_ACCOUNTING_IN"
CHAIN_OUT = "TRAFFIC_ACCOUNTING_OUT"
PORT_CHAIN_HOOKS = (("INPUT", CHAIN_IN), ("OUTPUT", CHAIN_OUT))
TRACKED_PROTOCOLS = ("tcp", "udp")

# _IN видит пакеты, пришедшие на хост (INPUT и FORWARD), _OUT — уходящие с него
# (OUTPUT и FORWARD): транзитный пакет релея проходит обе, как на интерфейсе он
# и принят, и отправлен.
CHAIN_IP_IN = "TRAFFIC_ACCOUNTING_IP_IN"
CHAIN_IP_OUT = "TRAFFIC_ACCOUNTING_IP_OUT"
IP_CHAIN_HOOKS = (
    ("INPUT", CHAIN_IP_IN),
    ("FORWARD", CHAIN_IP_IN),
    ("OUTPUT", CHAIN_IP_OUT),
    ("FORWARD", CHAIN_IP_OUT),
)
# Своих соединений из встроенных цепочек не видно: в них переходят из _IP_IN и _IP_OUT.
# Имена не длиннее 28 символов — предел iptables.
CHAIN_OWN_IN = "TRAFFIC_ACCOUNTING_OWN_IN"
CHAIN_OWN_OUT = "TRAFFIC_ACCOUNTING_OWN_OUT"
# Правило на адрес проверяется каждым пакетом, а адресов бывает два десятка (пул
# исходящих). Поэтому первое правило цепочки отправляет пакеты своих соединений
# (на VPN-ноде это около половины) goto-переходом в цепочку своих: клиентские
# правила они не перебирают, а RETURN оттуда возвращает сразу во встроенную
# цепочку. Совпавший с адресом пакет тоже сразу возвращается.
IP_DIRECTION_SPLITS = ((CHAIN_IP_IN, "REPLY", CHAIN_OWN_IN), (CHAIN_IP_OUT, "ORIGINAL", CHAIN_OWN_OUT))
# (цепочка, адрес из conntrack, направление пакета, поле снимка)
IP_COUNTER_RULES = (
    (CHAIN_IP_IN, "--ctorigdst", "ORIGINAL", "rx_bytes"),
    (CHAIN_IP_OUT, "--ctorigdst", "REPLY", "tx_bytes"),
    (CHAIN_OWN_IN, "--ctrepldst", "REPLY", "own_rx_bytes"),
    (CHAIN_OWN_OUT, "--ctrepldst", "ORIGINAL", "own_tx_bytes"),
)
IP_COUNTER_FIELDS = frozenset(field for *_, field in IP_COUNTER_RULES)
# Сборки до учёта своих соединений отбрасывали их первым правилом цепочки
LEGACY_DIRECTION_GUARDS = ((CHAIN_IP_IN, "REPLY"), (CHAIN_IP_OUT, "ORIGINAL"))
HOST_MASK_SUFFIX = "/32"

PORT_MIN = 1
PORT_MAX = 65535

# Без -w команда падает, как только ufw или ipset держат блокировку xtables, и
# счётчики теряются молча. У iptables-save такого флага нет — он ничего не меняет.
XTABLES_LOCK_WAIT_SEC = 5
COMMAND_TIMEOUT_SEC = 20
# `ufw --force reset` при применении firewall-профиля сносит наши цепочки целиком
CHAIN_RECHECK_INTERVAL_SEC = 600

EXEC_FAILED = -1
ZERO_COUNTER = {"rx_bytes": 0, "tx_bytes": 0}

AddressSource = Callable[[], list[str]]


def _counted_rules(dump: str):
    """Правила из `iptables-save -c`: (байты, токены правила начиная с `-A`)."""
    for line in dump.splitlines():
        if not line.startswith("["):
            continue
        counter, separator, rule = line[1:].partition("] ")
        if not separator:
            continue
        tokens = rule.split()
        byte_count = counter.partition(":")[2]
        if len(tokens) < 2 or tokens[0] != "-A" or not byte_count.isdigit():
            continue
        yield int(byte_count), tokens


def parse_port_counters(dump: str) -> dict[int, dict[str, int]]:
    """Разобрать вывод `iptables-save -c -t filter` в {порт: {rx_bytes, tx_bytes}}.

    Порт берётся как отдельный токен правила (`--dport 80`), а не поиском
    подстроки в выводе `iptables -L`: там `dpt:80` совпадает и внутри
    `dpt:8080`, и трафик 8080-го приписывался 80-му.
    """
    counters: dict[int, dict[str, int]] = {}

    for byte_count, tokens in _counted_rules(dump):
        if tokens[1] == CHAIN_IN:
            field, port_flag = "rx_bytes", "--dport"
        elif tokens[1] == CHAIN_OUT:
            field, port_flag = "tx_bytes", "--sport"
        else:
            continue

        port = _port_argument(tokens, port_flag)
        if port is None:
            continue
        counters.setdefault(port, dict(ZERO_COUNTER))[field] += byte_count

    return counters


def parse_ip_counters(dump: str) -> dict[str, dict[str, int]]:
    """Разобрать вывод `iptables-save -c -t mangle` в {адрес: {поле: байты}}.

    Поле — по цепочке (`IP_COUNTER_RULES`); у адреса только те поля, правила которых
    нашлись в дампе. Маску `/32` сохранение печатает не во всех сборках iptables,
    поэтому она снимается.
    """
    rules_by_chain = {chain: (address_flag, field) for chain, address_flag, _, field in IP_COUNTER_RULES}
    counters: dict[str, dict[str, int]] = {}

    for byte_count, tokens in _counted_rules(dump):
        rule = rules_by_chain.get(tokens[1])
        if rule is None:
            continue
        address_flag, field = rule
        address = _flag_argument(tokens, address_flag)
        if not address:
            continue
        entry = counters.setdefault(address.removesuffix(HOST_MASK_SUFFIX), {})
        entry[field] = entry.get(field, 0) + byte_count

    return counters


def _flag_argument(tokens: list[str], flag: str) -> Optional[str]:
    try:
        return tokens[tokens.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _port_argument(tokens: list[str], flag: str) -> Optional[int]:
    value = _flag_argument(tokens, flag)
    return int(value) if value and value.isdigit() else None


def _ip_rules(address: str) -> list[list[str]]:
    return [
        [
            chain, "-m", "conntrack", "--ctstate", "ESTABLISHED",
            address_flag, address, "--ctdir", direction, "-j", "RETURN",
        ]
        for chain, address_flag, direction, _ in IP_COUNTER_RULES
    ]


def _direction_split(chain: str, direction: str, own_chain: str) -> list[str]:
    return [chain, "-m", "conntrack", "--ctdir", direction, "-g", own_chain]


def _legacy_direction_guard(chain: str, direction: str) -> list[str]:
    return [chain, "-m", "conntrack", "--ctdir", direction, "-j", "RETURN"]


class TrafficCounterSampler:
    """Опрашивает цепочки учёта и держит последний снимок в памяти."""

    def __init__(self) -> None:
        settings = get_settings()
        self._interval = settings.port_sample_interval
        self._config_path = Path(settings.traffic_db_path).parent / "traffic_config.json"
        self._tracked: list[int] = []
        self._ports: list[dict[str, int]] = []
        self._address_source: Optional[AddressSource] = None
        self._addresses: list[str] = []
        self._ips: list[dict] = []
        self._available = False
        self._sampled_at: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        # Правки правил идут и из фоновой задачи, и из запросов панели
        self._lock = asyncio.Lock()

    async def init(self, address_source: Optional[AddressSource] = None) -> None:
        """address_source отдаёт адреса, по которым вести учёт; опрашивается
        каждый цикл, потому что панель добавляет и снимает адреса на ходу."""
        self._address_source = address_source
        self._tracked = self._load_tracked_ports()
        async with self._lock:
            await self._ensure_chains()
            if self._available:
                await self._remove_leftovers()
        if not self._available:
            logger.warning("iptables is not available - port and IP traffic accounting is off")
            return
        logger.info("Traffic counter sampler ready, tracked ports: %s", self._tracked)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._sampling_loop())
        logger.info("Traffic counter sampler started, interval %ss", self._interval)

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("Traffic counter sampler stopped")

    def snapshot(self) -> tuple[list[dict], bool, Optional[float]]:
        """Копия последнего снимка портов: (счётчики, доступность iptables, время снятия).

        Вызывается из сбора метрик, поэтому синхронно и без I/O.
        """
        return [dict(entry) for entry in self._ports], self._available, self._sampled_at

    def ip_snapshot(self) -> tuple[list[dict], bool, Optional[float]]:
        """То же для адресов. Время снятия общее: порты и адреса читаются в одном цикле."""
        return [dict(entry) for entry in self._ips], self._available, self._sampled_at

    def tracked_ports(self) -> list[int]:
        return self._tracked.copy()

    async def add_tracked_port(self, port: int) -> tuple[bool, str]:
        if not PORT_MIN <= port <= PORT_MAX:
            return False, f"Port {port} is out of range {PORT_MIN}-{PORT_MAX}"
        if port in self._tracked:
            return False, f"Port {port} already tracked"
        if not self._available:
            return False, "iptables not available - port tracking disabled"

        async with self._lock:
            if not await self._ensure_port_rules(port):
                return False, f"Failed to add iptables rules for port {port}"
            self._tracked.append(port)
            self._tracked.sort()
            self._save_tracked_ports()
            # Внеочередной замер обязан идти под тем же локом, что и фоновый: иначе более
            # старый дамп ляжет последним, панель увидит счётчик «назад» и спишет это на
            # сброс — дельта за окно потеряется молча.
            await self._sample()

        logger.info("Port %s added to traffic tracking", port)
        return True, f"Port {port} added to tracking"

    async def remove_tracked_port(self, port: int) -> tuple[bool, str]:
        if port not in self._tracked:
            return False, f"Port {port} not tracked"

        async with self._lock:
            await self._remove_port_rules(port)
            self._tracked.remove(port)
            self._save_tracked_ports()
            await self._sample()

        logger.info("Port %s removed from traffic tracking", port)
        return True, f"Port {port} removed from tracking"

    async def _sampling_loop(self) -> None:
        chains_checked_at = time.monotonic()

        while True:
            try:
                if time.monotonic() - chains_checked_at >= CHAIN_RECHECK_INTERVAL_SEC:
                    async with self._lock:
                        await self._ensure_chains()
                    chains_checked_at = time.monotonic()
                await self._sync_addresses()
                if not await self._sample():
                    # Правила учёта пропали (обычно их сносит `ufw --force reset`
                    # при синке firewall-профиля) — восстанавливаем сразу, не
                    # дожидаясь плановой перепроверки: всё это время порт или адрес
                    # не измеряется, и панель видит разрыв вместо тишины
                    async with self._lock:
                        await self._ensure_chains()
                    chains_checked_at = time.monotonic()
                    await self._sample()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Traffic counter sampling failed: %s", exc, exc_info=True)

            await asyncio.sleep(self._interval)

    async def _sync_addresses(self) -> None:
        """Привести правила учёта к текущему набору адресов хоста."""
        if not self._available or self._address_source is None:
            return
        current = sorted(set(await asyncio.to_thread(self._address_source)))
        if current == self._addresses:
            return

        gone = set(self._addresses) - set(current)
        added = set(current) - set(self._addresses)
        async with self._lock:
            for address in gone:
                await self._remove_ip_rules(address)
            for address in added:
                await self._ensure_ip_rules(address)
            self._addresses = current
        logger.info("IP traffic accounting: added %s, removed %s", sorted(added), sorted(gone))

    async def _sample(self) -> bool:
        """Снять счётчики. False — у части портов или адресов не нашлось правил учёта."""
        if not self._tracked and not self._addresses:
            self._ports = []
            self._ips = []
            self._sampled_at = time.time()
            return True

        port_counters: dict[int, dict[str, int]] = {}
        ip_counters: dict[str, dict[str, int]] = {}
        if self._tracked:
            dump = await self._dump(PORT_TABLE)
            if dump is None:
                return True
            port_counters = parse_port_counters(dump)
        if self._addresses:
            dump = await self._dump(IP_TABLE)
            if dump is None:
                return True
            ip_counters = parse_ip_counters(dump)
        # Отсутствие порта или адреса в дампе — это «правила снесены», а не «трафика
        # не было»: у только что добавленного правило есть и показывает честный ноль.
        # Отдать здесь ноль означало бы шаг счётчика назад — панель списала бы его
        # на сброс и молча потеряла бы накопленную дельту.
        missing_ports = [port for port in self._tracked if port not in port_counters]
        missing_ips = [
            address for address in self._addresses
            if ip_counters.get(address, {}).keys() != IP_COUNTER_FIELDS
        ]
        if missing_ports:
            logger.warning("Traffic accounting rules are missing for ports %s", missing_ports)
        if missing_ips:
            logger.warning("Traffic accounting rules are missing for addresses %s", missing_ips)

        # Списки пересобираются целиком: их читают из другого треда, и подмена
        # ссылки атомарна, а правка на месте отдала бы полуобновлённые данные
        self._ports = [
            {"port": port, **port_counters[port]}
            for port in self._tracked
            if port in port_counters
        ]
        self._ips = [
            {"ip": address, **ip_counters[address]}
            for address in self._addresses
            if address not in missing_ips
        ]
        self._sampled_at = time.time()
        self._available = True
        return not missing_ports and not missing_ips

    async def _dump(self, table: str) -> Optional[str]:
        returncode, dump = await self._exec(["iptables-save", "-c", "-t", table])
        if returncode != 0:
            self._available = False
            logger.warning("iptables-save -t %s failed (rc=%s), traffic counters are stale", table, returncode)
            return None
        return dump

    async def _ensure_chains(self) -> None:
        """Поднять цепочки учёта и врезать их во встроенные. Вызывать под self._lock.

        Доступность iptables перепроверяется здесь же, а не только на старте:
        ядро могло получить нужные модули уже после запуска ноды.
        """
        self._available = await self._run_iptables(["-L", "INPUT", "-n"])
        if not self._available:
            return

        await self._ensure_hooks(PORT_TABLE, PORT_CHAIN_HOOKS)
        await self._ensure_hooks(IP_TABLE, IP_CHAIN_HOOKS)
        for chain, direction, own_chain in IP_DIRECTION_SPLITS:
            # Цепочка-цель goto обязана существовать до правила, которое на неё ссылается
            await self._run_iptables(["-N", own_chain], IP_TABLE)
            split = _direction_split(chain, direction, own_chain)
            if not await self._run_iptables(["-C", *split], IP_TABLE):
                await self._run_iptables(["-I", chain, "1", *split[1:]], IP_TABLE)

        for port in self._tracked:
            await self._ensure_port_rules(port)
        for address in self._addresses:
            await self._ensure_ip_rules(address)

    async def _ensure_hooks(self, table: str, hooks: tuple[tuple[str, str], ...]) -> None:
        for chain in dict.fromkeys(chain for _, chain in hooks):
            await self._run_iptables(["-N", chain], table)
        for builtin, chain in hooks:
            if not await self._run_iptables(["-C", builtin, "-j", chain], table):
                await self._run_iptables(["-I", builtin, "-j", chain], table)

    async def _remove_leftovers(self) -> None:
        """Убрать то, что оставили прежние сборки: без уборки каждый пакет проходил
        бы ещё и брошенные правила.

        Цепочки учёта по IP жили в filter до переезда в mangle; первым правилом
        цепочек в mangle стоял отброс своих соединений, пока их не начали считать.
        """
        for chain, direction in LEGACY_DIRECTION_GUARDS:
            guard = _legacy_direction_guard(chain, direction)
            if await self._run_iptables(["-C", *guard], IP_TABLE):
                await self._run_iptables(["-D", *guard], IP_TABLE)

        if not await self._run_iptables(["-L", CHAIN_IP_IN, "-n"], PORT_TABLE):
            return
        for builtin, chain in IP_CHAIN_HOOKS:
            if await self._run_iptables(["-C", builtin, "-j", chain], PORT_TABLE):
                await self._run_iptables(["-D", builtin, "-j", chain], PORT_TABLE)
        for chain in (CHAIN_IP_IN, CHAIN_IP_OUT):
            await self._run_iptables(["-F", chain], PORT_TABLE)
            await self._run_iptables(["-X", chain], PORT_TABLE)
        logger.info("Removed IP accounting chains left in the filter table")

    async def _ensure_port_rules(self, port: int) -> bool:
        added = True
        for protocol in TRACKED_PROTOCOLS:
            for chain, port_flag in ((CHAIN_IN, "--dport"), (CHAIN_OUT, "--sport")):
                rule = [chain, "-p", protocol, port_flag, str(port)]
                if await self._run_iptables(["-C", *rule]):
                    continue
                if not await self._run_iptables(["-A", *rule]):
                    added = False
        return added

    async def _remove_port_rules(self, port: int) -> None:
        for protocol in TRACKED_PROTOCOLS:
            for chain, port_flag in ((CHAIN_IN, "--dport"), (CHAIN_OUT, "--sport")):
                await self._run_iptables(["-D", chain, "-p", protocol, port_flag, str(port)])

    async def _ensure_ip_rules(self, address: str) -> None:
        for rule in _ip_rules(address):
            if not await self._run_iptables(["-C", *rule], IP_TABLE):
                await self._run_iptables(["-A", *rule], IP_TABLE)

    async def _remove_ip_rules(self, address: str) -> None:
        for rule in _ip_rules(address):
            await self._run_iptables(["-D", *rule], IP_TABLE)

    async def _run_iptables(self, args: list[str], table: str = PORT_TABLE) -> bool:
        """Аргументы принимаются списком: сборка команды из строки со split()
        сама по себе источник ошибок, а значения сюда приходят из API."""
        returncode, _ = await self._exec(
            ["iptables", "-w", str(XTABLES_LOCK_WAIT_SEC), "-t", table, *args]
        )
        return returncode == 0

    async def _exec(self, command: list[str]) -> tuple[int, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            logger.error("Cannot run %s: %s", command[0], exc)
            return EXEC_FAILED, ""

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=COMMAND_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            logger.error("Timed out after %ss: %s", COMMAND_TIMEOUT_SEC, " ".join(command))
            return EXEC_FAILED, ""

        if process.returncode != 0:
            # Ненулевой код — штатный ответ для -C и -N, поэтому это не ошибка
            logger.debug(
                "%s exited %s: %s",
                " ".join(command),
                process.returncode,
                stderr.decode(errors="replace").strip(),
            )
        return process.returncode, stdout.decode(errors="replace")

    def _load_tracked_ports(self) -> list[int]:
        try:
            data = json.loads(self._config_path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            logger.warning("Cannot read %s, no ports tracked: %s", self._config_path, exc)
            return []

        ports = data.get("tracked_ports", [])
        return sorted({
            port for port in ports
            if isinstance(port, int) and PORT_MIN <= port <= PORT_MAX
        })

    def _save_tracked_ports(self) -> None:
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(
                json.dumps({"tracked_ports": self._tracked}, indent=2)
            )
        except OSError as exc:
            logger.error("Failed to save %s: %s", self._config_path, exc)


_sampler: Optional[TrafficCounterSampler] = None


def get_traffic_counter_sampler() -> TrafficCounterSampler:
    global _sampler
    if _sampler is None:
        _sampler = TrafficCounterSampler()
    return _sampler
