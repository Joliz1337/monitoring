"""IPSet manager for IP/CIDR blocklist management

Works from Docker container by using nsenter to execute commands on host.
Requires container to run with: privileged: true, pid: host

Four block lists (two per direction):
- blocklist_permanent / blocklist_out_permanent: permanent blocks (hash:net)
- blocklist_temp / blocklist_out_temp: temporary blocks with timeout (hash:net)

Two allow lists (one per direction):
- allowlist / allowlist_out: trusted IPs that must always pass (hash:net)

Incoming (in): iptables INPUT chain, match src → DROP (block) / ACCEPT (allow)
Outgoing (out): iptables OUTPUT chain, match dst → DROP (block) / ACCEPT (allow)

Правило ACCEPT белого списка всегда вставляется в позицию 1 цепочки (выше всех
DROP) — iptables идёт сверху вниз и ACCEPT обрывает обход, поэтому доверенный IP
проходит ещё до блокировок, даже если попадает под заблокированный CIDR.
"""

import ipaddress
import json
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PERSISTENT_FILE = "/var/lib/monitoring/blocklist.json"

# Приватные/служебные диапазоны никогда не попадают в block-сеты: DROP по ним
# убивает loopback, docker-bridge и внутренние сети хостера (инцидент с
# firehol_level1, который включает bogon-диапазоны для бордер-роутеров).
NON_PUBLIC_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
))


def is_public_range(ip: str) -> bool:
    """True, если IP/CIDR не пересекается с приватными/служебными диапазонами."""
    try:
        net = ipaddress.ip_network(ip, strict=False)
    except ValueError:
        return False
    return not any(net.overlaps(bad) for bad in NON_PUBLIC_NETS)

# Incoming (default)
SET_PERMANENT = "blocklist_permanent"
SET_TEMP = "blocklist_temp"

# Outgoing
SET_OUT_PERMANENT = "blocklist_out_permanent"
SET_OUT_TEMP = "blocklist_out_temp"

# Allow lists (whitelist) — one per direction, always permanent
SET_ALLOW = "allowlist"
SET_ALLOW_OUT = "allowlist_out"

DEFAULT_TIMEOUT = 600  # 10 minutes

# Direction config: chain + match flag
_DIR_CONFIG = {
    "in":  {"chain": "INPUT",  "match": "src", "perm": SET_PERMANENT,     "temp": SET_TEMP},
    "out": {"chain": "OUTPUT", "match": "dst", "perm": SET_OUT_PERMANENT, "temp": SET_OUT_TEMP},
}

# Allow config: chain + match flag + set name per direction
_ALLOW_CONFIG = {
    "in":  {"chain": "INPUT",  "match": "src", "set": SET_ALLOW},
    "out": {"chain": "OUTPUT", "match": "dst", "set": SET_ALLOW_OUT},
}


@dataclass
class DirectionStatus:
    permanent_count: int
    temp_count: int
    iptables_rules_exist: bool
    allow_count: int = 0


@dataclass
class IpsetStatus:
    """Status of ipset lists for both directions"""
    incoming: DirectionStatus
    outgoing: DirectionStatus
    temp_timeout: int


