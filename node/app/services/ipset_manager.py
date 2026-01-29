"""IPSet manager for IP/CIDR blocklist management

Works from Docker container by using nsenter to execute commands on host.
Requires container to run with: privileged: true, pid: host

Two lists:
- blocklist_permanent: permanent blocks (hash:net)
- blocklist_temp: temporary blocks with timeout (hash:net)
"""

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PERSISTENT_FILE = "/var/lib/monitoring/blocklist.json"
SET_PERMANENT = "blocklist_permanent"
SET_TEMP = "blocklist_temp"
DEFAULT_TIMEOUT = 300  # 5 minutes


@dataclass
class IpsetStatus:
    """Status of ipset lists"""
    permanent_count: int
    temp_count: int
    temp_timeout: int
    iptables_rules_exist: bool


class IpsetManager:
    """Manages ipset blocklists via nsenter (for Docker with pid: host)"""
    
    def __init__(self):
        self._use_nsenter = self._check_nsenter_needed()
        self._temp_timeout = DEFAULT_TIMEOUT
        self._initialized = False
    
    def _check_nsenter_needed(self) -> bool:
        """Check if we're in a container and need nsenter"""
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
        """Run command on host and return success, stdout, stderr"""
        if self._use_nsenter:
            cmd = ["nsenter", "-t", "1", "-m", "-u", "-n", "-i", "--"] + cmd
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except FileNotFoundError:
            return False, "", "Command not found"
        except subprocess.TimeoutExpired:
            return False, "", "Command timed out"
        except Exception as e:
            return False, "", str(e)
    
    def _run_ipset(self, args: list[str]) -> tuple[bool, str, str]:
        """Run ipset command"""
        return self._run_cmd(["ipset"] + args)
    
    def _run_iptables(self, args: list[str]) -> tuple[bool, str, str]:
        """Run iptables command"""
        return self._run_cmd(["iptables"] + args)
    
    def _validate_ip_cidr(self, ip: str) -> bool:
        """Validate IP address or CIDR notation"""
        ip = ip.strip()
        if not ip:
            return False
        
        # IPv4 CIDR pattern
        cidr_pattern = r'^(\d{1,3}\.){3}\d{1,3}(/\d{1,2})?$'
        if not re.match(cidr_pattern, ip):
            return False
        
        # Validate octets
        parts = ip.split('/')[0].split('.')
        for part in parts:
            if int(part) > 255:
                return False
        
        # Validate CIDR prefix
        if '/' in ip:
            prefix = int(ip.split('/')[1])
            if prefix < 0 or prefix > 32:
                return False
        
        return True
    
    def _normalize_ip(self, ip: str) -> str:
        """Normalize IP/CIDR format"""
        ip = ip.strip()
        # Remove /32 suffix for single IPs
        if ip.endswith('/32'):
            ip = ip[:-3]
        return ip
    
    def _set_exists(self, set_name: str) -> bool:
        """Check if ipset exists"""
        success, _, _ = self._run_ipset(["list", set_name])
        return success
    
    def _create_set(self, set_name: str, with_timeout: bool = False) -> tuple[bool, str]:
        """Create ipset if not exists"""
        if self._set_exists(set_name):
            return True, f"Set {set_name} already exists"
        
        args = ["create", set_name, "hash:net", "family", "inet", "hashsize", "4096", "maxelem", "1000000"]
        if with_timeout:
            args.extend(["timeout", str(self._temp_timeout)])
        
        success, stdout, stderr = self._run_ipset(args)
        if success:
            logger.info(f"Created ipset: {set_name}")
            return True, f"Set {set_name} created"
        else:
            logger.error(f"Failed to create ipset {set_name}: {stderr}")
            return False, f"Failed to create set: {stderr}"
    
    def _iptables_rule_exists(self, set_name: str) -> bool:
        """Check if iptables rule for set exists"""
        success, stdout, _ = self._run_iptables(["-C", "INPUT", "-m", "set", "--match-set", set_name, "src", "-j", "DROP"])
        return success
    
    def _add_iptables_rule(self, set_name: str) -> tuple[bool, str]:
        """Add iptables DROP rule for ipset"""
        if self._iptables_rule_exists(set_name):
            return True, f"Rule for {set_name} already exists"
        
        success, stdout, stderr = self._run_iptables(["-I", "INPUT", "-m", "set", "--match-set", set_name, "src", "-j", "DROP"])
        if success:
            logger.info(f"Added iptables rule for {set_name}")
            return True, f"Rule for {set_name} added"
        else:
            logger.error(f"Failed to add iptables rule for {set_name}: {stderr}")
            return False, f"Failed to add rule: {stderr}"
    
    def _remove_iptables_rule(self, set_name: str) -> tuple[bool, str]:
        """Remove iptables DROP rule for ipset"""
        if not self._iptables_rule_exists(set_name):
            return True, f"Rule for {set_name} does not exist"
        
        success, stdout, stderr = self._run_iptables(["-D", "INPUT", "-m", "set", "--match-set", set_name, "src", "-j", "DROP"])
        if success:
            logger.info(f"Removed iptables rule for {set_name}")
            return True, f"Rule for {set_name} removed"
        else:
            return False, f"Failed to remove rule: {stderr}"
    
    def init_sets(self) -> tuple[bool, str]:
        """Initialize ipset lists and iptables rules"""
        if self._initialized:
            return True, "Already initialized"
        
        # Ensure directory exists
        self._run_cmd(["mkdir", "-p", "/var/lib/monitoring"])
        
        # Load config (timeout)
        self._load_config()
        
        # Create permanent set
        success, msg = self._create_set(SET_PERMANENT, with_timeout=False)
        if not success:
            return False, f"Failed to create permanent set: {msg}"
        
        # Create temp set with timeout
        success, msg = self._create_set(SET_TEMP, with_timeout=True)
        if not success:
            return False, f"Failed to create temp set: {msg}"
        
        # Add iptables rules
        success, msg = self._add_iptables_rule(SET_PERMANENT)
        if not success:
            return False, f"Failed to add iptables rule for permanent: {msg}"
        
        success, msg = self._add_iptables_rule(SET_TEMP)
        if not success:
            return False, f"Failed to add iptables rule for temp: {msg}"
        
        # Load permanent IPs from file
        self._load_permanent_ips()
        
        self._initialized = True
        logger.info("IpsetManager initialized successfully")
        return True, "Initialized successfully"
    
    def _load_config(self):
        """Load config from persistent file"""
        try:
            if os.path.exists(PERSISTENT_FILE):
                with open(PERSISTENT_FILE, 'r') as f:
                    data = json.load(f)
                    self._temp_timeout = data.get('temp_timeout', DEFAULT_TIMEOUT)
        except Exception as e:
            logger.warning(f"Failed to load config: {e}")
    
    def _save_config(self):
        """Save config and permanent IPs to file"""
        try:
            permanent_ips = self.list_ips(permanent=True)
            data = {
                'permanent': permanent_ips,
                'temp_timeout': self._temp_timeout
            }
            
            # Ensure directory exists
            Path(PERSISTENT_FILE).parent.mkdir(parents=True, exist_ok=True)
            
            with open(PERSISTENT_FILE, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.debug("Saved config to file")
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
    
    def _load_permanent_ips(self):
        """Load permanent IPs from file on startup"""
        try:
            if not os.path.exists(PERSISTENT_FILE):
                return
            
            with open(PERSISTENT_FILE, 'r') as f:
                data = json.load(f)
            
            ips = data.get('permanent', [])
            if ips:
                count = 0
                for ip in ips:
                    success, _ = self.add_ip(ip, permanent=True, save=False)
                    if success:
                        count += 1
                logger.info(f"Loaded {count} permanent IPs from file")
        except Exception as e:
            logger.warning(f"Failed to load permanent IPs: {e}")
    
    def add_ip(self, ip: str, permanent: bool = True, save: bool = True) -> tuple[bool, str]:
        """Add IP/CIDR to blocklist"""
        ip = self._normalize_ip(ip)
        
        if not self._validate_ip_cidr(ip):
            return False, f"Invalid IP/CIDR: {ip}"
        
        set_name = SET_PERMANENT if permanent else SET_TEMP
        
        # Check if already exists
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
        else:
            # Check if already exists (race condition)
            if "already added" in stderr.lower() or "already in set" in stderr.lower():
                return True, f"{ip} already in {set_name}"
            logger.error(f"Failed to add {ip} to {set_name}: {stderr}")
            return False, f"Failed to add: {stderr}"
    
    def _ip_in_set(self, ip: str, set_name: str) -> bool:
        """Check if IP is in set"""
        success, _, _ = self._run_ipset(["test", set_name, ip])
        return success
    
    def remove_ip(self, ip: str, permanent: bool = True) -> tuple[bool, str]:
        """Remove IP/CIDR from blocklist"""
        ip = self._normalize_ip(ip)
        
        if not self._validate_ip_cidr(ip):
            return False, f"Invalid IP/CIDR: {ip}"
        
        set_name = SET_PERMANENT if permanent else SET_TEMP
        
        success, stdout, stderr = self._run_ipset(["del", set_name, ip])
        
        if success:
            logger.info(f"Removed {ip} from {set_name}")
            if permanent:
                self._save_config()
            return True, f"Removed {ip} from {set_name}"
        else:
            # Not in set is not an error
            if "not in set" in stderr.lower() or "element is missing" in stderr.lower():
                return True, f"{ip} was not in {set_name}"
            logger.error(f"Failed to remove {ip} from {set_name}: {stderr}")
            return False, f"Failed to remove: {stderr}"
    
    def list_ips(self, permanent: bool = True) -> list[str]:
        """Get list of IPs in blocklist"""
        set_name = SET_PERMANENT if permanent else SET_TEMP
        
        success, stdout, stderr = self._run_ipset(["list", set_name])
        
        if not success:
            logger.error(f"Failed to list {set_name}: {stderr}")
            return []
        
        # Parse output - IPs are after "Members:" line
        ips = []
        in_members = False
        for line in stdout.split('\n'):
            line = line.strip()
            if line.startswith('Members:'):
                in_members = True
                continue
            if in_members and line:
                # Format: "IP timeout VALUE" or just "IP"
                parts = line.split()
                if parts:
                    ips.append(parts[0])
        
        return ips
    
    def clear_set(self, permanent: bool = True) -> tuple[bool, str]:
        """Clear all IPs from blocklist"""
        set_name = SET_PERMANENT if permanent else SET_TEMP
        
        success, stdout, stderr = self._run_ipset(["flush", set_name])
        
        if success:
            logger.info(f"Cleared {set_name}")
            if permanent:
                self._save_config()
            return True, f"Cleared {set_name}"
        else:
            logger.error(f"Failed to clear {set_name}: {stderr}")
            return False, f"Failed to clear: {stderr}"
    
    def set_timeout(self, seconds: int) -> tuple[bool, str]:
        """Change timeout for temp list (requires recreation)"""
        if seconds < 1 or seconds > 86400 * 30:  # Max 30 days
            return False, "Invalid timeout (1 - 2592000 seconds)"
        
        old_timeout = self._temp_timeout
        self._temp_timeout = seconds
        
        # Need to recreate the set with new timeout
        # First, remove iptables rule
        self._remove_iptables_rule(SET_TEMP)
        
        # Destroy old set
        self._run_ipset(["destroy", SET_TEMP])
        
        # Create new set with new timeout
        success, msg = self._create_set(SET_TEMP, with_timeout=True)
        if not success:
            # Rollback
            self._temp_timeout = old_timeout
            self._create_set(SET_TEMP, with_timeout=True)
            self._add_iptables_rule(SET_TEMP)
            return False, f"Failed to recreate set: {msg}"
        
        # Re-add iptables rule
        success, msg = self._add_iptables_rule(SET_TEMP)
        if not success:
            return False, f"Failed to re-add iptables rule: {msg}"
        
        self._save_config()
        logger.info(f"Changed temp timeout to {seconds}s")
        return True, f"Timeout changed to {seconds} seconds"
    
    def bulk_add(self, ips: list[str], permanent: bool = True) -> tuple[int, int, list[str]]:
        """Add multiple IPs. Returns (success_count, fail_count, errors)"""
        success_count = 0
        fail_count = 0
        errors = []
        
        for ip in ips:
            success, msg = self.add_ip(ip, permanent=permanent, save=False)
            if success:
                success_count += 1
            else:
                fail_count += 1
                errors.append(f"{ip}: {msg}")
        
        if permanent and success_count > 0:
            self._save_config()
        
        return success_count, fail_count, errors
    
    def bulk_remove(self, ips: list[str], permanent: bool = True) -> tuple[int, int, list[str]]:
        """Remove multiple IPs. Returns (success_count, fail_count, errors)"""
        success_count = 0
        fail_count = 0
        errors = []
        
        for ip in ips:
            success, msg = self.remove_ip(ip, permanent=permanent)
            if success:
                success_count += 1
            else:
                fail_count += 1
                errors.append(f"{ip}: {msg}")
        
        return success_count, fail_count, errors
    
    def sync(self, ips: list[str], permanent: bool = True) -> tuple[bool, str, dict]:
        """Replace entire list with new IPs (atomic sync)"""
        set_name = SET_PERMANENT if permanent else SET_TEMP
        
        # Normalize and deduplicate
        normalized_ips = set()
        invalid_ips = []
        for ip in ips:
            ip = self._normalize_ip(ip)
            if self._validate_ip_cidr(ip):
                normalized_ips.add(ip)
            else:
                invalid_ips.append(ip)
        
        current_ips = set(self.list_ips(permanent=permanent))
        new_ips = normalized_ips
        
        # Calculate diff
        to_add = new_ips - current_ips
        to_remove = current_ips - new_ips
        
        # Apply changes
        added = 0
        removed = 0
        
        for ip in to_remove:
            success, _ = self.remove_ip(ip, permanent=permanent)
            if success:
                removed += 1
        
        for ip in to_add:
            success, _ = self.add_ip(ip, permanent=permanent, save=False)
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
        """Get status of ipset lists"""
        return IpsetStatus(
            permanent_count=len(self.list_ips(permanent=True)),
            temp_count=len(self.list_ips(permanent=False)),
            temp_timeout=self._temp_timeout,
            iptables_rules_exist=self._iptables_rule_exists(SET_PERMANENT) and self._iptables_rule_exists(SET_TEMP)
        )


# Singleton instance
_manager: Optional[IpsetManager] = None


def get_ipset_manager() -> IpsetManager:
    """Get or create IpsetManager instance"""
    global _manager
    if _manager is None:
        _manager = IpsetManager()
    return _manager
