"""UFW Firewall manager for port management

Works from Docker container by using nsenter to execute commands on host.
Requires container to run with: privileged: true, pid: host
"""

import hashlib
import ipaddress
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BACKUP_DIR = "/etc/monitoring"
MAX_BACKUPS = 5
NODE_API_PORT = 9100

_IP_OR_CIDR_RE = re.compile(
    r'^(?:(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?|[0-9a-fA-F:]+(?:/\d{1,3})?)$'
)


def _is_valid_from_ip(value: str) -> bool:
    """UFW принимает IP или CIDR; исключаем произвольный текст во избежание injection."""
    if not _IP_OR_CIDR_RE.match(value):
        return False
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False
    return True


@dataclass
class FirewallRule:
    """Firewall rule representation"""
    number: int
    port: int
    protocol: str
    action: str  # ALLOW/DENY
    from_ip: str  # Anywhere, specific IP, etc.
    direction: str  # IN/OUT
    ipv6: bool = False


class FirewallManager:
    """Manages UFW firewall rules via nsenter (for Docker with pid: host)"""
    
    def __init__(self):
        self._use_nsenter = self._check_nsenter_needed()
    
    def _check_nsenter_needed(self) -> bool:
        """Check if we're in a container and need nsenter"""
        # Check if running in Docker
        if os.path.exists('/.dockerenv'):
            return True
        # Check cgroup
        try:
            with open('/proc/1/cgroup', 'r') as f:
                if 'docker' in f.read():
                    return True
        except Exception:
            pass
        return False
    
    def _run_ufw(self, args: list[str], check: bool = True) -> tuple[bool, str, str]:
        """Run ufw command on host and return success, stdout, stderr"""
        if self._use_nsenter:
            # Use nsenter to run command in host's namespace
            # -t 1: target PID 1 (init process on host)
            # -m: mount namespace
            # -u: UTS namespace  
            # -n: network namespace
            # -i: IPC namespace
            cmd = ["nsenter", "-t", "1", "-m", "-u", "-n", "-i", "--", "ufw"] + args
        else:
            cmd = ["ufw"] + args
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            success = result.returncode == 0
            return success, result.stdout.strip(), result.stderr.strip()
        except FileNotFoundError:
            if self._use_nsenter:
                return False, "", "nsenter not found - container must have privileged: true and pid: host"
            return False, "", "ufw not found"
        except subprocess.TimeoutExpired:
            return False, "", "Command timed out"
        except Exception as e:
            return False, "", str(e)
    
    def is_active(self) -> bool:
        """Check if UFW is active"""
        success, stdout, _ = self._run_ufw(["status"])
        return success and "Status: active" in stdout
    
    def add_rule(self, port: int, protocol: str = "tcp") -> tuple[bool, str, Optional[str]]:
        """
        Add firewall rule to allow port (simple version)
        Returns: (success, message, error_log)
        """
        return self.add_advanced_rule(port, protocol, "allow", None, "in")
    
    def add_advanced_rule(
        self,
        port: int,
        protocol: str = "tcp",
        action: str = "allow",
        from_ip: Optional[str] = None,
        direction: str = "in",
        comment: Optional[str] = None,
    ) -> tuple[bool, str, Optional[str]]:
        """
        Add firewall rule with full control

        Args:
            port: Port number (1-65535)
            protocol: tcp, udp, or any
            action: allow or deny
            from_ip: Source IP (None = Anywhere)
            direction: in or out
            comment: optional UFW rule comment

        Returns: (success, message, error_log)
        """
        if not 1 <= port <= 65535:
            return False, "Invalid port number", None

        if protocol not in ("tcp", "udp", "any"):
            return False, "Invalid protocol (use tcp, udp, or any)", None

        action = action.lower()
        if action not in ("allow", "deny"):
            return False, "Invalid action (use allow or deny)", None

        direction = direction.lower()
        if direction not in ("in", "out"):
            return False, "Invalid direction (use in or out)", None

        # Build UFW command
        # Format: ufw [allow|deny] [in|out] [from IP] to any port PORT [proto PROTOCOL] [comment 'TEXT']
        args = [action, direction]

        if from_ip and from_ip.lower() not in ("any", "anywhere", ""):
            if not _is_valid_from_ip(from_ip):
                return False, f"Invalid from_ip (expected IPv4/IPv6 or CIDR): {from_ip}", None
            args.extend(["from", from_ip])

        # UFW requires "to any" before "port"
        args.extend(["to", "any"])

        if protocol == "any":
            args.extend(["port", str(port)])
        else:
            args.extend(["port", str(port), "proto", protocol])

        # UFW поддерживает comment 'TEXT' в конце команды. Передаём списком —
        # subprocess сам экранирует, shell-injection невозможен.
        if comment:
            sanitized = comment.replace("\n", " ").replace("\r", " ").strip()[:200]
            if sanitized:
                args.extend(["comment", sanitized])

        success, stdout, stderr = self._run_ufw(args)
        
        from_desc = from_ip if from_ip else "Anywhere"
        rule_desc = f"{action.upper()} {direction.upper()} port {port}/{protocol} from {from_desc}"
        
        if success:
            logger.info(f"Firewall: added rule - {rule_desc}")
            return True, f"Rule added: {rule_desc}", None
        else:
            error_log = f"Command: ufw {' '.join(args)}\nStdout: {stdout}\nStderr: {stderr}"
            logger.error(f"Failed to add firewall rule: {stderr}")
            return False, f"Failed to add rule: {stderr or stdout}", error_log
    
    def remove_rule(self, port: int, protocol: str = "tcp") -> tuple[bool, str, Optional[str]]:
        """
        Remove firewall rule (simple - removes first matching ALLOW rule)
        Returns: (success, message, error_log)
        """
        if not 1 <= port <= 65535:
            return False, "Invalid port number", None
        
        if protocol not in ("tcp", "udp", "any"):
            return False, "Invalid protocol", None
        
        # Delete rule (non-interactive)
        if protocol == "any":
            args = ["--force", "delete", "allow", str(port)]
        else:
            args = ["--force", "delete", "allow", f"{port}/{protocol}"]
        
        success, stdout, stderr = self._run_ufw(args)
        
        if success:
            logger.info(f"Firewall: closed port {port}/{protocol}")
            return True, f"Port {port}/{protocol} closed successfully", None
        else:
            # Check if rule didn't exist
            if "Could not delete non-existent rule" in stderr or "Could not delete non-existent rule" in stdout:
                return True, f"Port {port}/{protocol} was not open", None
            
            error_log = f"Command: ufw {' '.join(args)}\nStdout: {stdout}\nStderr: {stderr}"
            logger.error(f"Failed to close port {port}/{protocol}: {stderr}")
            return False, f"Failed to close port: {stderr or stdout}", error_log
    
    def remove_rule_by_number(self, rule_number: int) -> tuple[bool, str, Optional[str]]:
        """
        Remove firewall rule by its number
        Returns: (success, message, error_log)
        """
        if rule_number < 1:
            return False, "Invalid rule number", None
        
        args = ["--force", "delete", str(rule_number)]
        success, stdout, stderr = self._run_ufw(args)
        
        if success:
            logger.info(f"Firewall: deleted rule #{rule_number}")
            return True, f"Rule #{rule_number} deleted successfully", None
        else:
            error_log = f"Command: ufw {' '.join(args)}\nStdout: {stdout}\nStderr: {stderr}"
            logger.error(f"Failed to delete rule #{rule_number}: {stderr}")
            return False, f"Failed to delete rule: {stderr or stdout}", error_log
    
    def list_rules(self) -> list[FirewallRule]:
        """Get list of all firewall rules (works even when UFW is inactive)"""
        success, stdout, _ = self._run_ufw(["status", "numbered"])

        if not success:
            return []

        # Если UFW неактивен — ufw status numbered не показывает правила,
        # используем ufw show added как fallback
        if "Status: inactive" in stdout:
            return self._list_rules_from_added()

        rules = []
        # Parse ufw status numbered output
        # Example: [ 1] 80/tcp                     ALLOW IN    Anywhere
        pattern = re.compile(
            r'\[\s*(\d+)\]\s+'  # Rule number
            r'(\d+)(?:/(\w+))?\s+'  # Port/protocol
            r'(ALLOW|DENY)\s+'  # Action
            r'(IN|OUT|FWD)?\s*'  # Direction
            r'(.+?)(?:\s+\(v6\))?$'  # From IP and optional v6
        )

        for line in stdout.split('\n'):
            line = line.strip()
            if not line or line.startswith('Status:') or line.startswith('To'):
                continue

            match = pattern.match(line)
            if match:
                number, port, protocol, action, direction, from_ip = match.groups()
                is_v6 = '(v6)' in line
                # UFW дописывает "# <comment>" после from_ip — отрезаем, в from_ip только адрес.
                from_ip_clean = from_ip.split('#', 1)[0].strip()

                rules.append(FirewallRule(
                    number=int(number),
                    port=int(port),
                    protocol=protocol or "any",
                    action=action,
                    from_ip=from_ip_clean,
                    direction=direction or "IN",
                    ipv6=is_v6
                ))

        return rules

    def _list_rules_from_added(self) -> list[FirewallRule]:
        """Parse rules from 'ufw show added' (works when UFW is inactive)"""
        success, stdout, _ = self._run_ufw(["show", "added"])
        if not success:
            return []

        rules = []
        # Format: ufw allow in from 10.0.0.1 to any port 9100 proto tcp
        # Simpler: ufw allow 22/tcp
        # With route: ufw route allow in on eth0 ... (skip route rules)
        pattern = re.compile(
            r'^ufw\s+'
            r'(allow|deny|reject)\s+'
            r'(?:(in|out)\s+)?'
            r'(?:from\s+(\S+)\s+)?'
            r'(?:to\s+any\s+)?'
            r'(?:port\s+(\d+)\s*)?'
            r'(?:proto\s+(\w+))?'
        )
        # Простой формат: ufw allow 80/tcp или ufw deny 443
        simple_pattern = re.compile(
            r'^ufw\s+(allow|deny|reject)\s+(\d+)(?:/(\w+))?$'
        )

        rule_num = 0
        for line in stdout.split('\n'):
            line = line.strip()
            if not line or line.startswith('Added') or line.startswith('#'):
                continue

            is_v6 = line.endswith('(v6)')

            simple = simple_pattern.match(line)
            if simple:
                action, port, protocol = simple.groups()
                rule_num += 1
                rules.append(FirewallRule(
                    number=rule_num,
                    port=int(port),
                    protocol=protocol or "any",
                    action=action.upper(),
                    from_ip="Anywhere",
                    direction="IN",
                    ipv6=is_v6
                ))
                continue

            match = pattern.match(line)
            if match:
                action, direction, from_ip, port, protocol = match.groups()
                if not port:
                    continue
                rule_num += 1
                rules.append(FirewallRule(
                    number=rule_num,
                    port=int(port),
                    protocol=protocol or "any",
                    action=action.upper(),
                    from_ip=from_ip or "Anywhere",
                    direction=(direction or "in").upper(),
                    ipv6=is_v6
                ))

        return rules
    
    def check_port_open(self, port: int, protocol: str = "tcp") -> bool:
        """Check if specific port is open"""
        rules = self.list_rules()
        
        for rule in rules:
            if rule.port == port and rule.action == "ALLOW":
                if protocol == "any" or rule.protocol == "any" or rule.protocol == protocol:
                    return True
        
        return False
    
    def get_status(self) -> dict:
        """Get firewall status summary"""
        success, stdout, stderr = self._run_ufw(["status", "verbose"])
        
        if not success:
            return {
                "active": False,
                "default_incoming": "unknown",
                "default_outgoing": "unknown",
                "logging": "unknown",
                "error": stderr or "Failed to get status"
            }
        
        # Parse status
        lines = stdout.split('\n')
        status = {
            "active": "Status: active" in stdout,
            "default_incoming": "deny",
            "default_outgoing": "allow",
            "logging": "off"
        }
        
        for line in lines:
            if "Default:" in line:
                if "incoming" in line.lower():
                    status["default_incoming"] = "deny" if "deny" in line.lower() else "allow"
                if "outgoing" in line.lower():
                    status["default_outgoing"] = "allow" if "allow" in line.lower() else "deny"
            if "Logging:" in line:
                status["logging"] = line.split(":")[-1].strip().lower()
        
        return status
    
    def enable(self) -> tuple[bool, str, Optional[str]]:
        """Enable UFW firewall"""
        # Use --force to skip confirmation prompt
        success, stdout, stderr = self._run_ufw(["--force", "enable"])
        
        if success:
            logger.info("UFW firewall enabled")
            return True, "Firewall enabled successfully", None
        else:
            error_log = f"Command: ufw --force enable\nStdout: {stdout}\nStderr: {stderr}"
            logger.error(f"Failed to enable UFW: {stderr}")
            return False, f"Failed to enable firewall: {stderr or stdout}", error_log
    
    def disable(self) -> tuple[bool, str, Optional[str]]:
        """Disable UFW firewall"""
        success, stdout, stderr = self._run_ufw(["disable"])
        
        if success:
            logger.info("UFW firewall disabled")
            return True, "Firewall disabled successfully", None
        else:
            error_log = f"Command: ufw disable\nStdout: {stdout}\nStderr: {stderr}"
            logger.error(f"Failed to disable UFW: {stderr}")
            return False, f"Failed to disable firewall: {stderr or stdout}", error_log
    
    def reset(self) -> tuple[bool, str, Optional[str]]:
        """Reset UFW to default settings (disable and remove all rules)"""
        success, stdout, stderr = self._run_ufw(["--force", "reset"])
        
        if success:
            logger.info("UFW firewall reset to defaults")
            return True, "Firewall reset to defaults", None
        else:
            error_log = f"Command: ufw --force reset\nStdout: {stdout}\nStderr: {stderr}"
            return False, f"Failed to reset firewall: {stderr or stdout}", error_log

    # ==================== Profile application ====================

    @staticmethod
    def _normalize_rule(rule: dict) -> dict:
        """Каноничный вид правила для сравнения и хэширования.

        Comment в хэш не входит — UFW не сохраняет комментарии через
        `ufw allow ...`, поэтому после apply значение всегда будет пустым,
        и сравнение с панелью даст ложный drift.
        """
        return {
            "port": int(rule.get("port", 0)),
            "protocol": (rule.get("protocol") or "tcp").lower(),
            "action": (rule.get("action") or "allow").lower(),
            "from_ip": rule.get("from_ip") or None,
            "direction": (rule.get("direction") or "in").lower(),
        }

    def compute_rules_hash(
        self,
        rules: list[dict],
        default_in: str,
        default_out: str,
    ) -> str:
        canonical = {
            "rules": sorted(
                (self._normalize_rule(r) for r in rules),
                key=lambda r: (r["direction"], r["action"], r["port"], r["protocol"], r["from_ip"] or ""),
            ),
            "default_incoming": (default_in or "deny").lower(),
            "default_outgoing": (default_out or "allow").lower(),
        }
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def _ensure_backup_dir(self) -> bool:
        if self._use_nsenter:
            cmd = ["nsenter", "-t", "1", "-m", "-u", "-n", "-i", "--", "mkdir", "-p", BACKUP_DIR]
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
                return True
            except Exception as e:
                logger.warning(f"Could not create backup dir: {e}")
                return False
        try:
            Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
            return True
        except Exception as e:
            logger.warning(f"Could not create backup dir: {e}")
            return False

    def _write_host_file(self, path: str, content: str) -> bool:
        if self._use_nsenter:
            cmd = ["nsenter", "-t", "1", "-m", "-u", "-n", "-i", "--", "tee", path]
            try:
                subprocess.run(
                    cmd, input=content, capture_output=True, text=True, timeout=10, check=False,
                )
                return True
            except Exception as e:
                logger.error(f"Failed to write {path}: {e}")
                return False
        try:
            Path(path).write_text(content)
            return True
        except Exception as e:
            logger.error(f"Failed to write {path}: {e}")
            return False

    def _read_host_file(self, path: str) -> Optional[str]:
        if self._use_nsenter:
            cmd = ["nsenter", "-t", "1", "-m", "-u", "-n", "-i", "--", "cat", path]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if result.returncode != 0:
                    return None
                return result.stdout
            except Exception:
                return None
        try:
            return Path(path).read_text()
        except Exception:
            return None

    def _backup_state(self) -> Optional[str]:
        """Снимок текущего UFW (verbose + правила + статус) в JSON-файл."""
        if not self._ensure_backup_dir():
            return None

        ok_v, status_verbose, _ = self._run_ufw(["status", "verbose"])
        rules = self.list_rules()

        timestamp = int(time.time())
        backup_path = f"{BACKUP_DIR}/ufw_backup_{timestamp}.json"

        snapshot = {
            "timestamp": timestamp,
            "active": "Status: active" in status_verbose if ok_v else False,
            "status_verbose": status_verbose,
            "rules": [
                {
                    "port": r.port,
                    "protocol": r.protocol,
                    "action": r.action.lower(),
                    "from_ip": None if r.from_ip in ("Anywhere", "") else r.from_ip,
                    "direction": r.direction.lower(),
                }
                for r in rules
                if not r.ipv6  # IPv6-копии UFW создаст сам при apply
            ],
        }

        if not self._write_host_file(backup_path, json.dumps(snapshot, indent=2)):
            return None

        self._prune_old_backups()
        logger.info(f"Firewall state backed up to {backup_path}")
        return backup_path

    def _prune_old_backups(self) -> None:
        if self._use_nsenter:
            list_cmd = [
                "nsenter", "-t", "1", "-m", "-u", "-n", "-i", "--",
                "sh", "-c",
                f"ls -1t {BACKUP_DIR}/ufw_backup_*.json 2>/dev/null | tail -n +{MAX_BACKUPS + 1}",
            ]
            try:
                result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=10)
                old_files = [line for line in result.stdout.strip().split("\n") if line]
                for path in old_files:
                    subprocess.run(
                        ["nsenter", "-t", "1", "-m", "-u", "-n", "-i", "--", "rm", "-f", path],
                        capture_output=True, timeout=5, check=False,
                    )
            except Exception:
                pass
            return
        try:
            backups = sorted(
                Path(BACKUP_DIR).glob("ufw_backup_*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for path in backups[MAX_BACKUPS:]:
                try:
                    path.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    def _read_backup(self, path: str) -> Optional[dict]:
        raw = self._read_host_file(path)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    def _apply_default_policies(self, default_in: str, default_out: str) -> tuple[bool, str]:
        for policy, direction in (
            ((default_in or "deny").lower(), "incoming"),
            ((default_out or "allow").lower(), "outgoing"),
        ):
            if policy not in ("allow", "deny", "reject"):
                return False, f"Invalid default {direction} policy: {policy}"
            ok, stdout, stderr = self._run_ufw(["default", policy, direction])
            if not ok:
                return False, f"Failed default {policy} {direction}: {stderr or stdout}"
        return True, ""

    def _apply_rules_list(self, rules: list[dict]) -> tuple[bool, str]:
        for index, raw in enumerate(rules):
            normalized = self._normalize_rule(raw)
            comment = (raw.get("comment") or "").strip() or None
            ok, msg, _ = self.add_advanced_rule(
                port=normalized["port"],
                protocol=normalized["protocol"],
                action=normalized["action"],
                from_ip=normalized["from_ip"],
                direction=normalized["direction"],
                comment=comment,
            )
            if not ok:
                return False, f"Rule #{index + 1} failed: {msg}"
        return True, ""

    def _parse_default_policies_from_verbose(self, verbose: str) -> tuple[str, str]:
        default_in, default_out = "deny", "allow"
        for line in verbose.split("\n"):
            low = line.lower()
            if "default:" not in low:
                continue
            if "(incoming)" in low:
                for p in ("allow", "deny", "reject"):
                    if f"{p} (incoming)" in low:
                        default_in = p
                        break
            if "(outgoing)" in low:
                for p in ("allow", "deny", "reject"):
                    if f"{p} (outgoing)" in low:
                        default_out = p
                        break
        return default_in, default_out

    def _restore_state(self, backup_path: str) -> tuple[bool, str]:
        snapshot = self._read_backup(backup_path)
        if not snapshot:
            return False, f"Cannot read backup {backup_path}"

        ok, stdout, stderr = self._run_ufw(["--force", "reset"])
        if not ok:
            return False, f"Reset during rollback failed: {stderr or stdout}"

        default_in, default_out = self._parse_default_policies_from_verbose(
            snapshot.get("status_verbose", "")
        )
        ok, err = self._apply_default_policies(default_in, default_out)
        if not ok:
            return False, err

        ok, err = self._apply_rules_list(snapshot.get("rules", []))
        if not ok:
            return False, err

        if snapshot.get("active"):
            ok, stdout, stderr = self._run_ufw(["--force", "enable"])
            if not ok:
                return False, f"Enable during rollback failed: {stderr or stdout}"

        return True, "Rolled back to previous state"

    def get_full_state(self) -> dict:
        """Текущее состояние UFW + canonical hash (для drift-детекции)."""
        status = self.get_status()
        raw = self.list_rules()
        rules = [
            {
                "port": r.port,
                "protocol": r.protocol,
                "action": r.action.lower(),
                "from_ip": None if r.from_ip in ("Anywhere", "") else r.from_ip,
                "direction": r.direction.lower(),
                "comment": "",
            }
            for r in raw
            if not r.ipv6
        ]
        rules_hash = self.compute_rules_hash(
            rules,
            status.get("default_incoming", "deny"),
            status.get("default_outgoing", "allow"),
        )
        return {
            "active": status.get("active", False),
            "default_incoming": status.get("default_incoming", "deny"),
            "default_outgoing": status.get("default_outgoing", "allow"),
            "rules": rules,
            "rules_hash": rules_hash,
        }

    @staticmethod
    def _has_node_port_allow(rules: list[dict], default_in: str) -> bool:
        """Без правила allow 9100/tcp IN панель потеряет связь с нодой."""
        if (default_in or "deny").lower() == "allow":
            return True
        for raw in rules:
            r = FirewallManager._normalize_rule(raw)
            if (
                r["port"] == NODE_API_PORT
                and r["protocol"] in ("tcp", "any")
                and r["action"] == "allow"
                and r["direction"] == "in"
            ):
                return True
        return False

    def apply_profile(
        self,
        rules: list[dict],
        default_incoming: str = "deny",
        default_outgoing: str = "allow",
        force: bool = False,
    ) -> dict:
        """Атомарно заменить состояние UFW правилами профиля.

        Стратегия: backup → reset → defaults → rules → enable. При ошибке —
        rollback из бэкапа. Без правила 9100/tcp IN apply отказывает, если
        не передан force=True.
        """
        if not force and not self._has_node_port_allow(rules, default_incoming):
            return {
                "success": False,
                "message": (
                    f"Allow rule for node API port {NODE_API_PORT}/tcp missing — "
                    "panel will lose connection to node. Use force=true to apply anyway"
                ),
                "rules_hash": None,
                "rolled_back": False,
                "error_log": None,
            }

        backup_path = self._backup_state()

        ok, stdout, stderr = self._run_ufw(["--force", "reset"])
        if not ok:
            return self._rollback_with_result(backup_path, f"Reset failed: {stderr or stdout}")

        ok, err = self._apply_default_policies(default_incoming, default_outgoing)
        if not ok:
            return self._rollback_with_result(backup_path, err)

        ok, err = self._apply_rules_list(rules)
        if not ok:
            return self._rollback_with_result(backup_path, err)

        ok, stdout, stderr = self._run_ufw(["--force", "enable"])
        if not ok:
            return self._rollback_with_result(backup_path, f"Enable failed: {stderr or stdout}")

        new_hash = self.compute_rules_hash(rules, default_incoming, default_outgoing)
        logger.info(f"Firewall profile applied: {len(rules)} rules, hash={new_hash[:12]}")

        return {
            "success": True,
            "message": f"Applied {len(rules)} rules",
            "rules_hash": new_hash,
            "rolled_back": False,
            "error_log": None,
        }

    def _rollback_with_result(self, backup_path: Optional[str], error: str) -> dict:
        logger.error(f"Apply failed, rolling back: {error}")
        if not backup_path:
            return {
                "success": False,
                "message": f"Apply failed (no backup to restore): {error}",
                "rules_hash": None,
                "rolled_back": False,
                "error_log": error,
            }

        rb_ok, rb_msg = self._restore_state(backup_path)
        if not rb_ok:
            critical = f"Apply failed AND rollback failed: apply={error}; rollback={rb_msg}"
            logger.critical(critical)
            return {
                "success": False,
                "message": critical,
                "rules_hash": None,
                "rolled_back": False,
                "error_log": critical,
            }

        return {
            "success": False,
            "message": f"Apply failed, rolled back: {error}",
            "rules_hash": None,
            "rolled_back": True,
            "error_log": error,
        }


# Singleton instance
_manager: Optional[FirewallManager] = None


def get_firewall_manager() -> FirewallManager:
    """Get or create FirewallManager instance"""
    global _manager
    if _manager is None:
        _manager = FirewallManager()
    return _manager
