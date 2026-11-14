"""Установка сторожа анти-DDoS: проверка до замены и откат при неудаче.

Панель ставит сторож сама при расхождении версий, поэтому скрипт из ветки
попадает на весь парк за минуты. Раньше он записывался поверх рабочего и сервис
перезапускался без единой проверки: негодный скрипт оставлял ноду без сторожа и
доезжал заново на каждом опросе.

Первый класс проверяет решения менеджера на подставном хосте и идёт везде. Второй
гоняет настоящие команды в bash над временным каталогом с подставным systemctl —
на Windows он пропускается (запуск под WSL).

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import antiddos_manager, host_executor, host_files  # noqa: E402
from app.services.antiddos_manager import (  # noqa: E402
    CANDIDATE_FILES, CANDIDATE_SUFFIX, INSTALLED_FILES, WATCHDOG_SCRIPT, WATCHDOG_SERVICE, AntiDdosManager,
)
from app.services.host_executor import HostExecutor  # noqa: E402

STATUS_JSON = '{"mode":"off","source":"none","since":0,"reason":"","watchdog":"on","version":"2.6.0"}'
CANDIDATE = WATCHDOG_SCRIPT + CANDIDATE_SUFFIX
LINK_CHECK = " || ".join(f"test -L {path}" for path in INSTALLED_FILES)
SYNTAX_CHECK = f"bash -n {CANDIDATE}"
VERSION_CHECK = f"{CANDIDATE} version"
PLACE = " && ".join(f"mv -fT {path}{CANDIDATE_SUFFIX} {path}" for path in INSTALLED_FILES)
RESTART = f"systemctl restart {WATCHDOG_SERVICE}"
IS_ACTIVE = f"systemctl is-active {WATCHDOG_SERVICE}"
STATUS = f"{WATCHDOG_SCRIPT} status"
DISCARD_CANDIDATES = f"rm -f {CANDIDATE_FILES}"


@dataclass
class FakeResult:
    success: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None


FAILED = FakeResult(success=False, exit_code=1, stderr="boom")


class ScriptedHost:
    """Хост, на котором всё удаётся, кроме команд из `replies`."""

    def __init__(self, replies: Optional[dict[str, FakeResult]] = None):
        self.replies = {LINK_CHECK: FakeResult(success=False, exit_code=1),
                        VERSION_CHECK: FakeResult(stdout="2.6.0"), IS_ACTIVE: FakeResult(stdout="active"),
                        STATUS: FakeResult(stdout=STATUS_JSON), **(replies or {})}
        self.commands: list[str] = []

    async def execute(self, command: str, timeout: int = 30, shell: str = "sh",
                      log_label: Optional[str] = None, stdin_data: Optional[str] = None) -> FakeResult:
        self.commands.append(command)
        return self.replies.get(command, FakeResult())

    def ran(self, command: str) -> bool:
        return command in self.commands

    def position(self, command: str) -> int:
        return self.commands.index(command)


class InstallDecisionsTest(unittest.TestCase):
    def setUp(self):
        self.written: list[tuple[str, str]] = []
        self.write_ok = True

        async def fake_write(path: str, content: str, mode: Optional[str] = None) -> bool:
            self.written.append((path, mode))
            return self.write_ok

        for name, replacement in (("write_host_file", fake_write), ("START_GRACE_SECONDS", 0)):
            patcher = mock.patch.object(antiddos_manager, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self, host: ScriptedHost, enable_watchdog: bool = True) -> tuple[bool, str]:
        return asyncio.run(AntiDdosManager(host).install("#!/bin/bash\n", "[Unit]\n", enable_watchdog))

    def test_working_script_is_installed(self):
        host = ScriptedHost()

        self.assertEqual(self.install(host), (True, "watchdog installed"))
        self.assertFalse(host.ran(antiddos_manager._rollback_command()))

    def test_new_files_are_written_next_to_the_working_ones(self):
        self.install(ScriptedHost())

        self.assertEqual(self.written, [
            (CANDIDATE, "755"),
            (antiddos_manager.WATCHDOG_SERVICE_PATH + CANDIDATE_SUFFIX, "644"),
        ])

    def test_script_is_checked_and_previous_kept_before_anything_is_replaced(self):
        host = ScriptedHost()

        self.install(host)

        keep = host.position(antiddos_manager._keep_previous_command())
        self.assertLess(host.position(SYNTAX_CHECK), host.position(VERSION_CHECK))
        self.assertLess(host.position(VERSION_CHECK), keep)
        self.assertLess(keep, host.position(PLACE))
        self.assertLess(host.position(PLACE), host.position(RESTART))

    def test_service_and_status_are_checked_after_the_restart(self):
        host = ScriptedHost()

        self.install(host)

        self.assertLess(host.position(RESTART), host.position(IS_ACTIVE))
        self.assertLess(host.position(IS_ACTIVE), host.position(STATUS))

    def test_auto_detection_follows_the_panel_default(self):
        for enable, verb in ((True, "watchdog-on"), (False, "watchdog-off")):
            with self.subTest(enable=enable):
                host = ScriptedHost()

                self.install(host, enable_watchdog=enable)

                self.assertTrue(host.ran(f"{WATCHDOG_SCRIPT} {verb}"))

    def test_rejected_script_never_replaces_the_working_one(self):
        rejections = {
            "syntax error": {SYNTAX_CHECK: FakeResult(success=False, exit_code=2, stderr="line 7: syntax error")},
            "version verb fails": {VERSION_CHECK: FAILED},
            "version verb is silent": {VERSION_CHECK: FakeResult(stdout="")},
            "previous copy not kept": {antiddos_manager._keep_previous_command(): FAILED},
        }
        for name, replies in rejections.items():
            with self.subTest(name):
                host = ScriptedHost(replies)

                ok, _ = self.install(host)

                self.assertFalse(ok)
                self.assertFalse(host.ran(PLACE))
                self.assertFalse(any(command.startswith("systemctl") for command in host.commands))
                self.assertFalse(host.ran(antiddos_manager._rollback_command()))
                self.assertTrue(host.ran(DISCARD_CANDIDATES))

    def test_syntax_error_reaches_the_panel(self):
        host = ScriptedHost({SYNTAX_CHECK: FakeResult(success=False, exit_code=2, stderr="line 7: syntax error")})

        ok, message = self.install(host)

        self.assertFalse(ok)
        self.assertIn("line 7: syntax error", message)

    def test_script_that_cannot_be_written_is_not_checked(self):
        self.write_ok = False
        host = ScriptedHost()

        self.assertEqual(self.install(host), (False, "failed to write watchdog script"))
        self.assertEqual(host.commands, [LINK_CHECK, DISCARD_CANDIDATES])

    def test_unit_masked_by_the_admin_is_left_alone(self):
        host = ScriptedHost({LINK_CHECK: FakeResult()})

        ok, message = self.install(host)

        self.assertFalse(ok)
        self.assertIn("masked", message)
        self.assertEqual(self.written, [])
        self.assertEqual(host.commands, [LINK_CHECK, DISCARD_CANDIDATES])

    def test_version_that_does_not_come_up_is_rolled_back(self):
        failures = {
            "files not replaced": {PLACE: FAILED},
            "daemon-reload fails": {"systemctl daemon-reload": FAILED},
            "enable fails": {f"systemctl enable {WATCHDOG_SERVICE}": FAILED},
            "restart fails": {RESTART: FAILED},
            "state is not saved": {f"{WATCHDOG_SCRIPT} watchdog-on": FAILED},
            "service keeps restarting": {IS_ACTIVE: FakeResult(success=False, exit_code=3, stdout="activating")},
            "status verb fails": {STATUS: FAILED},
            "status is not json": {STATUS: FakeResult(stdout="Segmentation fault")},
        }
        for name, replies in failures.items():
            with self.subTest(name):
                host = ScriptedHost(replies)

                ok, message = self.install(host)

                self.assertFalse(ok)
                self.assertEqual(host.commands.count(antiddos_manager._rollback_command()), 1)
                self.assertTrue(message.endswith("rolled back"), message)

    def test_reason_names_the_state_the_service_was_left_in(self):
        host = ScriptedHost({IS_ACTIVE: FakeResult(success=False, exit_code=3, stdout="activating")})

        _, message = self.install(host)

        self.assertIn("watchdog service is activating after restart", message)

    def test_failed_rollback_is_reported_as_such(self):
        host = ScriptedHost({
            RESTART: FAILED,
            antiddos_manager._rollback_command(): FakeResult(success=False, exit_code=1, stderr="unit not found"),
        })

        ok, message = self.install(host)

        self.assertFalse(ok)
        self.assertIn("rollback failed: unit not found", message)


SCRIPT_TEMPLATE = """#!/bin/bash
# {marker}
VERSION="{version}"
STATE="$(dirname "$0")/watchdog.state"
case "${{1:-}}" in
    version) {version_verb} ;;
    status) printf '{{"mode":"off","watchdog":"%s","version":"%s"}}\\n' "$(cat "$STATE" 2>/dev/null)" "$VERSION" ;;
    watchdog-on) echo on > "$STATE" ;;
    watchdog-off) echo off > "$STATE" ;;
    *) exit 1 ;;
