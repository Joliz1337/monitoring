"""Whitelist анти-DDoS: список адресов идёт сторожу через stdin, сбой виден по коду ответа.

Команда уходит на хост одним аргументом `bash -c`, а один аргумент execve
ограничен 128 КБ: вписанный в неё список на 8–9 тысяч адресов не запускался
вовсе (E2BIG). Эндпоинт при этом отвечал 200 с `success: false`, панель
судит о доставке по коду ответа — и whitelist молча переставал обновляться
на всём парке.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest
from dataclasses import dataclass
from typing import Optional
from unittest import mock

from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers import antiddos as antiddos_router  # noqa: E402
from app.routers.antiddos import WhitelistSyncRequest  # noqa: E402
from app.services.antiddos_manager import WATCHDOG_SCRIPT, AntiDdosManager  # noqa: E402

# Командная строка со списком такого размера — 600 КБ при пределе аргумента в 128 КБ
FLEET_WHITELIST_SIZE = 50_000


@dataclass
class FakeResult:
    success: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None


@dataclass
class Launch:
    command: str
    stdin_data: Optional[str]


class FakeExecutor:
    """Хост со сторожем: запоминает, что запускали и что дали команде на stdin."""

    def __init__(self, sync_result: Optional[FakeResult] = None, installed: bool = True):
        self._sync_result = sync_result or FakeResult()
        self._installed = installed
        self.launches: list[Launch] = []

    async def execute(self, command: str, timeout: int = 30, shell: str = "sh",
                      log_label: Optional[str] = None, stdin_data: Optional[str] = None) -> FakeResult:
        if command == f"test -x {WATCHDOG_SCRIPT}":
            return FakeResult(success=self._installed, exit_code=0 if self._installed else 1)
        self.launches.append(Launch(command, stdin_data))
        return self._sync_result


def sync(executor: FakeExecutor, ips: list[str]) -> tuple[bool, str, int]:
    return asyncio.run(AntiDdosManager(executor).sync_whitelist(ips))


def sync_launch(ips: list[str]) -> Launch:
    executor = FakeExecutor()
    sync(executor, ips)
    (launch,) = executor.launches
    return launch


def addresses(count: int) -> list[str]:
    return [f"10.{i >> 16 & 255}.{i >> 8 & 255}.{i & 255}" for i in range(count)]


class WhitelistSyncTests(unittest.TestCase):

    def test_command_line_does_not_grow_with_the_list(self):
        one, fleet = sync_launch(addresses(1)), sync_launch(addresses(FLEET_WHITELIST_SIZE))

        self.assertEqual({one.command, fleet.command}, {f"{WATCHDOG_SCRIPT} whitelist-sync"})

    def test_every_address_reaches_the_script_on_stdin(self):
        fleet = addresses(FLEET_WHITELIST_SIZE)

        delivered = (sync_launch(fleet).stdin_data or "").split()

        self.assertEqual(delivered, sorted(fleet))

    def test_invalid_and_repeated_entries_never_reach_the_script(self):
        launch = sync_launch([
            "203.0.113.7", " 198.51.100.0/24 ", "203.0.113.7",
            "999.1.1.1", "10.0.0.0/33", "example.com", "2001:db8::1", "1.2.3.4; reboot", "",
        ])

        self.assertEqual((launch.stdin_data or "").split(), ["198.51.100.0/24", "203.0.113.7"])

    def test_failed_script_reports_its_stderr(self):
        executor = FakeExecutor(FakeResult(success=False, exit_code=1, stderr="ipset: Operation not permitted"))

        self.assertEqual(sync(executor, ["203.0.113.7"]), (False, "ipset: Operation not permitted", 0))

    def test_script_that_never_ran_reports_why(self):
        # Не запустился или не уложился в таймаут: stderr пуст, причину знает только исполнитель
        executor = FakeExecutor(FakeResult(success=False, exit_code=-1, error="Command timed out after 60 seconds"))

        self.assertEqual(sync(executor, ["203.0.113.7"]), (False, "Command timed out after 60 seconds", 0))

    def test_sync_without_watchdog_fails_and_runs_nothing(self):
        executor = FakeExecutor(installed=False)

        self.assertEqual(sync(executor, ["203.0.113.7"]), (False, "watchdog not installed", 0))
        self.assertEqual(executor.launches, [])


def post_sync(executor: FakeExecutor, ips: list[str]) -> dict:
    manager = AntiDdosManager(executor)
    with mock.patch.object(antiddos_router, "get_antiddos_manager", return_value=manager):
        return asyncio.run(antiddos_router.sync_whitelist(WhitelistSyncRequest(ips=ips)))


class WhitelistSyncEndpointTests(unittest.TestCase):

    def test_failed_sync_answers_502_with_the_reason(self):
        executor = FakeExecutor(FakeResult(success=False, exit_code=1, stderr="ipset: Operation not permitted"))

        with self.assertRaises(HTTPException) as raised:
            post_sync(executor, ["203.0.113.7"])

        self.assertEqual(
            (raised.exception.status_code, raised.exception.detail),
            (502, "ipset: Operation not permitted"),
        )

    def test_successful_sync_keeps_the_response_body(self):
        # Последняя строка вывода скрипта — число адресов, реально попавших в набор
        executor = FakeExecutor(FakeResult(stdout="2"))

        response = post_sync(executor, ["203.0.113.7", "198.51.100.0/24", "not-an-address"])

        self.assertEqual(response, {"success": True, "message": "whitelist synced", "count": 2})


if __name__ == "__main__":
    unittest.main()