class IpsetManager:
    """Manages ipset blocklists via nsenter (for Docker with pid: host)"""
    
    def __init__(self):
        self._use_nsenter = self._check_nsenter_needed()
        self._temp_timeout = DEFAULT_TIMEOUT
        self._initialized = False
        # Мутации сетов сериализуются: эндпоинты выполняются в threadpool,
        # параллельные sync с панели не должны перемешивать diff-ы.
        self._mutate_lock = threading.Lock()
    
    def _check_nsenter_needed(self) -> bool:
        if os.path.exists('/.dockerenv'):
            return True
        try:
            with open('/proc/1/cgroup', 'r') as f:
                if 'docker' in f.read():
                    return True
        except Exception:
            pass
        return False
    
    def _run_cmd(self, cmd: list[str], timeout: int = 30) -> tuple[bool, str, str]:
        if self._use_nsenter:
            cmd = ["nsenter", "-t", "1", "-m", "-u", "-n", "-i", "--"] + cmd
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except FileNotFoundError:
            return False, "", "Command not found"
        except subprocess.TimeoutExpired:
            return False, "", "Command timed out"
        except Exception as e:
            return False, "", str(e)
    
    def _run_ipset(self, args: list[str]) -> tuple[bool, str, str]:
        return self._run_cmd(["ipset"] + args)

    def _run_ipset_restore(self, lines: list[str], timeout: int = 180) -> tuple[bool, str, str]:
        """Применить пачку add/del одним процессом `ipset restore`.

        Per-IP вызовы ipset (2 subprocess на запись) на списках в десятки тысяч
        записей блокируют API ноды на десятки минут; restore применяет весь diff
        за доли секунды. `-exist` — уже существующие/отсутствующие записи не ошибка.
        """
        if not lines:
            return True, "", ""
        cmd = ["ipset", "-exist", "restore"]
        if self._use_nsenter:
            cmd = ["nsenter", "-t", "1", "-m", "-u", "-n", "-i", "--"] + cmd
        try:
            result = subprocess.run(
                cmd, input="\n".join(lines) + "\n",
                capture_output=True, text=True, timeout=timeout,
            )
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            return False, "", "ipset restore timed out"
        except Exception as e:
            return False, "", str(e)

    def _set_count(self, set_name: str) -> int:
        """Число записей из заголовка `ipset list -t` — не выгружает весь сет."""
        success, stdout, _ = self._run_ipset(["list", set_name, "-t"])
        if not success:
            return 0
        for line in stdout.split('\n'):
            if line.startswith('Number of entries:'):
                try:
                    return int(line.split(':', 1)[1].strip())
                except ValueError:
                    return 0
        return 0

    def _run_iptables(self, args: list[str]) -> tuple[bool, str, str]:
        return self._run_cmd(["iptables"] + args)

    # ── helpers to resolve direction → set names / chain ──

    def _get_dir_cfg(self, direction: str) -> dict:
        return _DIR_CONFIG.get(direction, _DIR_CONFIG["in"])

    def _resolve_set(self, permanent: bool, direction: str = "in") -> str:
        cfg = self._get_dir_cfg(direction)
        return cfg["perm"] if permanent else cfg["temp"]

    def _get_allow_cfg(self, direction: str) -> dict:
        return _ALLOW_CONFIG.get(direction, _ALLOW_CONFIG["in"])
    
    # ── IP validation ──
    
    def _validate_ip_cidr(self, ip: str) -> bool:
        ip = ip.strip()
        if not ip:
            return False
        cidr_pattern = r'^(\d{1,3}\.){3}\d{1,3}(/\d{1,2})?$'
        if not re.match(cidr_pattern, ip):
            return False
        parts = ip.split('/')[0].split('.')
        for part in parts:
            if int(part) > 255:
                return False
        if '/' in ip:
            prefix = int(ip.split('/')[1])
            if prefix < 0 or prefix > 32:
                return False
        return True
    
    def _normalize_ip(self, ip: str) -> str:
        ip = ip.strip()
        if ip.endswith('/32'):
            ip = ip[:-3]
        return ip
    
    # ── ipset operations ──
    
    def _set_exists(self, set_name: str) -> bool:
        success, _, _ = self._run_ipset(["list", set_name])
        return success
    
    def _create_set(self, set_name: str, with_timeout: bool = False) -> tuple[bool, str]:
        if self._set_exists(set_name):
            return True, f"Set {set_name} already exists"
        args = ["create", set_name, "hash:net", "family", "inet", "hashsize", "4096", "maxelem", "1000000"]
        if with_timeout:
            args.extend(["timeout", str(self._temp_timeout)])
        success, stdout, stderr = self._run_ipset(args)
        if success:
            logger.info(f"Created ipset: {set_name}")
            return True, f"Set {set_name} created"
        logger.error(f"Failed to create ipset {set_name}: {stderr}")
        return False, f"Failed to create set: {stderr}"
    
    # ── iptables rules (direction-aware) ──
    
    def _iptables_rule_exists(self, set_name: str, direction: str = "in") -> bool:
        cfg = self._get_dir_cfg(direction)
        success, _, _ = self._run_iptables([
            "-C", cfg["chain"], "-m", "set",
            "--match-set", set_name, cfg["match"], "-j", "DROP"
        ])
        return success
    
    def _add_iptables_rule(self, set_name: str, direction: str = "in") -> tuple[bool, str]:
        if self._iptables_rule_exists(set_name, direction):
            return True, f"Rule for {set_name} already exists"
        cfg = self._get_dir_cfg(direction)
        success, stdout, stderr = self._run_iptables([
            "-I", cfg["chain"], "-m", "set",
            "--match-set", set_name, cfg["match"], "-j", "DROP"
        ])
        if success:
            logger.info(f"Added iptables {cfg['chain']} rule for {set_name}")
            return True, f"Rule for {set_name} added"
        logger.error(f"Failed to add iptables rule for {set_name}: {stderr}")
        return False, f"Failed to add rule: {stderr}"
    
    def _remove_iptables_rule(self, set_name: str, direction: str = "in") -> tuple[bool, str]:
        if not self._iptables_rule_exists(set_name, direction):
            return True, f"Rule for {set_name} does not exist"
        cfg = self._get_dir_cfg(direction)
        success, stdout, stderr = self._run_iptables([
            "-D", cfg["chain"], "-m", "set",
            "--match-set", set_name, cfg["match"], "-j", "DROP"
        ])
        if success:
            logger.info(f"Removed iptables {cfg['chain']} rule for {set_name}")
            return True, f"Rule for {set_name} removed"
        return False, f"Failed to remove rule: {stderr}"

    # ── allowlist iptables rule (ACCEPT, must sit above DROP rules) ──

    def _ensure_allow_rule_priority(self, direction: str = "in") -> None:
        """Гарантирует, что ACCEPT белого списка стоит первым в цепочке.

        Удаляет существующее правило (если есть) и вставляет в позицию 1, выше
        всех DROP. Вызывается после любой операции, способной всплыть DROP-правило
        наверх (init_sets, set_timeout)."""
        cfg = self._get_allow_cfg(direction)
        rule = ["-m", "set", "--match-set", cfg["set"], cfg["match"], "-j", "ACCEPT"]
        # idempotent: снимаем дубликаты, затем ставим единственное правило на верх
        while self._run_iptables(["-C", cfg["chain"]] + rule)[0]:
            self._run_iptables(["-D", cfg["chain"]] + rule)
        success, _, stderr = self._run_iptables(["-I", cfg["chain"], "1"] + rule)
        if success:
            logger.info(f"Allowlist ACCEPT rule on top of {cfg['chain']} ({direction})")
        else:
            logger.error(f"Failed to add allowlist ACCEPT rule ({direction}): {stderr}")
    
    # ── init ──
    
    def init_sets(self) -> tuple[bool, str]:
        if self._initialized:
            return True, "Already initialized"
        
        self._run_cmd(["mkdir", "-p", "/var/lib/monitoring"])
        self._load_config()
        
        # Create sets + iptables rules for both directions
        for direction, cfg in _DIR_CONFIG.items():
            success, msg = self._create_set(cfg["perm"], with_timeout=False)
            if not success:
                return False, f"Failed to create {direction} permanent set: {msg}"
            
            success, msg = self._create_set(cfg["temp"], with_timeout=True)
            if not success:
                return False, f"Failed to create {direction} temp set: {msg}"
            
            success, msg = self._add_iptables_rule(cfg["perm"], direction)
            if not success:
                return False, f"Failed to add iptables rule for {direction} permanent: {msg}"
            
            success, msg = self._add_iptables_rule(cfg["temp"], direction)
            if not success:
                return False, f"Failed to add iptables rule for {direction} temp: {msg}"

        # Allow lists: create set, затем поставить ACCEPT выше всех DROP
        for direction, cfg in _ALLOW_CONFIG.items():
            success, msg = self._create_set(cfg["set"], with_timeout=False)
            if not success:
                return False, f"Failed to create {direction} allow set: {msg}"
            self._ensure_allow_rule_priority(direction)

        self._load_permanent_ips()
        self._load_allow_ips()

        self._initialized = True
        logger.info("IpsetManager initialized successfully (in + out, block + allow)")
        return True, "Initialized successfully"
    
    # ── persistence ──
    
    def _load_config(self):
        try:
            if not os.path.exists(PERSISTENT_FILE):
                return
            with open(PERSISTENT_FILE, 'r') as f:
                data = json.load(f)
                self._temp_timeout = data.get('temp_timeout', DEFAULT_TIMEOUT)
        except Exception as e:
            logger.warning(f"Failed to load config: {e}")
    
    def _save_config(self):
        try:
            data = {
                'in_permanent': self.list_ips(permanent=True, direction="in"),
                'out_permanent': self.list_ips(permanent=True, direction="out"),
                'in_allow': self.list_allow_ips(direction="in"),
                'out_allow': self.list_allow_ips(direction="out"),
                'temp_timeout': self._temp_timeout,
            }
            Path(PERSISTENT_FILE).parent.mkdir(parents=True, exist_ok=True)
            with open(PERSISTENT_FILE, 'w') as f:
                json.dump(data, f, indent=2)
            logger.debug("Saved config to file")
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
    
    def _load_permanent_ips(self):
        try:
            if not os.path.exists(PERSISTENT_FILE):
                return
            with open(PERSISTENT_FILE, 'r') as f:
                data = json.load(f)
            
            # Backward compat: old format has 'permanent' key (all incoming)
            if 'permanent' in data and 'in_permanent' not in data:
                data['in_permanent'] = data['permanent']
            
            for direction, key in [("in", "in_permanent"), ("out", "out_permanent")]:
                ips = data.get(key, [])
                if not ips:
                    continue
                set_name = self._resolve_set(permanent=True, direction=direction)
                valid, _, skipped = self._prepare_block_ips(ips)
                success, _, stderr = self._run_ipset_restore(
                    [f"add {set_name} {ip}" for ip in sorted(valid)]
                )
                if success:
                    logger.info(f"Loaded {len(valid)} {direction} permanent IPs from file"
                                + (f" (skipped {skipped} non-public)" if skipped else ""))
                else:
                    logger.error(f"Failed to load {direction} permanent IPs: {stderr}")
        except Exception as e:
            logger.warning(f"Failed to load permanent IPs: {e}")

    def _load_allow_ips(self):
        try:
            if not os.path.exists(PERSISTENT_FILE):
                return
            with open(PERSISTENT_FILE, 'r') as f:
                data = json.load(f)

            for direction, key in [("in", "in_allow"), ("out", "out_allow")]:
                ips = data.get(key, [])
                if not ips:
                    continue
                set_name = self._get_allow_cfg(direction)["set"]
                valid = sorted({
                    self._normalize_ip(ip) for ip in ips
                    if self._validate_ip_cidr(self._normalize_ip(ip))
                })
                success, _, stderr = self._run_ipset_restore(
                    [f"add {set_name} {ip}" for ip in valid]
                )
                if success:
                    logger.info(f"Loaded {len(valid)} {direction} allow IPs from file")
                else:
                    logger.error(f"Failed to load {direction} allow IPs: {stderr}")
        except Exception as e:
            logger.warning(f"Failed to load allow IPs: {e}")
    
    # ── core operations ──
    
    def _ip_in_set(self, ip: str, set_name: str) -> bool:
        success, _, _ = self._run_ipset(["test", set_name, ip])
        return success
    
    def add_ip(self, ip: str, permanent: bool = True, direction: str = "in", save: bool = True, timeout: int | None = None) -> tuple[bool, str]:
        ip = self._normalize_ip(ip)
        if not self._validate_ip_cidr(ip):
            return False, f"Invalid IP/CIDR: {ip}"
        if not is_public_range(ip):
            return False, f"Refused to block non-public range: {ip}"

        set_name = self._resolve_set(permanent, direction)

        if self._ip_in_set(ip, set_name):
            return True, f"{ip} already in {set_name}"

        args = ["add", set_name, ip]
        if not permanent:
            effective_timeout = timeout if timeout is not None else self._temp_timeout
            args.extend(["timeout", str(effective_timeout)])
        
        success, stdout, stderr = self._run_ipset(args)
        if success:
            logger.info(f"Added {ip} to {set_name}")
            if permanent and save:
                self._save_config()
            return True, f"Added {ip} to {set_name}"
        if "already added" in stderr.lower() or "already in set" in stderr.lower():
            return True, f"{ip} already in {set_name}"
        logger.error(f"Failed to add {ip} to {set_name}: {stderr}")
        return False, f"Failed to add: {stderr}"
    
    def remove_ip(self, ip: str, permanent: bool = True, direction: str = "in") -> tuple[bool, str]:
        ip = self._normalize_ip(ip)
        if not self._validate_ip_cidr(ip):
            return False, f"Invalid IP/CIDR: {ip}"
        
        set_name = self._resolve_set(permanent, direction)
        success, stdout, stderr = self._run_ipset(["del", set_name, ip])
        
        if success:
            logger.info(f"Removed {ip} from {set_name}")
            if permanent:
                self._save_config()
            return True, f"Removed {ip} from {set_name}"
        if "not in set" in stderr.lower() or "element is missing" in stderr.lower():
            return True, f"{ip} was not in {set_name}"
        logger.error(f"Failed to remove {ip} from {set_name}: {stderr}")
        return False, f"Failed to remove: {stderr}"
    
    def list_ips(self, permanent: bool = True, direction: str = "in") -> list[str]:
        set_name = self._resolve_set(permanent, direction)
        success, stdout, stderr = self._run_ipset(["list", set_name])
        if not success:
            logger.error(f"Failed to list {set_name}: {stderr}")
            return []
        
        ips = []
        in_members = False
        for line in stdout.split('\n'):
            line = line.strip()
            if line.startswith('Members:'):
                in_members = True
                continue
            if in_members and line:
                parts = line.split()
                if parts:
                    ips.append(parts[0])
        return ips
    
    def clear_set(self, permanent: bool = True, direction: str = "in") -> tuple[bool, str]:
        set_name = self._resolve_set(permanent, direction)
        success, stdout, stderr = self._run_ipset(["flush", set_name])
        if success:
            logger.info(f"Cleared {set_name}")
            if permanent:
                self._save_config()
            return True, f"Cleared {set_name}"
        logger.error(f"Failed to clear {set_name}: {stderr}")
        return False, f"Failed to clear: {stderr}"
    
    def set_timeout(self, seconds: int) -> tuple[bool, str]:
        if seconds < 1 or seconds > 86400 * 30:
            return False, "Invalid timeout (1 - 2592000 seconds)"
        
        old_timeout = self._temp_timeout
        self._temp_timeout = seconds
        
        # Recreate temp sets for both directions
        for direction, cfg in _DIR_CONFIG.items():
            temp_set = cfg["temp"]
            self._remove_iptables_rule(temp_set, direction)
            self._run_ipset(["destroy", temp_set])
            
            success, msg = self._create_set(temp_set, with_timeout=True)
            if not success:
                self._temp_timeout = old_timeout
                self._create_set(temp_set, with_timeout=True)
                self._add_iptables_rule(temp_set, direction)
                return False, f"Failed to recreate {direction} temp set: {msg}"
            
            success, msg = self._add_iptables_rule(temp_set, direction)
            if not success:
                return False, f"Failed to re-add iptables rule for {direction} temp: {msg}"

            # temp-DROP всплыл в позицию 1 — вернуть ACCEPT белого списка выше него
            self._ensure_allow_rule_priority(direction)

        self._save_config()
        logger.info(f"Changed temp timeout to {seconds}s")
        return True, f"Timeout changed to {seconds} seconds"
    
    def _prepare_block_ips(self, ips: list[str]) -> tuple[set[str], list[str], int]:
        """Нормализовать входные записи для block-сета.

        Возвращает (валидные, невалидные, число отброшенных приватных)."""
        normalized: set[str] = set()
        invalid: list[str] = []
        skipped_non_public = 0
        for ip in ips:
            ip = self._normalize_ip(ip)
            if not self._validate_ip_cidr(ip):
                invalid.append(ip)
            elif not is_public_range(ip):
                skipped_non_public += 1
            else:
                normalized.add(ip)
        return normalized, invalid, skipped_non_public

    def bulk_add(self, ips: list[str], permanent: bool = True, direction: str = "in", timeout: int | None = None) -> tuple[int, int, list[str]]:
        set_name = self._resolve_set(permanent, direction)
        normalized, invalid, skipped_non_public = self._prepare_block_ips(ips)

        errors = [f"{ip}: Invalid IP/CIDR" for ip in invalid]
        if skipped_non_public:
            errors.append(f"Refused to block {skipped_non_public} non-public range(s)")

        suffix = ""
        if not permanent:
            effective_timeout = timeout if timeout is not None else self._temp_timeout
            suffix = f" timeout {effective_timeout}"

        with self._mutate_lock:
            lines = [f"add {set_name} {ip}{suffix}" for ip in sorted(normalized)]
            success, _, stderr = self._run_ipset_restore(lines)
            if not success:
                logger.error(f"bulk_add restore failed for {set_name}: {stderr}")
                return 0, len(ips), errors + [f"ipset restore: {stderr}"]
            if permanent and normalized:
                self._save_config()

        fail_count = len(invalid) + skipped_non_public
        logger.info(f"Bulk-added {len(normalized)} entries to {set_name}")
        return len(normalized), fail_count, errors

    def bulk_remove(self, ips: list[str], permanent: bool = True, direction: str = "in") -> tuple[int, int, list[str]]:
        set_name = self._resolve_set(permanent, direction)

        normalized: set[str] = set()
        invalid: list[str] = []
        for ip in ips:
            ip = self._normalize_ip(ip)
            if self._validate_ip_cidr(ip):
                normalized.add(ip)
            else:
                invalid.append(ip)

        errors = [f"{ip}: Invalid IP/CIDR" for ip in invalid]

        with self._mutate_lock:
            lines = [f"del {set_name} {ip}" for ip in sorted(normalized)]
            success, _, stderr = self._run_ipset_restore(lines)
            if not success:
                logger.error(f"bulk_remove restore failed for {set_name}: {stderr}")
                return 0, len(ips), errors + [f"ipset restore: {stderr}"]
            if permanent and normalized:
                self._save_config()

        logger.info(f"Bulk-removed {len(normalized)} entries from {set_name}")
        return len(normalized), len(invalid), errors

    def sync(self, ips: list[str], permanent: bool = True, direction: str = "in") -> tuple[bool, str, dict]:
        set_name = self._resolve_set(permanent, direction)
        new_ips, invalid_ips, skipped_non_public = self._prepare_block_ips(ips)

        with self._mutate_lock:
            current_ips = set(self.list_ips(permanent=permanent, direction=direction))

            to_add = new_ips - current_ips
            to_remove = current_ips - new_ips

            lines = [f"del {set_name} {ip}" for ip in sorted(to_remove)]
            lines += [f"add {set_name} {ip}" for ip in sorted(to_add)]
            success, _, stderr = self._run_ipset_restore(lines)
            if not success:
                logger.error(f"sync restore failed for {set_name}: {stderr}")
                return False, f"ipset restore failed: {stderr}", {}

            if permanent:
                self._save_config()

        result = {
            'total': len(new_ips),
            'added': len(to_add),
            'removed': len(to_remove),
            'invalid': invalid_ips,
            'skipped_non_public': skipped_non_public,
        }
        if skipped_non_public:
            logger.warning(f"Sync {set_name}: refused {skipped_non_public} non-public range(s)")
        logger.info(f"Synced {set_name}: added {len(to_add)}, removed {len(to_remove)}")
        return True, f"Synced {set_name}", result

    # ── allow list operations (always permanent, no timeout) ──

    def list_allow_ips(self, direction: str = "in") -> list[str]:
        set_name = self._get_allow_cfg(direction)["set"]
        success, stdout, stderr = self._run_ipset(["list", set_name])
        if not success:
            logger.error(f"Failed to list {set_name}: {stderr}")
            return []

        ips = []
        in_members = False
        for line in stdout.split('\n'):
            line = line.strip()
            if line.startswith('Members:'):
                in_members = True
                continue
            if in_members and line:
                parts = line.split()
                if parts:
                    ips.append(parts[0])
        return ips

    def sync_allow(self, ips: list[str], direction: str = "in") -> tuple[bool, str, dict]:
        set_name = self._get_allow_cfg(direction)["set"]

        normalized_ips = set()
        invalid_ips = []
        for ip in ips:
            ip = self._normalize_ip(ip)
            if self._validate_ip_cidr(ip):
                normalized_ips.add(ip)
            else:
                invalid_ips.append(ip)

        with self._mutate_lock:
            current_ips = set(self.list_allow_ips(direction=direction))
            to_add = normalized_ips - current_ips
            to_remove = current_ips - normalized_ips

            lines = [f"del {set_name} {ip}" for ip in sorted(to_remove)]
            lines += [f"add {set_name} {ip}" for ip in sorted(to_add)]
            success, _, stderr = self._run_ipset_restore(lines)
            if not success:
                logger.error(f"sync_allow restore failed for {set_name}: {stderr}")
                return False, f"ipset restore failed: {stderr}", {}

            self._save_config()

        result = {
            'total': len(normalized_ips),
            'added': len(to_add),
            'removed': len(to_remove),
            'invalid': invalid_ips,
        }
        logger.info(f"Synced {set_name}: added {len(to_add)}, removed {len(to_remove)}")
        return True, f"Synced {set_name}", result

    def get_status(self) -> IpsetStatus:
        def _dir_status(direction: str) -> DirectionStatus:
            cfg = self._get_dir_cfg(direction)
            return DirectionStatus(
                permanent_count=self._set_count(cfg["perm"]),
                temp_count=self._set_count(cfg["temp"]),
                iptables_rules_exist=(
                    self._iptables_rule_exists(cfg["perm"], direction)
                    and self._iptables_rule_exists(cfg["temp"], direction)
                ),
                allow_count=self._set_count(self._get_allow_cfg(direction)["set"]),
            )
        
        return IpsetStatus(
            incoming=_dir_status("in"),
            outgoing=_dir_status("out"),
            temp_timeout=self._temp_timeout,
        )


# Singleton instance
_manager: Optional[IpsetManager] = None


def get_ipset_manager() -> IpsetManager:
    global _manager
    if _manager is None:
        _manager = IpsetManager()
    return _manager
