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


if __name__ == "__main__":
    unittest.main()
