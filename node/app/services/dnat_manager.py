"""DNAT-маршрутизация: проброс портов средствами netfilter вместо userspace-прокси.

Правила живут в трёх собственных цепочках — `MON_DNAT` (nat/PREROUTING),
`MON_DNAT_POST` (nat/POSTROUTING, MASQUERADE) и `MON_DNAT_FWD` (filter/FORWARD,
ACCEPT для проброшенных потоков: и Docker, и UFW держат политику FORWARD DROP).
Каждое правило помечено `-m comment mon-dnat:<имя>`: по метке считаются
счётчики и проверяется, что правило на месте.

Желаемое состояние хранится в JSON рядом с конфигом учёта портов и
переприменяется при старте агента и фоновым циклом: правила netfilter не
переживают перезагрузку, а `ufw --force reset` (профили firewall) вычищает
джампы из встроенных цепочек.

Применение атомарно на таблицу: содержимое цепочек заменяется одним
`iptables-restore --noflush`, поэтому «половинного» состояния не бывает, а
живые соединения продолжают работать — их NAT-привязка уже лежит в conntrack.
"""

import asyncio
import hashlib
import json
import logging
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.models.dnat import NODE_API_PORT, DnatRule

logger = logging.getLogger(__name__)

CHAIN_PREROUTING = "MON_DNAT"
CHAIN_POSTROUTING = "MON_DNAT_POST"
CHAIN_FORWARD = "MON_DNAT_FWD"
COMMENT_PREFIX = "mon-dnat:"
RELATED_COMMENT = f"{COMMENT_PREFIX}related"

# (таблица, встроенная цепочка, наша цепочка)
JUMPS: tuple[tuple[str, str, str], ...] = (
    ("nat", "PREROUTING", CHAIN_PREROUTING),
    ("nat", "POSTROUTING", CHAIN_POSTROUTING),
    ("filter", "FORWARD", CHAIN_FORWARD),
)

IP_FORWARD_PATH = Path("/proc/sys/net/ipv4/ip_forward")

XTABLES_LOCK_WAIT_SEC = 5
COMMAND_TIMEOUT_SEC = 30
SELF_HEAL_INTERVAL_SEC = 30
STATE_FILE_NAME = "dnat_rules.json"


# ---------------------------------------------------------------------------
# Чистые функции: хэш, проверка, генерация правил, разбор дампа
# ---------------------------------------------------------------------------

def normalize_rule(rule: dict) -> dict:
    """Каноничный вид правила для хэша. Комментарий не участвует.
    Формула обязана совпадать с compute_rules_hash в панели."""
    listen_port = int(rule.get("listen_port", 0))
    end = rule.get("listen_port_end")
    end = int(end) if end not in (None, "", 0) else None
    if end == listen_port:
        end = None
    return {
        "name": str(rule.get("name", "")),
        "protocol": (rule.get("protocol") or "tcp").lower(),
        "listen_port": listen_port,
        "listen_port_end": end,
        "target_ip": str(rule.get("target_ip", "")).strip(),
        "target_port": int(rule.get("target_port") or 0),
        "masquerade": bool(rule.get("masquerade", True)),
        "enabled": bool(rule.get("enabled", True)),
    }


def compute_rules_hash(rules: list[dict]) -> str:
    canonical = sorted((normalize_rule(r) for r in rules), key=lambda r: r["name"])
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_rules(rules: list[DnatRule]) -> Optional[str]:
    """Причина отказа или None. Проверяются только включённые правила: выключенное
    в netfilter не попадает и ни с чем не конфликтует."""
    seen: set[str] = set()
    for rule in rules:
        if rule.name in seen:
            return f"Duplicate rule name '{rule.name}'"
        seen.add(rule.name)

    active = [r for r in rules if r.enabled]
    for rule in active:
        if "tcp" in rule.protocols() and rule.covers_port(NODE_API_PORT):
            return (
                f"Rule '{rule.name}' covers node API port {NODE_API_PORT}/tcp — "
                "panel would lose connection to this node"
            )

    for index, rule in enumerate(active):
        low, high = rule.port_range
        for other in active[index + 1:]:
            if not set(rule.protocols()) & set(other.protocols()):
                continue
            other_low, other_high = other.port_range
            if low <= other_high and other_low <= high:
                return f"Rules '{rule.name}' and '{other.name}' overlap on the same ports"
    return None


