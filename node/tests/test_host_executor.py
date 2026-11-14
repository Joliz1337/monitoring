"""HostExecutor: данные для команды идут через stdin, а таймаут снимает всё её дерево.

Один аргумент execve ограничен 128 КБ (MAX_ARG_STRLEN), а команда уходит в
`bash -c` одним аргументом: whitelist анти-DDoS на десять тысяч адресов,
вписанный в неё, не запускался вовсе — E2BIG ещё до старта скрипта. Stdin
таким пределом не связан.

Команда хоста — не прямой потомок исполнителя: между ними sh, а на ноде ещё и
nsenter. По таймауту убивали только прямого потомка — сама команда жила дальше
с блокировкой xtables или dpkg и с открытыми stdout/stderr, из-за которых
asyncio не возвращал и сам вызов.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
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
        self.new_session_of_each: list = []

    async def __call__(self, *argv: str, stdin=None, stdout=None, stderr=None, start_new_session=False) -> EchoProcess:
        self.stdin_of_each.append(stdin)
        self.new_session_of_each.append(start_new_session)
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

    def test_command_leads_its_own_session(self):
        # Без своей сессии группа команды — группа самого агента: убивать её нельзя
        asyncio.run(HostExecutor().execute("uptime"))

        self.assertEqual(self.spawn.new_session_of_each, [True])


class HangingProcess:
    pid = 4242

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        await asyncio.sleep(3600)
        return b"", b""

    async def wait(self) -> int:
        return -signal.SIGTERM


class TimeoutTests(unittest.TestCase):

    def test_timeout_kills_the_process_group_of_the_command(self):
        killed: list[int] = []

        async def spawn(*argv: str, **options) -> HangingProcess:
            return HangingProcess()

        with mock.patch.object(host_executor.asyncio, "create_subprocess_exec", spawn), \
                mock.patch.object(host_executor, "_kill_process_group", killed.append):
            result = asyncio.run(HostExecutor().execute("sleep 600", timeout=1))

        self.assertEqual(killed, [HangingProcess.pid])
        self.assertEqual(result.error, "Command timed out after 1 seconds")


@unittest.skipIf(sys.platform == "win32", "нужны POSIX-сессии и flock: на Windows гоняется под WSL")
class TimeoutTreeTests(unittest.TestCase):
    """Команда держит блокировку в потомке: sh → flock → sh → sleep. Убитый по
    таймауту прямой потомок её не снимает — снимает только гибель всего дерева."""

    GUARD_SEC = 15

    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.lock = Path(work.name) / "held.lock"
        self.command = f"flock {self.lock} sh -c 'sleep 600; true'"
        with mock.patch.object(host_executor, "running_in_container", return_value=False):
            self.executor = HostExecutor()

    def lock_is_released(self) -> bool:
        import fcntl

        deadline = time.monotonic() + 3
        with open(self.lock, "w") as handle:
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except BlockingIOError:
                    time.sleep(0.05)
        return False

    def test_async_timeout_returns_in_time_and_releases_the_lock(self):
        started = time.monotonic()
        result = asyncio.run(asyncio.wait_for(self.executor.execute(self.command, timeout=1), self.GUARD_SEC))

        self.assertEqual(result.error, "Command timed out after 1 seconds")
        self.assertLess(time.monotonic() - started, 4)
        self.assertTrue(self.lock_is_released())

    def test_sync_timeout_releases_the_lock(self):
        result = self.executor.execute_sync(self.command, timeout=1)

        self.assertEqual(result.error, "Command timed out after 1 seconds")
        self.assertTrue(self.lock_is_released())

    def test_stream_timeout_ends_the_stream_and_releases_the_lock(self):
        async def collect() -> list[str]:
            return [event.split("\n")[0] async for event in self.executor.execute_stream(self.command, timeout=1)]

        events = asyncio.run(asyncio.wait_for(collect(), self.GUARD_SEC))

        self.assertEqual(events, ["event: error", "event: done"])
        self.assertTrue(self.lock_is_released())

    def test_child_that_left_the_session_does_not_hang_the_call(self):
        # setsid выводит потомка из группы: убить его нечем, а stdout он держит
        pid_file = self.lock.with_name("detached.pid")
        self.addCleanup(self.kill_detached, pid_file)
        command = f"setsid sh -c 'echo $$ > {pid_file}; exec sleep 600' & sleep 600"

        with mock.patch.object(host_executor, "REAP_TIMEOUT", 0.3):
            result = asyncio.run(asyncio.wait_for(self.executor.execute(command, timeout=1), self.GUARD_SEC))

        self.assertEqual(result.error, "Command timed out after 1 seconds")

    @staticmethod
    def kill_detached(pid_file: Path) -> None:
        try:
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


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
