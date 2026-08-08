"""Кумулятивные счётчики трафика по портам из отдельных цепочек iptables.

Счётчики снимает фоновая задача, а не сам запрос метрик: у `/api/metrics`
жёсткий бюджет (nginx ноды — `proxy_read_timeout 10s`, панель — read timeout
10s), а дамп iptables под конкурентной блокировкой xtables съедает его
целиком — медленно отвечающая нода для панели неотличима от упавшей. Поэтому
`snapshot()` синхронный и только копирует уже собранное.

Отсюда уходят сырые кумулятивные значения: дельты, историю и арифметику ведёт
панель.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

CHAIN_IN = "TRAFFIC_ACCOUNTING_IN"
CHAIN_OUT = "TRAFFIC_ACCOUNTING_OUT"
TRACKED_PROTOCOLS = ("tcp", "udp")

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


def parse_port_counters(dump: str) -> dict[int, dict[str, int]]:
    """Разобрать вывод `iptables-save -c -t filter` в {порт: {rx_bytes, tx_bytes}}.

    Порт берётся как отдельный токен правила (`--dport 80`), а не поиском
    подстроки в выводе `iptables -L`: там `dpt:80` совпадает и внутри
    `dpt:8080`, и трафик 8080-го приписывался 80-му.
    """
    counters: dict[int, dict[str, int]] = {}

    for line in dump.splitlines():
        if not line.startswith("["):
            continue
        counter, separator, rule = line[1:].partition("] ")
        if not separator:
            continue

        tokens = rule.split()
        if len(tokens) < 2 or tokens[0] != "-A":
            continue
        if tokens[1] == CHAIN_IN:
            field, port_flag = "rx_bytes", "--dport"
        elif tokens[1] == CHAIN_OUT:
            field, port_flag = "tx_bytes", "--sport"
        else:
            continue

        port = _port_argument(tokens, port_flag)
        byte_count = counter.partition(":")[2]
        if port is None or not byte_count.isdigit():
            continue

        counters.setdefault(port, dict(ZERO_COUNTER))[field] += int(byte_count)

    return counters


def _port_argument(tokens: list[str], flag: str) -> Optional[int]:
    try:
        value = tokens[tokens.index(flag) + 1]
    except (ValueError, IndexError):
        return None
    return int(value) if value.isdigit() else None


class PortTrafficSampler:
    """Опрашивает цепочки учёта и держит последний снимок в памяти."""

    def __init__(self) -> None:
        settings = get_settings()
        self._interval = settings.port_sample_interval
        self._config_path = Path(settings.traffic_db_path).parent / "traffic_config.json"
        self._tracked: list[int] = []
        self._ports: list[dict[str, int]] = []
        self._available = False
        self._sampled_at: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        # Правки правил идут и из фоновой задачи, и из запросов панели
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        self._tracked = self._load_tracked_ports()
        async with self._lock:
            await self._ensure_chains()
        if not self._available:
            logger.warning("iptables is not available - per-port traffic accounting is off")
            return
        logger.info("Port traffic sampler ready, tracked ports: %s", self._tracked)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._sampling_loop())
        logger.info("Port traffic sampler started, interval %ss", self._interval)

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("Port traffic sampler stopped")

    def snapshot(self) -> tuple[list[dict], bool, Optional[float]]:
        """Копия последнего снимка: (счётчики, доступность iptables, время снятия).

        Вызывается из сбора метрик, поэтому синхронно и без I/O.
        """
        return [dict(entry) for entry in self._ports], self._available, self._sampled_at

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
                await self._sample()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Port traffic sampling failed: %s", exc, exc_info=True)

            await asyncio.sleep(self._interval)

    async def _sample(self) -> None:
        if not self._tracked:
            self._ports = []
            self._sampled_at = time.time()
            return

        returncode, dump = await self._exec(["iptables-save", "-c", "-t", "filter"])
        if returncode != 0:
            self._available = False
            logger.warning("iptables-save failed (rc=%s), port counters are stale", returncode)
            return

        counters = parse_port_counters(dump)
        # Список пересобирается целиком: его читают из другого треда, и подмена
        # ссылки атомарна, а правка на месте отдала бы полуобновлённые данные
        self._ports = [
            {"port": port, **counters.get(port, ZERO_COUNTER)}
            for port in self._tracked
        ]
        self._sampled_at = time.time()
        self._available = True

    async def _ensure_chains(self) -> None:
        """Поднять цепочки учёта и врезать их в INPUT/OUTPUT. Вызывать под self._lock.

        Доступность iptables перепроверяется здесь же, а не только на старте:
        ядро могло получить нужные модули уже после запуска ноды.
        """
        self._available = await self._run_iptables(["-L", "INPUT", "-n"])
        if not self._available:
            return

        await self._run_iptables(["-N", CHAIN_IN])
        await self._run_iptables(["-N", CHAIN_OUT])

        if not await self._run_iptables(["-C", "INPUT", "-j", CHAIN_IN]):
            await self._run_iptables(["-I", "INPUT", "-j", CHAIN_IN])
        if not await self._run_iptables(["-C", "OUTPUT", "-j", CHAIN_OUT]):
            await self._run_iptables(["-I", "OUTPUT", "-j", CHAIN_OUT])

        for port in self._tracked:
            await self._ensure_port_rules(port)

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

    async def _run_iptables(self, args: list[str]) -> bool:
        """Аргументы принимаются списком: сборка команды из строки со split()
        сама по себе источник ошибок, а значения сюда приходят из API."""
        returncode, _ = await self._exec(
            ["iptables", "-w", str(XTABLES_LOCK_WAIT_SEC), *args]
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


_sampler: Optional[PortTrafficSampler] = None


def get_port_traffic_sampler() -> PortTrafficSampler:
    global _sampler
    if _sampler is None:
        _sampler = PortTrafficSampler()
    return _sampler