def _port_spec(rule: DnatRule) -> str:
    low, high = rule.port_range
    return str(low) if low == high else f"{low}:{high}"


def _target_port_spec(rule: DnatRule) -> str:
    """Порт(ы), под которыми поток приходит к цели: явный target_port или те же, что на входе."""
    return str(rule.target_port) if rule.target_port else _port_spec(rule)


def _destination(rule: DnatRule) -> str:
    return f"{rule.target_ip}:{rule.target_port}" if rule.target_port else rule.target_ip


def _comment(tag: str) -> str:
    return f'-m comment --comment "{COMMENT_PREFIX}{tag}"'


def build_restore_script(rules: list[DnatRule]) -> str:
    """Текст для `iptables-restore --noflush`: наши цепочки объявляются заново и
    заполняются с нуля, остальное содержимое таблиц не трогается."""
    nat: list[str] = []
    fwd: list[str] = []
    for rule in rules:
        if not rule.enabled:
            continue
        for proto in rule.protocols():
            nat.append(
                f"-A {CHAIN_PREROUTING} -p {proto} --dport {_port_spec(rule)} "
                f"{_comment(rule.name)} -j DNAT --to-destination {_destination(rule)}"
            )
            if rule.masquerade:
                nat.append(
                    f"-A {CHAIN_POSTROUTING} -p {proto} -d {rule.target_ip} "
                    f"--dport {_target_port_spec(rule)} -m conntrack --ctstate DNAT "
                    f"{_comment(rule.name)} -j MASQUERADE"
                )
            fwd.append(
                f"-A {CHAIN_FORWARD} -p {proto} -d {rule.target_ip} "
                f"--dport {_target_port_spec(rule)} -m conntrack --ctstate DNAT "
                f"{_comment(rule.name + ':in')} -j ACCEPT"
            )
            fwd.append(
                f"-A {CHAIN_FORWARD} -p {proto} -s {rule.target_ip} "
                f"--sport {_target_port_spec(rule)} -m conntrack --ctstate DNAT "
                f"{_comment(rule.name + ':out')} -j ACCEPT"
            )
    # ICMP-ошибки (в т.ч. fragmentation needed для PMTUD) к проброшенным потокам
    fwd.append(
        f'-A {CHAIN_FORWARD} -m conntrack --ctstate RELATED -m comment --comment "{RELATED_COMMENT}" -j ACCEPT'
    )

    # Объявление `:CHAIN` создаёт цепочку или, при --noflush, очищает уже
    # существующую; явный -F дублирует это намеренно — поведение объявления
    # в noflush-режиме в самом iptables помечено как «apparently»
    lines = [
        "*nat",
        f":{CHAIN_PREROUTING} - [0:0]",
        f":{CHAIN_POSTROUTING} - [0:0]",
        f"-F {CHAIN_PREROUTING}",
        f"-F {CHAIN_POSTROUTING}",
        *nat,
        "COMMIT",
        "*filter",
        f":{CHAIN_FORWARD} - [0:0]",
        f"-F {CHAIN_FORWARD}",
        *fwd,
        "COMMIT",
    ]
    return "\n".join(lines) + "\n"


