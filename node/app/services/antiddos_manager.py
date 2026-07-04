"""Anti-DDoS manager — drives the host-side ddos-watchdog.sh script.

The rule logic lives entirely in /opt/monitoring/scripts/ddos-watchdog.sh so the
emergency ruleset is identical whether the watchdog toggles it automatically or
the panel toggles it manually. This manager just invokes the script's CLI verbs
on the host via the shared host executor (nsenter), plus validates whitelist IPs
before handing them to the script.
"""

import json
import logging
import re
from dataclasses import dataclass

from app.services.host_executor import get_host_executor

logger = logging.getLogger(__name__)

WATCHDOG_SCRIPT = "/opt/monitoring/scripts/ddos-watchdog.sh"
WATCHDOG_SERVICE = "ddos-watchdog.service"
WATCHDOG_SERVICE_PATH = "/etc/systemd/system/ddos-watchdog.service"

_IP_CIDR_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?$")


def _valid_ip_cidr(value: str) -> bool:
    value = value.strip()
    if not _IP_CIDR_RE.match(value):
        return False
    host, _, prefix = value.partition("/")
    if any(int(octet) > 255 for octet in host.split(".")):
        return False
    if prefix and not (0 <= int(prefix) <= 32):
        return False
    return True


@dataclass
class AntiDdosStatus:
    installed: bool
    mode: str          # on | off
    source: str        # auto | manual | none
    since: int
    reason: str
    watchdog: str      # on | off
    watchdog_active: bool
    client_ports: list[int]


class AntiDdosManager:
    """Thin async wrapper over ddos-watchdog.sh (runs on host via nsenter)."""

    def __init__(self):
        self._executor = get_host_executor()

    async def _run(self, verb: str, timeout: int = 20) -> tuple[bool, str, str]:
        result = await self._executor.execute(
            f"{WATCHDOG_SCRIPT} {verb}", timeout=timeout, shell="bash"
        )
        return result.success, result.stdout, result.stderr

    async def _script_installed(self) -> bool:
        result = await self._executor.execute(f"test -x {WATCHDOG_SCRIPT}", timeout=5)
        return result.exit_code == 0

    async def _write_host_file(self, path: str, content: str) -> bool:
        result = await self._executor.execute(
            f"mkdir -p $(dirname {path}) && cat > {path} << 'EOFADDOS'\n{content}\nEOFADDOS",
            timeout=10, shell="bash",
        )
        return result.success and result.exit_code == 0

    async def install(self, script_content: str, service_content: str,
                      enable_watchdog: bool = True) -> tuple[bool, str]:
        """Install ddos-watchdog.sh + service on the host and start it.

        Native systemd unit (like the tune services), so it survives reboots and
        runs independently of Docker/panel. Watchdog auto-detection is on by
        default; the emergency ruleset stays dormant until a signal fires.
        """
        if not await self._write_host_file(WATCHDOG_SCRIPT, script_content):
            return False, "failed to write watchdog script"
        await self._executor.execute(f"chmod +x {WATCHDOG_SCRIPT}", timeout=5)
        if not await self._write_host_file(WATCHDOG_SERVICE_PATH, service_content):
            return False, "failed to write watchdog service"

        await self._executor.execute("systemctl daemon-reload", timeout=10)
        await self._executor.execute(f"systemctl enable {WATCHDOG_SERVICE}", timeout=10)
        restart = await self._executor.execute(
            f"systemctl restart {WATCHDOG_SERVICE}", timeout=15
        )
        await self._run("watchdog-on" if enable_watchdog else "watchdog-off")
        if not restart.success:
            return False, f"watchdog service failed to start: {restart.stderr}"
        return True, "watchdog installed"

    async def enable_emergency(self, source: str = "manual") -> tuple[bool, str]:
        # source is fixed to "manual" from the API — the loop owns "auto"
        if not await self._script_installed():
            return False, "watchdog not installed"
        ok, _, stderr = await self._run("enable-manual", timeout=40)
        return ok, "emergency enabled" if ok else f"failed: {stderr}"

    async def disable_emergency(self) -> tuple[bool, str]:
        if not await self._script_installed():
            return False, "watchdog not installed"
        ok, _, stderr = await self._run("disable-manual", timeout=40)
        return ok, "emergency disabled" if ok else f"failed: {stderr}"

    async def set_watchdog(self, enabled: bool) -> tuple[bool, str]:
        if not await self._script_installed():
            return False, "watchdog not installed"
        ok, _, stderr = await self._run("watchdog-on" if enabled else "watchdog-off")
        return ok, "ok" if ok else f"failed: {stderr}"

    async def sync_whitelist(self, ips: list[str]) -> tuple[bool, str, int]:
        """Replace the antiddos_allow ipset from a validated IP/CIDR list."""
        if not await self._script_installed():
            return False, "watchdog not installed", 0

        valid = sorted({ip.strip() for ip in ips if _valid_ip_cidr(ip)})
        # space-separated (json.dumps escapes newlines, the script splits on space)
        payload = " ".join(valid)
        cmd = f"printf '%s' {json.dumps(payload)} | {WATCHDOG_SCRIPT} whitelist-sync"
        result = await self._executor.execute(cmd, timeout=60, shell="bash")
        if not result.success:
            return False, result.stderr or "sync failed", 0
        count = 0
        try:
            count = int(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            count = len(valid)
        return True, "whitelist synced", count

    async def get_client_ports(self) -> list[int]:
        if not await self._script_installed():
            return []
        ok, stdout, _ = await self._run("detect-ports", timeout=15)
        if not ok or not stdout.strip():
            return []
        return [int(p) for p in stdout.strip().split(",") if p.strip().isdigit()]

    async def get_status(self) -> AntiDdosStatus:
        installed = await self._script_installed()
        if not installed:
            return AntiDdosStatus(
                installed=False, mode="off", source="none", since=0,
                reason="", watchdog="off", watchdog_active=False, client_ports=[],
            )

        ok, stdout, _ = await self._run("status", timeout=15)
        state = {}
        if ok and stdout.strip():
            try:
                state = json.loads(stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                state = {}

        svc = await self._executor.execute(
            f"systemctl is-active {WATCHDOG_SERVICE} 2>/dev/null", timeout=5
        )
        watchdog_active = svc.stdout.strip() == "active"

        return AntiDdosStatus(
            installed=True,
            mode=state.get("mode", "off"),
            source=state.get("source", "none"),
            since=int(state.get("since", 0) or 0),
            reason=state.get("reason", ""),
            watchdog=state.get("watchdog", "on"),
            watchdog_active=watchdog_active,
            client_ports=await self.get_client_ports(),
        )


_manager: AntiDdosManager | None = None


def get_antiddos_manager() -> AntiDdosManager:
    global _manager
    if _manager is None:
        _manager = AntiDdosManager()
    return _manager
