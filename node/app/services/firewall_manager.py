"""UFW Firewall manager for port management

Works from Docker container by using nsenter to execute commands on host.
Requires container to run with: privileged: true, pid: host
"""

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


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
        direction: str = "in"
    ) -> tuple[bool, str, Optional[str]]:
        """
        Add firewall rule with full control
        
        Args:
            port: Port number (1-65535)
            protocol: tcp, udp, or any
            action: allow or deny
            from_ip: Source IP (None = Anywhere)
            direction: in or out
        
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
        # Format: ufw [allow|deny] [in|out] [from IP] to any port PORT [proto PROTOCOL]
        args = [action, direction]
        
        if from_ip and from_ip.lower() not in ("any", "anywhere", ""):
            args.extend(["from", from_ip])
        
        # UFW requires "to any" before "port"
        args.extend(["to", "any"])
        
        if protocol == "any":
            args.extend(["port", str(port)])
        else:
            args.extend(["port", str(port), "proto", protocol])
        
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
        """Get list of all firewall rules"""
        success, stdout, _ = self._run_ufw(["status", "numbered"])
        
        if not success:
            return []
        
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
                
                rules.append(FirewallRule(
                    number=int(number),
                    port=int(port),
                    protocol=protocol or "any",
                    action=action,
                    from_ip=from_ip.strip(),
                    direction=direction or "IN",
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


# Singleton instance
_manager: Optional[FirewallManager] = None


def get_firewall_manager() -> FirewallManager:
    """Get or create FirewallManager instance"""
    global _manager
    if _manager is None:
        _manager = FirewallManager()
    return _manager