def parse_dump(dump: str) -> tuple[list[tuple[str, str, int, int]], set[tuple[str, str]]]:
    """Разобрать `iptables-save -c`: помеченные правила → (цепочка, метка без
    префикса, пакеты, байты) и множество джампов (цепочка, цель)."""
    marked: list[tuple[str, str, int, int]] = []
    jumps: set[tuple[str, str]] = set()
    for line in dump.splitlines():
        if not line.startswith("["):
            continue
        counter, separator, rule = line[1:].partition("] ")
        if not separator:
            continue
        try:
            tokens = shlex.split(rule)
        except ValueError:
            tokens = rule.split()
        if len(tokens) < 4 or tokens[0] != "-A":
            continue
        chain = tokens[1]
        target = _argument(tokens, "-j")
        if target and target.startswith("MON_DNAT"):
            jumps.add((chain, target))
        comment = _argument(tokens, "--comment")
        if not comment or not comment.startswith(COMMENT_PREFIX):
            continue
        packets_str, _, bytes_str = counter.partition(":")
        packets = int(packets_str) if packets_str.isdigit() else 0
        byte_count = int(bytes_str) if bytes_str.isdigit() else 0
        marked.append((chain, comment[len(COMMENT_PREFIX):], packets, byte_count))
    return marked, jumps


