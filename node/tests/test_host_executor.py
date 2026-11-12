"""HostExecutor: данные для команды идут через stdin, а не в командной строке.

Один аргумент execve ограничен 128 КБ (MAX_ARG_STRLEN), а команда уходит в
`bash -c` одним аргументом: whitelist анти-DDoS на десять тысяч адресов,
вписанный в неё, не запускался вовсе — E2BIG ещё до старта скрипта. Stdin
таким пределом не связан.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import host_executor  # noqa: E402
from app.services.host_executor import HostExecutor  # noqa: E402

MAX_ARG_STRLEN = 131072
WHITELIST_SIZE = 50_000


def whitelist_text() -> str:
    return "\n".join(f"10.{i >> 16 & 255}.{i >> 8 & 255}.{i & 255}" for i in range(WHITELIST_SIZE))


class EchoProcess:
    """Как `cat`: отдаёт в stdout то, что пришло на stdin. Без канала stdin
    asyncio молча отбрасывает переданные данные — подставной процесс повторяет и это."""

    returncode = 0

    def __init__(self, stdin_piped: bool):
        self._stdin_piped = stdin_piped

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return (input if self._stdin_piped and input else b""), b""


class FakeSpawn:
    """Подмена asyncio.create_subprocess_exec: запоминает, что получил каждый процесс на stdin."""

    def __init__(self):
        self.stdin_of_each: list = []

    async def __call__(self, *argv: str, stdin=None, stdout=None, stderr=None) -> EchoProcess:
        self.stdin_of_each.append(stdin)
        return EchoProcess(stdin_piped=stdin == asyncio.subprocess.PIPE)


class StdinDataTests(unittest.TestCase):

    def setUp(self):
        self.spawn = FakeSpawn()
        patcher = mock.patch.object(host_executor.asyncio, "create_subprocess_exec", self.spawn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stdin_data_reaches_the_command(self):
        result = asyncio.run(HostExecutor().execute("cat", stdin_data="1.2.3.4\n10.0.0.0/8"))

        self.assertTrue(result.success)
        self.assertEqual(result.stdout, "1.2.3.4\n10.0.0.0/8")

    def test_command_without_stdin_data_inherits_agent_stdin(self):
        asyncio.run(HostExecutor().execute("uptime"))

        self.assertEqual(self.spawn.stdin_of_each, [None])


@unittest.skipIf(sys.platform == "win32", "нужен POSIX sh хоста: на Windows гоняется под WSL")
class RealProcessTests(unittest.TestCase):

    def test_list_beyond_the_argument_limit_arrives_whole(self):
        whitelist = whitelist_text()
        self.assertGreater(len(whitelist), MAX_ARG_STRLEN)
        # В контейнере CI исполнитель пошёл бы через nsenter, на который там нет прав
        with mock.patch.object(host_executor, "running_in_container", return_value=False):
            executor = HostExecutor()

        result = asyncio.run(executor.execute("awk 'END { print NR, $0 }'", stdin_data=whitelist + "\n"))

        self.assertIsNone(result.error)
        self.assertEqual(result.stdout, f"{WHITELIST_SIZE} {whitelist.splitlines()[-1]}")


if __name__ == "__main__":
    unittest.main()
