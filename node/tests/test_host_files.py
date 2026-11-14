"""Tests for exact host-file reads.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

HostExecutor стрипает stdout, поэтому обычный `cat` теряет завершающий
перевод строки. Для конфигов это ломало откат байт-в-байт и сверку хэшей.
"""

import asyncio
import base64
import os
import stat
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import host_files  # noqa: E402
from app.services.host_executor import HostExecutor  # noqa: E402


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

    async def execute(self, command: str, timeout: int = 5, shell: str = "sh", log_label=None):
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
        self.log_labels: list = []

    async def execute(self, command: str, timeout: int = 5, shell: str = "sh", log_label=None):
        self.commands.append(command)
        self.log_labels.append(log_label)
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

    def test_secret_content_never_reaches_the_log(self):
        # Исполнитель логирует начало команды, а в нём начало base64 файла
        executor = self._executor(WriteExecutor())
        asyncio.run(host_files.write_host_file("/opt/monitoring-node/.env", "NODE_SECRET=abc\n", secret=True))
        self.assertEqual(executor.log_labels, ["write /opt/monitoring-node/.env"])

    def test_plain_write_logs_as_before(self):
        executor = self._executor(WriteExecutor())
        asyncio.run(host_files.write_host_file("/etc/haproxy/haproxy.cfg", "global\n"))
        self.assertEqual(executor.log_labels, [None])

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
        ok = asyncio.run(host_files.write_host_file("/opt/remnawave/nginx.conf", CONFIG, in_place=True))
        self.assertTrue(ok)
        self.assertNotIn("umask", executor.commands[0])
        self.assertNotIn("chmod", executor.commands[0])


class AtomicWriteTests(unittest.TestCase):
    """Файл встаёт на место одним rename из временного рядом с целью.

    Запись прямо в файл оставляла его обрезанным при сбое посреди записи, а
    работающий bash дочитывал новый скрипт со старого смещения.
    """

    def command(self, path: str, mode=None, in_place: bool = False) -> str:
        executor = WriteExecutor(mode or "")
        original = host_files.get_host_executor
        host_files.get_host_executor = lambda: executor
        try:
            asyncio.run(host_files.write_host_file(path, "#!/bin/bash\n", mode=mode, in_place=in_place))
        finally:
            host_files.get_host_executor = original
        return executor.commands[0]

    def test_content_goes_to_a_temp_file_and_is_renamed_over_the_target(self):
        command = self.command("/opt/monitoring/scripts/ddos-watchdog.sh", mode="755")

        self.assertIn('base64 -d > "$tmp"', command)
        self.assertNotIn("base64 -d > /opt/monitoring/scripts/ddos-watchdog.sh", command)
        self.assertLess(command.index('base64 -d > "$tmp"'), command.index('mv -fT "$tmp" "$target"'))

    def test_temp_file_lives_next_to_the_target(self):
        # rename атомарен только внутри одной файловой системы
        command = self.command("/etc/systemd/system/ddos-watchdog.service", mode="644")

        self.assertIn('tmp="$target.tmp.$$"', command)

    def test_target_that_is_not_a_regular_file_is_refused_before_anything_is_written(self):
        command = self.command("/etc/systemd/system/ddos-watchdog.service", mode="644")

        self.assertLess(command.index('[ -f "$target" ] ||'), command.index("base64 -d"))

    def test_failed_write_removes_the_temp_file_and_keeps_the_target(self):
        command = self.command("/opt/monitoring/scripts/ddos-watchdog.sh", mode="755")

        self.assertIn('base64 -d > "$tmp" || { rm -f "$tmp"; exit 1; }', command)

    def test_symlink_is_followed_like_an_in_place_write_did(self):
        command = self.command("/etc/letsencrypt/live/example.com/fullchain.pem", mode="644")

        self.assertIn("target=$(readlink -f /etc/letsencrypt/live/example.com/fullchain.pem)", command)

    def test_owner_of_the_existing_file_is_carried_over(self):
        # authorized_keys пользователя, переписанный от root, sshd читать бы не стал
        command = self.command("/home/ubuntu/.ssh/authorized_keys", mode="600")

        self.assertIn('chown --reference="$target" "$tmp"', command)

    def test_requested_mode_is_set_before_the_file_appears_at_the_target(self):
        command = self.command("/opt/monitoring/scripts/ddos-watchdog.sh", mode="755")

        self.assertLess(command.index('chmod 755 "$tmp"'), command.index("mv -fT"))
        self.assertNotIn("chmod --reference", command)

    def test_without_mode_the_existing_permissions_are_carried_over(self):
        command = self.command("/opt/remnawave/docker-compose.yml")

        self.assertIn('chmod --reference="$target" "$tmp"', command)
        self.assertNotIn("umask", command)

    def test_in_place_write_keeps_the_inode(self):
        # single-file bind-mount: после rename контейнер остался бы на старом файле
        command = self.command("/opt/remnawave/nginx.conf", mode="644", in_place=True)

        self.assertIn("base64 -d > /opt/remnawave/nginx.conf", command)
        self.assertNotIn("mv ", command)

    def test_sync_twin_writes_atomically_too(self):
        executor = mock.Mock()
        executor.execute_sync.return_value = FakeResult()
        with mock.patch.object(host_files, "get_host_executor", return_value=executor):
            ok = host_files.write_host_file_sync("/opt/monitoring/firewall/backup.json", "{}")

        self.assertTrue(ok)
        self.assertIn('mv -fT "$tmp" "$target"', executor.execute_sync.call_args.args[0])


