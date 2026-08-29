"""Часовой пояс и NTP-синхронизация хоста — обёртка над host_time_sync.sh."""

import shlex
from dataclasses import dataclass
from pathlib import Path

from app.services.host_executor import ExecuteResult

HOST_TIME_SYNC_SCRIPT = Path(__file__).with_name("host_time_sync.sh").read_text(encoding="utf-8")

# Худший случай — установка chrony с обновлением списков apt плюс ожидание
# первой синхронизации (SYNC_WAIT_SECONDS в скрипте)
TIME_SYNC_TIMEOUT = 240


@dataclass
class TimeSyncReport:
    success: bool
    timezone: str
    ntp_service: str
    ntp_installed: bool
    ntp_managed_by_host: bool
    ntp_enabled: bool
    ntp_synchronized: bool
    current_time: str
    errors: list[str]


def build_time_sync_command(timezone_name: str) -> str:
    return f"TZ_NAME={shlex.quote(timezone_name)}\n{HOST_TIME_SYNC_SCRIPT}"


def parse_key_values(stdout: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def report_from_result(result: ExecuteResult, requested_timezone: str) -> TimeSyncReport:
    values = parse_key_values(result.stdout)
    errors: list[str] = []
    if not result.success:
        errors = [line for line in result.stderr.splitlines() if line.strip()]
        if not errors:
            errors.append(result.error or f"exit code {result.exit_code}")
    return TimeSyncReport(
        success=result.success,
        timezone=values.get("Timezone", requested_timezone),
        ntp_service=values.get("NTPService", ""),
        ntp_installed=values.get("NTPInstalled") == "yes",
        ntp_managed_by_host=values.get("NTPManagedByHost") == "yes",
        ntp_enabled=values.get("NTP") == "yes",
        ntp_synchronized=values.get("NTPSynchronized") == "yes",
        current_time=values.get("TimeUSec", ""),
        errors=errors,
    )
