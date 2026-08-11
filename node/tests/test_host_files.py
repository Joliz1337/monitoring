"""Tests for exact host-file reads.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

HostExecutor стрипает stdout, поэтому обычный `cat` теряет завершающий
перевод строки. Для конфигов это ломало откат байт-в-байт и сверку хэшей.
"""

import asyncio
import base64
import os
import sys
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import host_files  # noqa: E402


@dataclass
class FakeResult:
    success: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeExecutor:
    """Повторяет ключевое свойство HostExecutor — strip() на stdout."""

    def __init__(self, files: dict):
        self.files = files
        self.commands: list[str] = []

    async def execute(self, command: str, timeout: int = 5, shell: str = "sh"):
        self.commands.append(command)
        for path, content in self.files.items():
            if command == f"cat {path}":
                return FakeResult(stdout=content.strip())
            if command == f"base64 -w0 {path}":
                encoded = base64.b64encode(content.encode()).decode()
                return FakeResult(stdout=encoded.strip())
        return FakeResult(success=False, exit_code=1)


CONFIG = "http {\n    server_tokens off;\n}\n"


class WriteExecutor:
    """Отдаёт итоговые права файла — ровно то, что печатает `stat` в конце
    команды записи."""

    def __init__(self, final_mode: str = "", write_succeeds: bool = True):
        self.final_mode = final_mode
        self.write_succeeds = write_succeeds
        self.commands: list[str] = []

    async def execute(self, command: str, timeout: int = 5, shell: str = "sh"):
        self.commands.append(command)
        if not self.write_succeeds:
            return FakeResult(success=False, exit_code=1, stderr="Read-only file system")
        return FakeResult(stdout=self.final_mode)


class ExactReadTests(unittest.TestCase):
    def setUp(self):
        self.executor = FakeExecutor({"/opt/remnawave/nginx.conf": CONFIG})
        self._orig = host_files.get_host_executor
        host_files.get_host_executor = lambda: self.executor

    def tearDown(self):
        host_files.get_host_executor = self._orig

    def test_plain_read_loses_trailing_newline(self):
        result = asyncio.run(host_files.read_host_file("/opt/remnawave/nginx.conf"))
        self.assertNotEqual(result, CONFIG)

    def test_exact_read_is_byte_identical(self):
        result = asyncio.run(host_files.read_host_file_exact("/opt/remnawave/nginx.conf"))
        self.assertEqual(result, CONFIG)

    def test_missing_file_returns_none(self):
        self.assertIsNone(asyncio.run(host_files.read_host_file_exact("/nope.conf")))


class WriteWithModeTests(unittest.TestCase):
    """Права выставляются umask'ом при создании, chmod — только доводка.

    На pmxcfs (`/etc/pve` на Proxmox) chmod запрещён: раньше его отказ ронял
    всю команду уже после успешной записи, и деплой wildcard-сертификата на
    Proxmox-ноду сообщал об ошибке, откатывая сертификат из бэкапа.
    """

    def _executor(self, executor):
        host_files.get_host_executor = lambda: executor
        return executor

    def setUp(self):
        self._orig = host_files.get_host_executor

    def tearDown(self):
        host_files.get_host_executor = self._orig

    def test_denied_chmod_passes_when_file_is_not_world_readable(self):
        self._executor(WriteExecutor("640"))
        ok = asyncio.run(host_files.write_host_file(
            "/etc/pve/local/pveproxy-ssl.key", "key", mode="600"
        ))
        self.assertTrue(ok)

    def test_world_readable_private_key_is_a_failure(self):
        self._executor(WriteExecutor("644"))
        ok = asyncio.run(host_files.write_host_file("/etc/ssl/privkey.pem", "key", mode="600"))
        self.assertFalse(ok)

    def test_script_left_without_execute_bit_is_a_failure(self):
        self._executor(WriteExecutor("644"))
        ok = asyncio.run(host_files.write_host_file(
            "/opt/monitoring/antiddos/ddos-watchdog.sh", "#!/bin/bash\n", mode="755"
        ))
        self.assertFalse(ok)

    def test_exact_mode_passes(self):
        self._executor(WriteExecutor("755"))
        ok = asyncio.run(host_files.write_host_file(
            "/opt/monitoring/antiddos/ddos-watchdog.sh", "#!/bin/bash\n", mode="755"
        ))
        self.assertTrue(ok)

    def test_umask_applies_before_content_is_written(self):
        executor = self._executor(WriteExecutor("600"))
        asyncio.run(host_files.write_host_file("/etc/ssl/privkey.pem", "key", mode="600"))
        command = executor.commands[0]
        # Перенаправление создаёт файл от 0666, поэтому режиму 600 отвечает umask 177.
        self.assertLess(command.index("umask 177"), command.index("base64 -d"))

    def test_failed_write_is_still_a_failure(self):
        self._executor(WriteExecutor(write_succeeds=False))
        ok = asyncio.run(host_files.write_host_file("/etc/ssl/privkey.pem", "key", mode="600"))
        self.assertFalse(ok)

    def test_write_without_mode_touches_neither_umask_nor_chmod(self):
        executor = self._executor(WriteExecutor())
        ok = asyncio.run(host_files.write_host_file("/opt/remnawave/nginx.conf", CONFIG))
        self.assertTrue(ok)
        self.assertNotIn("umask", executor.commands[0])
        self.assertNotIn("chmod", executor.commands[0])


if __name__ == "__main__":
    unittest.main()
