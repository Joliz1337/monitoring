"""IPSet manager for IP/CIDR blocklist management

Works from Docker container by using nsenter to execute commands on host.
Requires container to run with: privileged: true, pid: host

Four lists (two per direction):
- blocklist_permanent / blocklist_out_permanent: permanent blocks (hash:net)
- blocklist_temp / blocklist_out_temp: temporary blocks with timeout (hash:net)

Incoming (in): iptables INPUT chain, match src → DROP
Outgoing (out): iptables OUTPUT chain, match dst → DROP
"""

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PERSISTENT_FILE = "/var/lib/monitoring/blocklist.json"

# Incoming (default)
SET_PERMANENT = "blocklist_permanent"
SET_TEMP = "blocklist_temp"

# Outgoing
SET_OUT_PERMANENT = "blocklist_out_permanent"
SET_OUT_TEMP = "blocklist_out_temp"

DEFAULT_TIMEOUT = 600  # 10 minutes

# Direction config: chain + match flag
_DIR_CONFIG = {
    "in":  {"chain": "INPUT",  "match": "src", "perm": SET_PERMANENT,     "temp": SET_TEMP},
    "out": {"chain": "OUTPUT", "match": "dst", "perm": SET_OUT_PERMANENT, "temp": SET_OUT_TEMP},
}


@dataclass
class DirectionStatus:
    permanent_count: int
    temp_count: int
    iptables_rules_exist: bool


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
    
    def _run_iptables(self, args: list[str]) -> tuple[bool, str, str]:
        return self._run_cmd(["iptables"] + args)

    # ── helpers to resolve direction → set names / chain ──

    def _get_dir_cfg(self, direction: str) -> dict:
        return _DIR_CONFIG.get(direction, _DIR_CONFIG["in"])

    def _resolve_set(self, permanent: bool, direction: str = "in") -> str:
        cfg = self._get_dir_cfg(direction)
        return cfg["perm"] if permanent else cfg["temp"]
    
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
        
        self._load_permanent_ips()
        
        self._initialized = True
        logger.info("IpsetManager initialized successfully (in + out)")
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
            in_ips = self.list_ips(permanent=True, direction="in")
            out_ips = self.list_ips(permanent=True, direction="out")
            data = {
                'in_permanent': in_ips,
                'out_permanent': out_ips,
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
                if ips:
                    count = 0
                    for ip in ips:
                        success, _ = self.add_ip(ip, permanent=True, direction=direction, save=False)
                        if success:
                            count += 1
                    logger.info(f"Loaded {count} {direction} permanent IPs from file")
        except Exception as e:
            logger.warning(f"Failed to load permanent IPs: {e}")
    
    # ── core operations ──
    
    def _ip_in_set(self, ip: str, set_name: str) -> bool:
        success, _, _ = self._run_ipset(["test", set_name, ip])
        return success
    
    def add_ip(self, ip: str, permanent: bool = True, direction: str = "in", save: bool = True) -> tuple[bool, str]:
        ip = self._normalize_ip(ip)
        if not self._validate_ip_cidr(ip):
            return False, f"Invalid IP/CIDR: {ip}"
        
        set_name = self._resolve_set(permanent, direction)
        
        if self._ip_in_set(ip, set_name):
            return True, f"{ip} already in {set_name}"
        
        args = ["add", set_name, ip]
        if not permanent:
            args.extend(["timeout", str(self._temp_timeout)])
        
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
        
        self._save_config()
        logger.info(f"Changed temp timeout to {seconds}s")
        return True, f"Timeout changed to {seconds} seconds"
    
    def bulk_add(self, ips: list[str], permanent: bool = True, direction: str = "in") -> tuple[int, int, list[str]]:
        success_count = 0
        fail_count = 0
        errors = []
        for ip in ips:
            success, msg = self.add_ip(ip, permanent=permanent, direction=direction, save=False)
            if success:
                success_count += 1
            else:
                fail_count += 1
                errors.append(f"{ip}: {msg}")
        if permanent and success_count > 0:
            self._save_config()
        return success_count, fail_count, errors
    
    def bulk_remove(self, ips: list[str], permanent: bool = True, direction: str = "in") -> tuple[int, int, list[str]]:
        success_count = 0
        fail_count = 0
        errors = []
        for ip in ips:
            success, msg = self.remove_ip(ip, permanent=permanent, direction=direction)
            if success:
                success_count += 1
            else:
                fail_count += 1
                errors.append(f"{ip}: {msg}")
        return success_count, fail_count, errors
    
    def sync(self, ips: list[str], permanent: bool = True, direction: str = "in") -> tuple[bool, str, dict]:
        set_name = self._resolve_set(permanent, direction)
        
        normalized_ips = set()
        invalid_ips = []
        for ip in ips:
            ip = self._normalize_ip(ip)
            if self._validate_ip_cidr(ip):
                normalized_ips.add(ip)
            else:
                invalid_ips.append(ip)
        
        current_ips = set(self.list_ips(permanent=permanent, direction=direction))
        new_ips = normalized_ips
        
        to_add = new_ips - current_ips
        to_remove = current_ips - new_ips
        
        added = 0
        removed = 0
        
        for ip in to_remove:
            success, _ = self.remove_ip(ip, permanent=permanent, direction=direction)
            if success:
                removed += 1
        
        for ip in to_add:
            success, _ = self.add_ip(ip, permanent=permanent, direction=direction, save=False)
            if success:
                added += 1
        
        if permanent:
            self._save_config()
        
        result = {
            'total': len(new_ips),
            'added': added,
            'removed': removed,
            'invalid': invalid_ips
        }
        logger.info(f"Synced {set_name}: added {added}, removed {removed}")
        return True, f"Synced {set_name}", result
    
    def get_status(self) -> IpsetStatus:
        def _dir_status(direction: str) -> DirectionStatus:
            cfg = self._get_dir_cfg(direction)
            return DirectionStatus(
                permanent_count=len(self.list_ips(permanent=True, direction=direction)),
                temp_count=len(self.list_ips(permanent=False, direction=direction)),
                iptables_rules_exist=(
                    self._iptables_rule_exists(cfg["perm"], direction)
                    and self._iptables_rule_exists(cfg["temp"], direction)
                ),
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
