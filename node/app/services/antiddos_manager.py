"""Anti-DDoS manager — drives the host-side ddos-watchdog.sh script.

The rule logic lives entirely in /opt/monitoring/scripts/ddos-watchdog.sh so the
emergency ruleset is identical whether the watchdog toggles it automatically or
the panel toggles it manually. This manager just invokes the script's CLI verbs
on the host via the shared host executor (nsenter), plus validates whitelist IPs
before handing them to the script.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from app.services.host_executor import ExecuteResult, HostExecutor, get_host_executor
from app.services.host_files import write_host_file

logger = logging.getLogger(__name__)

WATCHDOG_SCRIPT = "/opt/monitoring/scripts/ddos-watchdog.sh"
WATCHDOG_SERVICE = "ddos-watchdog.service"
WATCHDOG_SERVICE_PATH = "/etc/systemd/system/ddos-watchdog.service"
INSTALLED_FILES = (WATCHDOG_SCRIPT, WATCHDOG_SERVICE_PATH)
# Суффиксы, а не свои каталоги: rename атомарен только внутри одной ФС, а
# systemd файлы с чужим расширением в каталоге юнитов не читает
CANDIDATE_SUFFIX = ".new"
PREVIOUS_SUFFIX = ".prev"
CANDIDATE_FILES = " ".join(path + CANDIDATE_SUFFIX for path in INSTALLED_FILES)
START_GRACE_SECONDS = 2
FAILURE_TEXT_LIMIT = 300

_IP_CIDR_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?$")


def _failure_text(result: ExecuteResult) -> str:
    return (result.stderr or result.error or f"exit code {result.exit_code}")[:FAILURE_TEXT_LIMIT]


def _keep_previous_command() -> str:
    """Рабочие файлы копируются в `.prev`. Файла не было — не остаётся и `.prev`
    от прошлых установок: по нему откат отличает обновление от первой установки."""
    return "; ".join(
        f"if [ -e {path} ]; then cp -pf {path} {path}{PREVIOUS_SUFFIX} || "
        f"{{ rm -f {path}{PREVIOUS_SUFFIX}; exit 1; }}; else rm -f {path}{PREVIOUS_SUFFIX}; fi"
        for path in INSTALLED_FILES
    )


def _rollback_command() -> str:
    """Вернуть то, что стояло до установки.

    Обновление — прежние файлы и рестарт сервиса на них. Первая установка —
    сторожа на ноде снова нет: сервис, падающий в цикле, хуже отсутствующего,
    а панель увидит «не установлен» и повторит попытку."""
    restore = "; ".join(f"restore {path}" for path in INSTALLED_FILES)
    return (
        f'restore() {{ if [ -e "$1{PREVIOUS_SUFFIX}" ]; then mv -fT "$1{PREVIOUS_SUFFIX}" "$1"; '
        f'else rm -f "$1"; fi; }}\n'
        f"rm -f {CANDIDATE_FILES}\n"
        f"if [ -e {WATCHDOG_SCRIPT}{PREVIOUS_SUFFIX} ]; then\n"
        f"    {restore}; systemctl daemon-reload; systemctl restart {WATCHDOG_SERVICE}\n"
        f"else\n"
        f"    systemctl disable --now {WATCHDOG_SERVICE}; {restore}; systemctl daemon-reload\n"
        f"fi"
    )


def _parse_state(stdout: str) -> Optional[dict]:
    """Состояние из последней строки вывода `status`; None — там не JSON-объект."""
    lines = stdout.strip().splitlines()
    if not lines:
        return None
    try:
        state = json.loads(lines[-1])
    except ValueError:
        return None
    return state if isinstance(state, dict) else None


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
    version: str       # installed watchdog script version ("" if unknown)


class AntiDdosManager:
    """Thin async wrapper over ddos-watchdog.sh (runs on host via nsenter)."""

    def __init__(self, executor: HostExecutor):
        self._executor = executor
        self._install_lock = asyncio.Lock()

    async def _run(self, verb: str, timeout: int = 20) -> tuple[bool, str, str]:
        result = await self._executor.execute(
            f"{WATCHDOG_SCRIPT} {verb}", timeout=timeout, shell="bash"
        )
        return result.success, result.stdout, result.stderr

    async def _script_installed(self) -> bool:
        result = await self._executor.execute(f"test -x {WATCHDOG_SCRIPT}", timeout=5)
        return result.exit_code == 0

    async def install(self, script_content: str, service_content: str,
                      enable_watchdog: bool = True) -> tuple[bool, str]:
        """Install ddos-watchdog.sh + service on the host and start it.

        Native systemd unit (like the tune services), so it survives reboots and
        runs independently of Docker/panel. Watchdog auto-detection is on by
        default; the emergency ruleset stays dormant until a signal fires.

        Панель ставит сторож сама при расхождении версий, поэтому негодный скрипт
        из ветки доехал бы до всего парка. Новая версия сначала проверяется рядом
        с рабочей, встаёт на место одним rename, а если после этого сервис не
        поднялся — возвращается прежняя, и панель получает причину отказа.
        """
        # Две установки разом затёрли бы друг другу и кандидатов, и `.prev`
        async with self._install_lock:
            problem = await self._stage_candidates(script_content, service_content)
            if problem is None:
                problem = await self._keep_previous()
            if problem:
                await self._executor.execute(f"rm -f {CANDIDATE_FILES}", timeout=5)
                return False, problem

            problem = await self._place_candidates() or await self._start_and_verify(enable_watchdog)
            if problem is None:
                return True, "watchdog installed"

            logger.error(f"Watchdog install failed, rolling back: {problem}")
            rollback = await self._executor.execute(_rollback_command(), timeout=40, shell="bash")
            if not rollback.success:
                logger.error(f"Watchdog rollback failed: {_failure_text(rollback)}")
                return False, f"{problem}; rollback failed: {_failure_text(rollback)}"
            return False, f"{problem}; rolled back"

    async def _stage_candidates(self, script_content: str, service_content: str) -> Optional[str]:
        """Новые скрипт и юнит ложатся рядом с рабочими и проверяются, не трогая их."""
        # Замаскированный юнит (симлинк на /dev/null) — решение админа на самом
        # хосте: rename снял бы маску и поднял сторож против его воли
        linked = await self._executor.execute(
            " || ".join(f"test -L {path}" for path in INSTALLED_FILES), timeout=5
        )
        if linked.exit_code == 0:
            return "watchdog script or unit is a symlink (masked unit?), left untouched"

        candidate = WATCHDOG_SCRIPT + CANDIDATE_SUFFIX
        if not await write_host_file(candidate, script_content, mode="755"):
            return "failed to write watchdog script"

        syntax = await self._executor.execute(f"bash -n {candidate}", timeout=15, shell="bash")
        if not syntax.success:
            return f"watchdog script failed the syntax check: {_failure_text(syntax)}"

        version = await self._executor.execute(f"{candidate} version", timeout=15, shell="bash")
        if not version.success or not version.stdout.strip():
            return f"watchdog script did not report its version: {_failure_text(version)}"

        if not await write_host_file(WATCHDOG_SERVICE_PATH + CANDIDATE_SUFFIX, service_content, mode="644"):
            return "failed to write watchdog service"
        return None

    async def _keep_previous(self) -> Optional[str]:
        result = await self._executor.execute(_keep_previous_command(), timeout=15, shell="bash")
        return None if result.success else f"failed to keep the previous watchdog: {_failure_text(result)}"

    async def _place_candidates(self) -> Optional[str]:
        result = await self._executor.execute(
            " && ".join(f"mv -fT {path}{CANDIDATE_SUFFIX} {path}" for path in INSTALLED_FILES),
            timeout=15, shell="bash",
        )
        return None if result.success else f"failed to replace the watchdog: {_failure_text(result)}"

    async def _start_and_verify(self, enable_watchdog: bool) -> Optional[str]:
        """Причина, по которой новая версия не заработала, либо None."""
        for command, timeout in (
            ("systemctl daemon-reload", 10),
            (f"systemctl enable {WATCHDOG_SERVICE}", 10),
            (f"systemctl restart {WATCHDOG_SERVICE}", 15),
        ):
            result = await self._executor.execute(command, timeout=timeout)
            if not result.success:
                return f"{command} failed: {_failure_text(result)}"

        ok, _, stderr = await self._run("watchdog-on" if enable_watchdog else "watchdog-off")
        if not ok:
            return f"watchdog script could not save its state: {stderr[:FAILURE_TEXT_LIMIT]}"

        # Type=simple: restart отвечает успехом сразу после fork, и скрипт,
        # упавший на первых строках цикла, виден только спустя паузу
        await asyncio.sleep(START_GRACE_SECONDS)
        active = await self._executor.execute(f"systemctl is-active {WATCHDOG_SERVICE}", timeout=5)
        if active.stdout.strip() != "active":
            return f"watchdog service is {active.stdout.strip() or 'not running'} after restart"

        ok, stdout, stderr = await self._run("status", timeout=15)
        if not ok or _parse_state(stdout) is None:
            return f"watchdog status check failed: {(stderr or stdout)[:FAILURE_TEXT_LIMIT]}"
        return None

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
        # Список идёт через stdin, а не в команде: она уходит в `bash -c` одним
        # аргументом, а аргумент execve ограничен 128 КБ — список на 8–9 тысяч
        # адресов в него не помещается, и запуск падает с E2BIG
        result = await self._executor.execute(
            f"{WATCHDOG_SCRIPT} whitelist-sync", timeout=60, shell="bash",
            stdin_data="\n".join(valid),
        )
        if not result.success:
            return False, result.stderr or result.error or "sync failed", 0
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
                version="",
            )

        ok, stdout, _ = await self._run("status", timeout=15)
        state = (_parse_state(stdout) if ok else None) or {}

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
            version=state.get("version", ""),
        )


_manager: AntiDdosManager | None = None


def get_antiddos_manager() -> AntiDdosManager:
    global _manager
    if _manager is None:
        _manager = AntiDdosManager(get_host_executor())
    return _manager
