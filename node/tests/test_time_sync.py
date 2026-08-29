"""Tests for host time sync command building and result parsing.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Скрипт исполняется на хосте от root, поэтому важно, что имя пояса уходит в него
квотированным, а разбор вывода не зависит от того, на каком шаге скрипт сдался:
ключи NTPService/NTPInstalled/NTPManagedByHost печатаются всегда, ошибки берутся
из stderr только при ненулевом коде выхода.
"""

import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.host_executor import ExecuteResult  # noqa: E402
from app.services.time_sync import (  # noqa: E402
    HOST_TIME_SYNC_SCRIPT,
    build_time_sync_command,
    parse_key_values,
    report_from_result,
)

TIMEDATECTL_SHOW = """Timezone=Europe/Moscow
LocalRTC=no
CanNTP=yes
NTP=yes
NTPSynchronized=yes
TimeUSec=Wed 2026-08-26 23:42:56 MSK
RTCTimeUSec=Wed 2026-08-26 23:42:57 MSK"""


def execute_result(exit_code: int, stdout: str = "", stderr: str = "", error: str | None = None) -> ExecuteResult:
    return ExecuteResult(
        success=exit_code == 0,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        execution_time_ms=1,
        error=error,
    )


class BuildCommandTests(unittest.TestCase):
    def test_timezone_is_quoted_and_precedes_script(self):
        command = build_time_sync_command("America/Argentina/Buenos_Aires")
        first_line, rest = command.split("\n", 1)
        self.assertEqual(first_line, "TZ_NAME=America/Argentina/Buenos_Aires")
        self.assertEqual(rest, HOST_TIME_SYNC_SCRIPT)

    def test_shell_metacharacters_are_neutralised(self):
        command = build_time_sync_command("Europe/Moscow; rm -rf /")
        self.assertTrue(command.startswith("TZ_NAME='Europe/Moscow; rm -rf /'\n"))

    @unittest.skipUnless(shutil.which("bash"), "bash not available")
    def test_script_has_valid_bash_syntax(self):
        check = subprocess.run(
            ["bash", "-n"], input=build_time_sync_command("UTC"),
            capture_output=True, text=True,
        )
        self.assertEqual(check.returncode, 0, check.stderr)


class ParseTests(unittest.TestCase):
    def test_key_values_ignore_lines_without_separator(self):
        values = parse_key_values("NTPService=chrony\nnoise line\n Timezone = UTC ")
        self.assertEqual(values, {"NTPService": "chrony", "Timezone": "UTC"})


class ReportTests(unittest.TestCase):
    def test_success_with_existing_daemon(self):
        stdout = f"NTPService=chrony\nNTPInstalled=no\nNTPManagedByHost=no\n{TIMEDATECTL_SHOW}"
        report = report_from_result(execute_result(0, stdout=stdout), "Europe/Moscow")
        self.assertTrue(report.success)
        self.assertEqual(report.ntp_service, "chrony")
        self.assertFalse(report.ntp_installed)
        self.assertFalse(report.ntp_managed_by_host)
        self.assertTrue(report.ntp_enabled)
        self.assertTrue(report.ntp_synchronized)
        self.assertEqual(report.timezone, "Europe/Moscow")
        self.assertEqual(report.current_time, "Wed 2026-08-26 23:42:56 MSK")
        self.assertEqual(report.errors, [])

    def test_installed_daemon_not_yet_synchronized(self):
        stdout = "NTPService=chrony\nNTPInstalled=yes\nNTPManagedByHost=no\nNTP=yes\nNTPSynchronized=no"
        report = report_from_result(execute_result(0, stdout=stdout), "UTC")
        self.assertTrue(report.success)
        self.assertTrue(report.ntp_installed)
        self.assertFalse(report.ntp_synchronized)
        self.assertEqual(report.timezone, "UTC")

    def test_container_leaves_ntp_to_host(self):
        stdout = "NTPService=\nNTPInstalled=no\nNTPManagedByHost=yes\nNTP=no\nNTPSynchronized=yes"
        report = report_from_result(execute_result(0, stdout=stdout), "UTC")
        self.assertTrue(report.success)
        self.assertTrue(report.ntp_managed_by_host)
        self.assertEqual(report.ntp_service, "")
        self.assertTrue(report.ntp_synchronized)

    def test_failure_collects_stderr_lines(self):
        stdout = "NTPService=\nNTPInstalled=no\nNTPManagedByHost=no\nNTP=no\nNTPSynchronized=no"
        stderr = "apt-get install chrony failed: E: Unable to locate package chrony\nno NTP daemon available on host\n"
        report = report_from_result(execute_result(1, stdout=stdout, stderr=stderr), "UTC")
        self.assertFalse(report.success)
        self.assertEqual(report.errors, [
            "apt-get install chrony failed: E: Unable to locate package chrony",
            "no NTP daemon available on host",
        ])

    def test_success_ignores_stderr_noise(self):
        report = report_from_result(execute_result(0, stdout="NTP=yes", stderr="warning: something"), "UTC")
        self.assertTrue(report.success)
        self.assertEqual(report.errors, [])

    def test_timeout_reports_executor_error(self):
        report = report_from_result(execute_result(-1, error="Command timed out after 240 seconds"), "UTC")
        self.assertFalse(report.success)
        self.assertEqual(report.errors, ["Command timed out after 240 seconds"])
        self.assertEqual(report.timezone, "UTC")


if __name__ == "__main__":
    unittest.main()