@unittest.skipIf(sys.platform == "win32", "команда записи — bash: на Windows гоняется под WSL")
class AtomicWriteOnRealShellTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        executor = HostExecutor()
        executor._use_nsenter = False
        patcher = mock.patch.object(host_files, "get_host_executor", return_value=executor)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path: Path, content: str, **kwargs) -> bool:
        return asyncio.run(host_files.write_host_file(str(path), content, **kwargs))

    def mode(self, path: Path) -> str:
        return oct(stat.S_IMODE(path.stat().st_mode))[2:]

    def temp_files(self, folder: Path) -> list[str]:
        return [path.name for path in folder.iterdir() if ".tmp." in path.name]

    def test_new_file_gets_content_and_mode_in_a_missing_directory(self):
        target = self.root / "scripts" / "watchdog.sh"

        self.assertTrue(self.write(target, "#!/bin/bash\n", mode="755"))

        self.assertEqual(target.read_text(), "#!/bin/bash\n")
        self.assertEqual(self.mode(target), "755")
        self.assertEqual(self.temp_files(target.parent), [])

    def test_existing_file_is_replaced_by_a_new_inode(self):
        target = self.root / "watchdog.sh"
        target.write_text("old\n")
        inode = target.stat().st_ino

        self.assertTrue(self.write(target, "new\n", mode="755"))

        self.assertEqual(target.read_text(), "new\n")
        self.assertNotEqual(target.stat().st_ino, inode)

    def test_failed_write_leaves_the_old_content(self):
        # Каталог вместо файла-цели: rename невозможен, как и при любой другой неудаче
        target = self.root / "occupied"
        (target / "keep").mkdir(parents=True)

        self.assertFalse(self.write(target, "new\n", mode="644"))

        self.assertEqual([path.name for path in target.iterdir()], ["keep"])
        self.assertEqual(self.temp_files(self.root), [])

    def test_symlink_stays_a_symlink_and_its_target_is_rewritten(self):
        real = self.root / "archive" / "cert1.pem"
        real.parent.mkdir()
        real.write_text("old")
        link = self.root / "cert.pem"
        link.symlink_to(real)

        self.assertTrue(self.write(link, "renewed", mode="644"))

        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), "renewed")

    def test_symlink_to_a_device_is_refused_and_the_device_survives(self):
        # Так выглядит замаскированный юнит systemd: ссылка на /dev/null
        device = self.root / "null"
        os.mkfifo(device)
        link = self.root / "masked.service"
        link.symlink_to(device)

        self.assertFalse(self.write(link, "[Unit]\n", mode="644"))

        self.assertTrue(stat.S_ISFIFO(device.stat().st_mode))
        self.assertTrue(link.is_symlink())

    def test_permissions_of_the_existing_file_survive_a_write_without_mode(self):
        target = self.root / "compose.yml"
        target.write_text("old\n")
        target.chmod(0o640)

        self.assertTrue(self.write(target, "new\n"))

        self.assertEqual(self.mode(target), "640")

    def test_in_place_write_keeps_the_inode(self):
        target = self.root / "nginx.conf"
        target.write_text("old\n")
        inode = target.stat().st_ino

        self.assertTrue(self.write(target, "new\n", mode="644", in_place=True))

        self.assertEqual(target.read_text(), "new\n")
        self.assertEqual(target.stat().st_ino, inode)


if __name__ == "__main__":
    unittest.main()