def _argument(tokens: list[str], flag: str) -> Optional[str]:
    try:
        return tokens[tokens.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def summarize(
    rules: list[DnatRule], nat_dump: str, filter_dump: str,
) -> tuple[list[dict], list[str]]:
    """Счётчики по правилам и список того, чего в ядре не хватает."""
    nat_marked, nat_jumps = parse_dump(nat_dump)
    fwd_marked, fwd_jumps = parse_dump(filter_dump)
    present: dict[tuple[str, str], tuple[int, int]] = {}
    for chain, tag, packets, byte_count in nat_marked + fwd_marked:
        old_packets, old_bytes = present.get((chain, tag), (0, 0))
        present[(chain, tag)] = (old_packets + packets, old_bytes + byte_count)

    missing: list[str] = []
    for table, builtin, chain in JUMPS:
        jumps = nat_jumps if table == "nat" else fwd_jumps
        if (builtin, chain) not in jumps:
            missing.append(f"jump:{builtin}")

    counters: list[dict] = []
    for rule in rules:
        if not rule.enabled:
            continue
        expected = [(CHAIN_PREROUTING, rule.name), (CHAIN_FORWARD, f"{rule.name}:in"), (CHAIN_FORWARD, f"{rule.name}:out")]
        if rule.masquerade:
            expected.append((CHAIN_POSTROUTING, rule.name))
        rule_present = all(key in present for key in expected)
        if not rule_present:
            missing.append(rule.name)
        conns, _ = present.get((CHAIN_PREROUTING, rule.name), (0, 0))
        packets_in, bytes_in = present.get((CHAIN_FORWARD, f"{rule.name}:in"), (0, 0))
        packets_out, bytes_out = present.get((CHAIN_FORWARD, f"{rule.name}:out"), (0, 0))
        counters.append({
            "name": rule.name,
            "present": rule_present,
            "conns": conns,
            "packets_in": packets_in,
            "bytes_in": bytes_in,
            "packets_out": packets_out,
            "bytes_out": bytes_out,
        })
    return counters, missing


# ---------------------------------------------------------------------------
# Менеджер
# ---------------------------------------------------------------------------

class DnatManager:
    def __init__(self, state_path: Optional[Path] = None):
        settings = get_settings()
        self._state_path = state_path or Path(settings.traffic_db_path).parent / STATE_FILE_NAME
        # Применение, самолечение и чтение состояния идут через один замок:
        # restore + verify не должны пересекаться с параллельным apply
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()

    # ── subprocess ──

    def _run(self, command: list[str], stdin: Optional[str] = None) -> tuple[int, str, str]:
        try:
            result = subprocess.run(
                command, input=stdin, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            logger.error("Timed out after %ss: %s", COMMAND_TIMEOUT_SEC, " ".join(command))
            return -1, "", "timeout"
        except OSError as exc:
            logger.error("Cannot run %s: %s", command[0], exc)
            return -1, "", str(exc)
        return result.returncode, result.stdout, result.stderr.strip()

    def _iptables(self, args: list[str]) -> bool:
        returncode, _, _ = self._run(["iptables", "-w", str(XTABLES_LOCK_WAIT_SEC), *args])
        return returncode == 0

    def _dump(self, table: str) -> Optional[str]:
        returncode, stdout, _ = self._run(["iptables-save", "-c", "-t", table])
        return stdout if returncode == 0 else None

    def iptables_available(self) -> bool:
        return self._iptables(["-t", "nat", "-L", "PREROUTING", "-n"])

    # ── state file ──

    def load_state(self) -> tuple[list[DnatRule], Optional[str]]:
        if not self._state_path.exists():
            return [], None
        try:
            data = json.loads(self._state_path.read_text())
            rules = [DnatRule(**item) for item in data.get("rules", [])]
            return rules, data.get("applied_at")
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Cannot read DNAT state %s: %s", self._state_path, exc)
            return [], None

    def _save_state(self, rules: list[DnatRule]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "rules": [rule.model_dump() for rule in rules],
            "applied_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        tmp.replace(self._state_path)

    # ── ip_forward ──

    @staticmethod
    def ip_forward_enabled() -> bool:
        try:
            return IP_FORWARD_PATH.read_text().strip() == "1"
        except OSError:
            return False

    def _enable_ip_forward(self) -> bool:
        if self.ip_forward_enabled():
            return True
        try:
            IP_FORWARD_PATH.write_text("1\n")
        except OSError as exc:
            logger.error("Cannot enable ip_forward: %s", exc)
            return False
        return self.ip_forward_enabled()

    # ── netfilter ──

    def _ensure_chains_and_jumps(self) -> Optional[str]:
        for table, builtin, chain in JUMPS:
            self._iptables(["-t", table, "-N", chain])
            if self._iptables(["-t", table, "-C", builtin, "-j", chain]):
                continue
            if not self._iptables(["-t", table, "-I", builtin, "1", "-j", chain]):
                return f"cannot insert jump {builtin} -> {chain} in table {table}"
        return None

    def _restore(self, rules: list[DnatRule]) -> Optional[str]:
        returncode, _, stderr = self._run(
            ["iptables-restore", "--noflush", "-w", str(XTABLES_LOCK_WAIT_SEC)],
            stdin=build_restore_script(rules),
        )
        return None if returncode == 0 else (stderr or f"iptables-restore exited {returncode}")

    def _summary(self, rules: list[DnatRule]) -> Optional[tuple[list[dict], list[str]]]:
        nat_dump = self._dump("nat")
        filter_dump = self._dump("filter")
        if nat_dump is None or filter_dump is None:
            return None
        return summarize(rules, nat_dump, filter_dump)

    def apply(self, rules: list[DnatRule]) -> dict:
        error = validate_rules(rules)
        if error:
            return {"success": False, "message": error, "rules_hash": None, "error_log": None}
        if not self.iptables_available():
            return {
                "success": False, "message": "iptables (nat table) is not available on this node",
                "rules_hash": None, "error_log": None,
            }
        if not self._enable_ip_forward():
            return {
                "success": False, "message": "cannot enable net.ipv4.ip_forward",
                "rules_hash": None, "error_log": None,
            }

        previous, _ = self.load_state()
        error = self._ensure_chains_and_jumps()
        if error:
            return {"success": False, "message": error, "rules_hash": None, "error_log": None}

        error = self._restore(rules)
        if error:
            logger.error("DNAT restore failed, re-applying previous rules: %s", error)
            self._restore(previous)
            return {"success": False, "message": "iptables-restore failed", "rules_hash": None, "error_log": error}

        summary = self._summary(rules)
        if summary is None:
            return {"success": False, "message": "cannot read iptables state after apply", "rules_hash": None, "error_log": None}
        _, missing = summary
        if missing:
            self._restore(previous)
            return {
                "success": False, "message": f"rules missing after apply: {', '.join(missing)}",
                "rules_hash": None, "error_log": None,
            }

        self._save_state(rules)
        rules_hash = compute_rules_hash([r.model_dump() for r in rules])
        active = sum(1 for r in rules if r.enabled)
        logger.info("DNAT rules applied: %s active of %s, hash=%s", active, len(rules), rules_hash[:12])
        return {"success": True, "message": f"Applied {active} rules", "rules_hash": rules_hash, "error_log": None}

    def clear(self) -> dict:
        """Снять всё: джампы, цепочки, файл состояния."""
        for table, builtin, chain in JUMPS:
            while self._iptables(["-t", table, "-C", builtin, "-j", chain]):
                self._iptables(["-t", table, "-D", builtin, "-j", chain])
            self._iptables(["-t", table, "-F", chain])
            self._iptables(["-t", table, "-X", chain])
        try:
            self._state_path.unlink(missing_ok=True)
        except OSError as exc:
            return {"success": False, "message": f"cannot remove state file: {exc}"}
        logger.info("DNAT rules cleared")
        return {"success": True, "message": "DNAT rules removed"}

    def state(self) -> dict:
        rules, applied_at = self.load_state()
        rules_hash = compute_rules_hash([r.model_dump() for r in rules])
        base = {
            "ip_forward": self.ip_forward_enabled(),
            "rules": [r.model_dump() for r in rules],
            "rules_hash": rules_hash,
            "applied_at": applied_at,
        }
        if not rules:
            available = self.iptables_available()
            return {**base, "available": available, "healthy": available, "missing": [],
                    "counters": [], "message": None if available else "iptables is not available"}
        summary = self._summary(rules)
        if summary is None:
            return {**base, "available": False, "healthy": False, "missing": [], "counters": [],
                    "message": "iptables is not available"}
        counters, missing = summary
        return {**base, "available": True, "healthy": not missing, "missing": missing,
                "counters": counters, "message": None}

    def ensure_applied(self) -> Optional[str]:
        """Самолечение: вернуть правила из файла состояния, если ядро их потеряло.
        Возвращает описание действия или None, если чинить нечего."""
        rules, _ = self.load_state()
        if not rules:
            return None
        summary = self._summary(rules)
        if summary is None:
            return None
        _, missing = summary
        if not missing and self.ip_forward_enabled():
            return None
        result = self.apply(rules)
        if result["success"]:
            return f"re-applied after drift: {', '.join(missing) or 'ip_forward'}"
        logger.error("DNAT self-heal failed: %s", result["message"])
        return f"self-heal failed: {result['message']}"

    # ── async-обёртки под общим замком ──

    async def apply_async(self, rules: list[DnatRule]) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self.apply, rules)

    async def clear_async(self) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self.clear)

    async def state_async(self) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self.state)

    async def ensure_async(self) -> Optional[str]:
        async with self._lock:
            return await asyncio.to_thread(self.ensure_applied)

    def request_recheck(self) -> None:
        """Разбудить фоновый цикл: вызывается после операций, сносящих джампы
        (применение firewall-профиля, ufw enable/disable)."""
        self._wake.set()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        rules, _ = self.load_state()
        if rules:
            action = await self.ensure_async()
            logger.info("DNAT startup: %s rules, %s", len(rules), action or "already in place")
        self._task = asyncio.create_task(self._self_heal_loop())

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _self_heal_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=SELF_HEAL_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                action = await self.ensure_async()
                if action:
                    logger.warning("DNAT self-heal: %s", action)
            except Exception as exc:
                logger.error("DNAT self-heal cycle failed: %s", exc, exc_info=True)


_manager: Optional[DnatManager] = None


def get_dnat_manager() -> DnatManager:
    global _manager
    if _manager is None:
        _manager = DnatManager()
    return _manager