esac
"""
CRASH_MARKER = "CRASHES_IN_LOOP"
# Сервис считается упавшим, пока на месте лежит скрипт с пометкой: так подставной
# systemctl отличает негодную версию от возвращённой прежней
FAKE_SYSTEMCTL = f"""#!/bin/sh
echo "$*" >> "$SYSTEMCTL_LOG"
if [ "$1" = is-active ]; then
    grep -q {CRASH_MARKER} "$WATCHDOG_UNDER_TEST" 2>/dev/null && {{ echo activating; exit 3; }}
    echo active
fi
exit 0
"""


def script(version: str, marker: str = "healthy", version_verb: str = 'echo "$VERSION"') -> str:
    return SCRIPT_TEMPLATE.format(version=version, marker=marker, version_verb=version_verb)


def unit(version: str) -> str:
    return f"[Unit]\nDescription=watchdog {version}\n"


@unittest.skipIf(sys.platform == "win32", "команды хоста — bash: на Windows гоняется под WSL")
class InstallOnRealShellTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.script = root / "scripts" / "ddos-watchdog.sh"
        self.unit = root / "systemd" / "ddos-watchdog.service"
        self.script.parent.mkdir()
        self.unit.parent.mkdir()
        self.systemctl_log = root / "systemctl.log"
        stub_dir = root / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "systemctl"
        stub.write_text(FAKE_SYSTEMCTL)
        stub.chmod(0o755)

        installed = (str(self.script), str(self.unit))
        for target, name, value in (
            (antiddos_manager, "WATCHDOG_SCRIPT", installed[0]),
            (antiddos_manager, "WATCHDOG_SERVICE_PATH", installed[1]),
            (antiddos_manager, "INSTALLED_FILES", installed),
            (antiddos_manager, "CANDIDATE_FILES", " ".join(path + CANDIDATE_SUFFIX for path in installed)),
            (antiddos_manager, "START_GRACE_SECONDS", 0),
            (host_executor, "EXTENDED_PATH", f"{stub_dir}:{host_executor.EXTENDED_PATH}"),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {
            "SYSTEMCTL_LOG": str(self.systemctl_log), "WATCHDOG_UNDER_TEST": str(self.script),
        })
        env.start()
        self.addCleanup(env.stop)

        executor = HostExecutor()
        executor._use_nsenter = False
        patcher = mock.patch.object(host_files, "get_host_executor", return_value=executor)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = AntiDdosManager(executor)

    def install(self, script_content: str, service_content: str = unit("new"), enable: bool = True) -> tuple[bool, str]:
        return asyncio.run(self.manager.install(script_content, service_content, enable))

    def have_installed(self, version: str) -> None:
        self.script.write_text(script(version))
        self.script.chmod(0o755)
        self.unit.write_text(unit(version))

    def systemctl_calls(self) -> list[str]:
        return self.systemctl_log.read_text().splitlines() if self.systemctl_log.exists() else []

    def leftovers(self) -> list[str]:
        return sorted(
            path.name for folder in (self.script.parent, self.unit.parent) for path in folder.iterdir()
            if path.name.endswith(CANDIDATE_SUFFIX) or ".tmp." in path.name
        )

    def previous(self, path: Path) -> Path:
        return path.with_name(path.name + antiddos_manager.PREVIOUS_SUFFIX)

    def test_update_replaces_both_files_and_keeps_the_previous_ones(self):
        self.have_installed("2.5.0")

        ok, message = self.install(script("2.6.0"))

        self.assertTrue(ok, message)
        self.assertEqual(self.script.read_text(), script("2.6.0"))
        self.assertEqual(self.unit.read_text(), unit("new"))
        self.assertEqual(self.previous(self.script).read_text(), script("2.5.0"))
        self.assertEqual(self.previous(self.unit).read_text(), unit("2.5.0"))
        self.assertTrue(os.access(self.script, os.X_OK))
        self.assertEqual(self.leftovers(), [])

    def test_update_restarts_the_service_once(self):
        self.have_installed("2.5.0")

        self.install(script("2.6.0"))

        self.assertEqual(self.systemctl_calls(), [
            "daemon-reload", f"enable {WATCHDOG_SERVICE}", f"restart {WATCHDOG_SERVICE}", f"is-active {WATCHDOG_SERVICE}",
        ])

    def test_running_script_is_replaced_by_rename_not_overwritten(self):
        # bash дочитывает исполняемый скрипт по смещению: запись поверх ломает работающий цикл
        self.have_installed("2.5.0")
        inode = self.script.stat().st_ino

        self.install(script("2.6.0"))

        self.assertNotEqual(self.script.stat().st_ino, inode)

    def test_script_with_a_syntax_error_leaves_the_node_untouched(self):
        self.have_installed("2.5.0")
        inode = self.script.stat().st_ino

        ok, message = self.install(script("2.6.0") + "if then\n")

        self.assertFalse(ok)
        self.assertIn("syntax", message)
        self.assertEqual(self.script.stat().st_ino, inode)
        self.assertEqual(self.unit.read_text(), unit("2.5.0"))
        self.assertEqual(self.systemctl_calls(), [])
        self.assertEqual(self.leftovers(), [])

    def test_script_that_does_not_report_a_version_is_rejected(self):
        self.have_installed("2.5.0")

        ok, message = self.install(script("2.6.0", version_verb=":"))

        self.assertFalse(ok)
        self.assertIn("version", message)
        self.assertEqual(self.script.read_text(), script("2.5.0"))
        self.assertEqual(self.systemctl_calls(), [])

    def test_version_that_crashes_after_start_gives_way_to_the_previous_one(self):
        self.have_installed("2.5.0")

        ok, message = self.install(script("2.6.0", marker=CRASH_MARKER))

        self.assertFalse(ok)
        self.assertIn("activating", message)
        self.assertEqual(self.script.read_text(), script("2.5.0"))
        self.assertEqual(self.unit.read_text(), unit("2.5.0"))
        self.assertTrue(os.access(self.script, os.X_OK))
        self.assertEqual(self.leftovers(), [])
        # Последним идёт рестарт сервиса уже на возвращённой версии
        self.assertEqual(self.systemctl_calls()[-2:], ["daemon-reload", f"restart {WATCHDOG_SERVICE}"])

    def test_failed_first_install_leaves_no_watchdog_behind(self):
        ok, _ = self.install(script("2.6.0", marker=CRASH_MARKER))

        self.assertFalse(ok)
        self.assertFalse(self.script.exists())
        self.assertFalse(self.unit.exists())
        self.assertIn(f"disable --now {WATCHDOG_SERVICE}", self.systemctl_calls())

    def test_copy_left_by_an_earlier_install_is_not_taken_for_the_previous_version(self):
        # Сторож снесли руками, .prev остался: откат первой установки не должен его воскресить
        self.previous(self.script).write_text(script("1.0.0"))

        ok, _ = self.install(script("2.6.0", marker=CRASH_MARKER))

        self.assertFalse(ok)
        self.assertFalse(self.script.exists())

    def test_masked_unit_stays_masked(self):
        # systemctl mask — ссылка на /dev/null: rename заменил бы её настоящим юнитом
        self.have_installed("2.5.0")
        self.unit.unlink()
        self.unit.symlink_to("/dev/null")

        ok, message = self.install(script("2.6.0"))

        self.assertFalse(ok)
        self.assertIn("masked", message)
        self.assertTrue(self.unit.is_symlink())
        self.assertEqual(self.script.read_text(), script("2.5.0"))
        self.assertEqual(self.systemctl_calls(), [])
        self.assertEqual(self.leftovers(), [])

    def test_first_install_leaves_no_previous_copy(self):
        ok, message = self.install(script("2.6.0"))

        self.assertTrue(ok, message)
        self.assertEqual(self.script.read_text(), script("2.6.0"))
        self.assertFalse(self.previous(self.script).exists())
        self.assertFalse(self.previous(self.unit).exists())

    def test_installed_script_gets_the_auto_detection_default(self):
        self.install(script("2.6.0"), enable=False)

        self.assertEqual((self.script.parent / "watchdog.state").read_text().strip(), "off")


if __name__ == "__main__":
    unittest.main()
