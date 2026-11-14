"""Тесты раскатки сторожа анти-DDoS (app/services/antiddos_manager.py).

Панель сама ставит сторож на ноды, где версия разошлась с `WATCHDOG_VERSION` на
GitHub. Раньше скачанный файл уходил на весь парк без единой проверки и сразу
всем: негодный скрипт из ветки оказывался на каждой ноде за один проход опроса.
Теперь он проверяется до рассылки, а новая версия идёт канарейкой — горстке нод,
остальным после подтверждений; при отказах раскатка останавливается.

Голый unittest, без PostgreSQL, сети и bash: сессия, ноды и подпроцесс подменены.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import json
import os
import sys
import unittest
from types import SimpleNamespace
from typing import Optional
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import AntiDdosSettings, Server  # noqa: E402
from app.services import antiddos_manager  # noqa: E402
from app.services.antiddos_manager import (  # noqa: E402
    CANARY_CONFIRMATIONS, CANARY_INSTALLS, ROLLOUT_FAILURE_LIMIT,
    AntiDdosManager, WatchdogRelease, bash_syntax_problem, release_problem, watchdog_version,
)
from app.services.node_capabilities import (  # noqa: E402
    ACCESS_NONE, ACCESS_READ, ACCESS_WRITE, Capability, denied_message,
)

SCRIPT = '#!/bin/bash\nset -u\nWATCHDOG_VERSION="2.7.0"\ncase "$1" in version) echo "$WATCHDOG_VERSION" ;; esac\n'
SERVICE = "[Unit]\nDescription=watchdog\n[Service]\nExecStart=/opt/monitoring/scripts/ddos-watchdog.sh loop\n"
RELEASE = WatchdogRelease(SCRIPT, SERVICE, "2.7.0")
WHITELIST = ["104.16.0.0/13", "5.9.0.10"]


def patch_module(case: unittest.TestCase, **replacements) -> None:
    for name, replacement in replacements.items():
        patcher = mock.patch.object(antiddos_manager, name, replacement)
        patcher.start()
        case.addCleanup(patcher.stop)


def capabilities(**granted: str) -> str:
    """Карта NODE_CAPABILITIES, как её шлёт нода: закрыто всё, кроме перечисленного."""
    return json.dumps({**{c.value: ACCESS_NONE for c in Capability}, **granted}, sort_keys=True)


def node(server_id: int, node_capabilities: Optional[str] = None) -> Server:
    return Server(
        id=server_id, name=f"node-{server_id}", url=f"https://10.0.0.{server_id}:9100",
        node_capabilities=node_capabilities,
    )


def status(version: str = "2.6.0", installed: bool = True, active: bool = True) -> dict:
    return {
        "installed": installed, "version": version if installed else "", "watchdog_active": active,
        "mode": "off", "source": "none", "since": 0, "reason": "", "watchdog": "on",
    }


class ReleaseSanityTest(unittest.TestCase):
    def test_script_from_the_repository_shape_passes(self):
        self.assertIsNone(release_problem(SCRIPT, SERVICE))

    def test_what_github_may_return_instead_of_the_script_is_rejected(self):
        broken = {
            "empty answer": "",
            "only whitespace": "\n\n",
            "error page": "<!DOCTYPE html><html><body>429: Too Many Requests</body></html>",
            "cut before the version line": "#!/bin/bash\nset -u\n",
            "version without quotes": "#!/bin/bash\nWATCHDOG_VERSION=2.7.0\n",
            "version only in a comment": '#!/bin/bash\n# was WATCHDOG_VERSION="2.6.0" before\n',
            "shebang is not the first line": '\n#!/bin/bash\nWATCHDOG_VERSION="2.7.0"\n',
        }
        for name, script in broken.items():
            with self.subTest(name):
                self.assertIsNotNone(release_problem(script, SERVICE))

    def test_error_page_instead_of_the_unit_is_rejected(self):
        self.assertIsNotNone(release_problem(SCRIPT, "404: Not Found"))

    def test_expected_version_is_the_one_written_in_the_script(self):
        self.assertEqual(watchdog_version(SCRIPT), "2.7.0")


class FakeProcess:
    def __init__(self, returncode: int = 0, stderr: bytes = b"", hangs: bool = False):
        self.returncode = returncode
        self._stderr = stderr
        self._hangs = hangs
        self.stdin_data: Optional[bytes] = None
        self.killed = False

    async def communicate(self, data: bytes):
        self.stdin_data = data
        if self._hangs:
            await asyncio.sleep(3600)
        return b"", self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


class BashSyntaxCheckTest(unittest.IsolatedAsyncioTestCase):
    async def check(self, process=None, error: Optional[Exception] = None) -> Optional[str]:
        spawn = mock.AsyncMock(return_value=process, side_effect=error)
        with mock.patch("asyncio.create_subprocess_exec", spawn):
            problem = await bash_syntax_problem(SCRIPT)
        self.spawn = spawn
        return problem

    async def test_script_goes_to_bash_dash_n_on_stdin(self):
        process = FakeProcess()

        self.assertIsNone(await self.check(process))

        self.assertEqual(self.spawn.await_args.args, ("bash", "-n"))
        self.assertEqual(process.stdin_data, SCRIPT.encode())

    async def test_syntax_error_is_reported_with_the_text_from_bash(self):
        problem = await self.check(FakeProcess(2, b"bash: line 7: syntax error near unexpected token `then'"))

        self.assertIn("line 7: syntax error", problem)

    async def test_panel_without_bash_skips_only_this_check(self):
        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            self.assertIsNone(await self.check(error=FileNotFoundError("bash")))

    async def test_bash_that_cannot_be_started_is_not_a_pass(self):
        self.assertIn("could not run", await self.check(error=PermissionError("denied")))

    async def test_hung_check_is_killed_and_fails(self):
        process = FakeProcess(hangs=True)
        patch_module(self, BASH_CHECK_TIMEOUT=0.01)

        self.assertEqual(await self.check(process), "bash -n timed out")
        self.assertTrue(process.killed)


class FakeGithub:
    def __init__(self, script: str = SCRIPT, service: str = SERVICE, status_code: int = 200):
        self.script, self.service, self.status_code = script, service, status_code
        self.downloads = 0

    async def get(self, url: str, timeout: float):
        self.downloads += 1
        await asyncio.sleep(0)
        return SimpleNamespace(
            status_code=self.status_code, text=self.script if url.endswith(".sh") else self.service,
        )


class ReleaseDownloadTest(unittest.IsolatedAsyncioTestCase):
    def use(self, github: FakeGithub, syntax_problem: Optional[str] = None) -> AntiDdosManager:
        self.github = github
        self.syntax_check = mock.AsyncMock(return_value=syntax_problem)
        patch_module(
            self,
            get_external_client=lambda: github,
            update_channel=SimpleNamespace(github_configs_base=lambda: "https://raw.example/configs"),
            bash_syntax_problem=self.syntax_check,
        )
        return AntiDdosManager()

    async def test_checked_script_becomes_the_release(self):
        manager = self.use(FakeGithub())

        self.assertEqual(await manager._watchdog_release(), RELEASE)

    async def test_release_is_downloaded_and_checked_once_for_the_whole_fleet(self):
        manager = self.use(FakeGithub())

        await asyncio.gather(*(manager._watchdog_release() for _ in range(50)))

        self.assertEqual(self.github.downloads, 2)
        self.syntax_check.assert_awaited_once()

    async def test_script_that_fails_bash_is_not_a_release(self):
        manager = self.use(FakeGithub(), syntax_problem="bash -n: line 7: syntax error")

        with self.assertLogs(antiddos_manager.logger, level="ERROR") as logs:
            self.assertIsNone(await manager._watchdog_release())

        self.assertIn("line 7: syntax error", logs.output[0])

    async def test_rejected_script_is_not_downloaded_again_on_every_poll(self):
        manager = self.use(FakeGithub(script="<html>oops</html>"))

        with self.assertLogs(antiddos_manager.logger, level="ERROR"):
            await manager._watchdog_release()
        await manager._watchdog_release()

        self.assertEqual(self.github.downloads, 2)
        self.syntax_check.assert_not_awaited()

    async def test_github_failure_is_retried_soon(self):
        manager = self.use(FakeGithub(status_code=503))
        patch_module(self, RELEASE_RETRY_INTERVAL=0)

        self.assertIsNone(await manager._watchdog_release())
        self.github.status_code = 200

        self.assertEqual(await manager._watchdog_release(), RELEASE)

    async def test_nothing_is_sent_to_a_node_without_a_valid_release(self):
        manager = self.use(FakeGithub(script="#!/bin/bash\n"))
        node_client = mock.Mock()
        patch_module(self, get_node_apply_client=node_client)

        with self.assertLogs(antiddos_manager.logger, level="ERROR"):
            result = await manager.install_to_node(node(1))

        self.assertEqual(result, (False, "no valid watchdog script from GitHub"))
        node_client.assert_not_called()


class FakeSession:
    def __init__(self, rows: dict[type, list]) -> None:
        self._rows = rows

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def execute(self, statement):
        entities = self._rows.get(statement.column_descriptions[0]["entity"], [])
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: list(entities)),
            scalar_one_or_none=lambda: entities[0] if entities else None,
        )

    async def commit(self) -> None:
        return None


class InstallAnswerTest(unittest.IsolatedAsyncioTestCase):
    async def install(self, answer: dict, node_capabilities: Optional[str] = None) -> tuple[bool, str]:
        self.posted: list[dict] = []

        async def post(url: str, **kwargs):
            self.posted.append(kwargs["json"])
            return SimpleNamespace(status_code=200, json=lambda: answer)

        patch_module(
            self,
            async_session=lambda: FakeSession({AntiDdosSettings: [AntiDdosSettings(watchdog_default_enabled=False)]}),
            get_node_apply_client=lambda server: SimpleNamespace(post=post),
            node_auth_headers=lambda server: {},
        )
        manager = AntiDdosManager()
        self.release = manager._watchdog_release = mock.AsyncMock(return_value=RELEASE)
        return await manager.install_to_node(node(1, node_capabilities))

    async def test_node_gets_the_checked_files_and_the_panel_default(self):
        result = await self.install({"success": True, "message": "watchdog installed"})

        self.assertEqual(result, (True, "installed"))
        self.assertEqual(self.posted, [
            {"script_content": SCRIPT, "service_content": SERVICE, "watchdog_enabled": False},
        ])

    async def test_reason_of_a_node_that_rolled_back_reaches_the_panel(self):
        result = await self.install({
            "success": False, "message": "watchdog service is activating after restart; rolled back",
        })

        self.assertEqual(result, (False, "watchdog service is activating after restart; rolled back"))

    async def test_node_without_exec_is_refused_before_anything_is_sent(self):
        # Скрипт сторожа нода исполняет от root: одного раздела antiddos для этого мало
        result = await self.install({"success": True}, capabilities(antiddos=ACCESS_WRITE))

        self.assertEqual(result, (False, denied_message(Capability.EXEC, True)))
        self.assertEqual(self.posted, [])
        self.release.assert_not_awaited()

    async def test_node_that_closed_the_section_is_refused_with_its_own_reason(self):
        result = await self.install({"success": True}, capabilities(antiddos=ACCESS_READ, exec=ACCESS_WRITE))

        self.assertEqual(result, (False, denied_message(Capability.ANTIDDOS, True)))
        self.assertEqual(self.posted, [])

    async def test_restricted_node_that_grants_exec_gets_the_watchdog(self):
        result = await self.install(
            {"success": True}, capabilities(antiddos=ACCESS_WRITE, exec=ACCESS_WRITE),
        )

        self.assertEqual(result, (True, "installed"))
        self.assertEqual(len(self.posted), 1)


class RolloutCase(unittest.IsolatedAsyncioTestCase):
    """Менеджер, у которого установка на ноду и рассылка whitelist подменены."""

    async def asyncSetUp(self) -> None:
        self.manager = AntiDdosManager()
        self.failing: dict[int, str] = {}
        self.install_calls: list[int] = []

        async def install_to_node(server: Server) -> tuple[bool, str]:
            self.install_calls.append(server.id)
            if server.id in self.failing:
                return False, self.failing[server.id]
            return True, "installed"

        self.manager.install_to_node = install_to_node
        self.manager.get_node_status = mock.AsyncMock(return_value=None)
        self.manager.sync_whitelist_to_node = mock.AsyncMock(return_value=(True, "synced"))
        self.manager._notify = mock.AsyncMock(return_value=True)
        self.whitelist = mock.AsyncMock(return_value=list(WHITELIST))

    async def poll(
        self, server_id: int, reported: dict, release: Optional[WatchdogRelease] = RELEASE,
        node_capabilities: Optional[str] = None,
    ) -> dict:
        server = node(server_id, node_capabilities)
        return await self.manager._maybe_auto_install(server, reported, release, self.whitelist)

    async def poll_many(self, server_ids, reported: dict) -> None:
        await asyncio.gather(*(self.poll(server_id, reported) for server_id in server_ids))

    def rollout(self) -> dict:
        return self.manager.rollout_state()


class CanaryTest(RolloutCase):
    async def test_new_version_first_goes_to_a_handful_of_nodes(self):
        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            await self.poll_many(range(1, 41), status("2.6.0"))

        self.assertEqual(len(self.install_calls), CANARY_INSTALLS)

    async def test_fleet_waits_while_the_version_is_not_confirmed(self):
        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            await self.poll_many(range(1, 41), status("2.6.0"))
            for server_id in self.install_calls[:CANARY_CONFIRMATIONS - 1]:
                await self.poll(server_id, status("2.7.0"))
            await self.poll_many(range(41, 81), status("2.6.0"))

        self.assertEqual(len(self.install_calls), CANARY_INSTALLS)
        self.assertEqual(self.rollout()["confirmed"], CANARY_CONFIRMATIONS - 1)

    async def test_rest_of_the_fleet_follows_once_status_polls_confirm_the_version(self):
        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            await self.poll_many(range(1, 41), status("2.6.0"))
            for server_id in self.install_calls[:CANARY_CONFIRMATIONS]:
                await self.poll(server_id, status("2.7.0"))
            await self.poll_many(range(41, 81), status("2.6.0"))

        self.assertEqual(len(self.install_calls), CANARY_INSTALLS + 40)

    async def test_fresh_status_right_after_the_install_is_not_a_confirmation(self):
        # Скрипт, падающий через минуту, в ответе сразу после установки ещё жив
        self.manager.get_node_status = mock.AsyncMock(return_value=status("2.7.0"))

        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            await self.poll_many(range(1, 41), status("2.6.0"))

        self.assertEqual(self.rollout()["confirmed"], 0)
        self.assertEqual(self.rollout()["pending"], CANARY_INSTALLS)

    async def test_nodes_already_on_the_version_confirm_it_after_a_panel_restart(self):
        await self.poll_many(range(1, CANARY_CONFIRMATIONS + 1), status("2.7.0"))

        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            await self.poll_many(range(10, 40), status(installed=False))

        self.assertEqual(len(self.install_calls), 30)

    async def test_stopped_watchdog_does_not_confirm_the_version(self):
        await self.poll_many(range(1, 10), status("2.7.0", active=False))

        self.assertEqual(self.rollout()["confirmed"], 0)

    async def test_node_that_does_not_name_its_version_is_left_alone(self):
        await self.poll(1, status(""))

        self.assertEqual(self.install_calls, [])

    async def test_nothing_is_installed_without_a_valid_release(self):
        await self.poll(1, status(installed=False), release=None)

        self.assertEqual(self.install_calls, [])
        self.assertIsNone(self.rollout())

    async def test_node_that_closed_the_section_is_not_touched(self):
        await self.poll(1, status(installed=False), node_capabilities=capabilities(exec=ACCESS_WRITE))

        self.assertEqual(self.install_calls, [])

    async def test_restricted_node_without_exec_is_not_touched(self):
        # Скрипт сторожа нода исполняет от root: без exec она ответила бы отказом
        await self.poll(1, status(installed=False), node_capabilities=capabilities(antiddos=ACCESS_WRITE))

        self.assertEqual(self.install_calls, [])

    async def test_node_that_would_refuse_does_not_take_a_canary_slot(self):
        refusing = capabilities(antiddos=ACCESS_WRITE)
        await asyncio.gather(*(
            self.poll(server_id, status("2.6.0"), node_capabilities=refusing) for server_id in range(1, 21)
        ))

        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            await self.poll_many(range(21, 41), status("2.6.0"))

        self.assertEqual(self.install_calls, list(range(21, 21 + CANARY_INSTALLS)))
        self.assertEqual(self.rollout()["failed"], 0)

    async def test_restricted_node_that_grants_exec_is_updated(self):
        granting = capabilities(antiddos=ACCESS_WRITE, exec=ACCESS_WRITE)

        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            await self.poll(1, status(installed=False), node_capabilities=granting)

        self.assertEqual(self.install_calls, [1])

    async def test_installed_node_gets_the_whitelist_and_its_fresh_status(self):
        fresh = status("2.7.0")
        self.manager.get_node_status = mock.AsyncMock(return_value=fresh)

        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            result = await self.poll(1, status("2.6.0"))

        self.assertIs(result, fresh)
        self.assertEqual(self.manager.sync_whitelist_to_node.await_args.args[1], WHITELIST)


class RolloutHaltTest(RolloutCase):
    async def fail_canary(self, count: int) -> None:
        self.failing = {server_id: "watchdog script failed the syntax check" for server_id in range(1, count + 1)}
        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            await self.poll_many(range(1, count + 1), status("2.6.0"))

    async def test_three_failed_installs_stop_the_rollout(self):
        await self.fail_canary(ROLLOUT_FAILURE_LIMIT)

        await self.poll_many(range(10, 40), status("2.6.0"))

        self.assertEqual(len(self.install_calls), ROLLOUT_FAILURE_LIMIT)
        self.assertEqual(
            {key: self.rollout()[key] for key in ("version", "confirmed", "failed", "halted")},
            {"version": "2.7.0", "confirmed": 0, "failed": ROLLOUT_FAILURE_LIMIT, "halted": True},
        )

    async def test_fewer_failures_do_not_stop_it(self):
        await self.fail_canary(ROLLOUT_FAILURE_LIMIT - 1)

        self.assertFalse(self.rollout()["halted"])
        self.manager._notify.assert_not_awaited()

    async def test_small_fleet_where_every_install_fails_stops_on_the_retry(self):
        # Двух нод мало для трёх разных отказов: считаются и повторные попытки
        await self.fail_canary(2)
        self.manager._install_cooldown.clear()

        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            await self.poll(1, status("2.6.0"))

        self.assertEqual((self.rollout()["failed"], self.rollout()["halted"]), (2, True))
        self.manager._notify.assert_awaited_once()

    async def test_one_stubborn_node_does_not_stop_a_big_fleet(self):
        self.manager._rollout_of("2.7.0").confirmed.update(range(1, 1991))
        self.failing = {2000: "Read-only file system"}

        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            for _ in range(10):
                self.manager._install_cooldown.clear()
                await self.poll(2000, status("2.6.0"))

        self.assertEqual((len(self.install_calls), self.rollout()["halted"]), (10, False))

    async def test_halt_is_announced_in_telegram_once(self):
        await self.fail_canary(CANARY_INSTALLS)

        self.manager._notify.assert_awaited_once()
        text = self.manager._notify.await_args.args[0]
        self.assertIn("2.7.0", text)
        self.assertIn("node-1", text)
        self.assertIn("watchdog script failed the syntax check", text)

    async def test_failed_nodes_and_reasons_are_exposed(self):
        await self.fail_canary(ROLLOUT_FAILURE_LIMIT)

        self.assertEqual(self.rollout()["failed_nodes"][0], {
            "server_id": 1, "server_name": "node-1", "message": "watchdog script failed the syntax check",
        })

    async def test_markup_in_a_node_answer_does_not_break_the_alert(self):
        self.failing = {server_id: "bash: unexpected token `<'" for server_id in range(1, 4)}

        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            await self.poll_many(range(1, 4), status("2.6.0"))

        self.assertIn("unexpected token `&lt;", self.manager._notify.await_args.args[0])

    async def test_new_version_starts_a_new_rollout(self):
        await self.fail_canary(ROLLOUT_FAILURE_LIMIT)
        self.failing = {}

        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            await self.poll(1, status("2.6.0"), release=WatchdogRelease(SCRIPT, SERVICE, "2.7.1"))

        self.assertEqual(self.install_calls[-1], 1)
        self.assertEqual(
            {key: self.rollout()[key] for key in ("version", "failed", "halted")},
            {"version": "2.7.1", "failed": 0, "halted": False},
        )

    async def test_failing_node_is_not_retried_on_every_poll(self):
        await self.fail_canary(1)

        await self.poll(1, status("2.6.0"))

        self.assertEqual(self.install_calls, [1])

    async def test_node_that_recovers_is_no_longer_counted_as_failed(self):
        await self.fail_canary(1)

        await self.poll(1, status("2.7.0"))

        self.assertEqual((self.rollout()["failed"], self.rollout()["confirmed"]), (0, 1))

    async def test_node_still_on_the_old_version_after_the_install_is_a_failure(self):
        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            await self.poll(1, status("2.6.0"))

        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            await self.poll(1, status("2.6.0"))

        self.assertEqual(self.rollout()["failed_nodes"][0]["message"], "node reports version 2.6.0 after the install")

    async def test_watchdog_that_died_after_the_install_is_a_failure(self):
        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            await self.poll(1, status("2.6.0"))

        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            await self.poll(1, status("2.7.0", active=False))

        self.assertEqual(self.rollout()["failed"], 1)

    async def test_nodes_that_went_silent_after_the_install_stop_the_rollout(self):
        # Сторож правит iptables: нода, которую он отрезал, об отказе не сообщит
        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            await self.poll_many(range(1, 4), status("2.6.0"))
        patch_module(self, CONFIRM_TIMEOUT=-1)

        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            await self.manager._fail_unconfirmed(self.manager._rollout, [node(1), node(2), node(3)])

        self.assertTrue(self.rollout()["halted"])
        self.manager._notify.assert_awaited_once()

    async def test_removed_server_is_not_a_failure_of_the_version(self):
        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            await self.poll_many(range(1, 4), status("2.6.0"))
        patch_module(self, CONFIRM_TIMEOUT=-1)

        await self.manager._fail_unconfirmed(self.manager._rollout, [])

        self.assertEqual((self.rollout()["failed"], self.rollout()["pending"]), (0, 0))

    async def test_few_failures_in_a_big_fleet_do_not_stop_a_proven_version(self):
        # 2000 нод: несколько с полным диском найдутся всегда
        self.manager._rollout_of("2.7.0").confirmed.update(range(1, 1991))
        self.failing = {server_id: "No space left on device" for server_id in range(2000, 2010)}

        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            await self.poll_many(range(2000, 2010), status("2.6.0"))

        self.assertEqual((self.rollout()["failed"], self.rollout()["halted"]), (10, False))

    async def test_many_failures_stop_a_proven_version_too(self):
        self.manager._rollout_of("2.7.0").confirmed.update(range(1, 101))
        self.failing = {server_id: "iptables: No chain/target/match by that name" for server_id in range(2000, 2100)}

        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            await self.poll_many(range(2000, 2100), status("2.6.0"))

        self.assertTrue(self.rollout()["halted"])
        self.assertLess(len(self.install_calls), 100)


class PollPassTest(unittest.IsolatedAsyncioTestCase):
    """Проход опроса: кого обходит и сколько раз собирает whitelist."""

    async def asyncSetUp(self) -> None:
        self.servers = [node(server_id) for server_id in range(1, 31)]
        self.silent: set[int] = set()
        collector = SimpleNamespace(_answered_last_poll=lambda server_id: server_id not in self.silent)
        patch_module(
            self,
            get_collector=lambda: collector,
            async_session=lambda: FakeSession({}),
        )
        self.manager = AntiDdosManager()
        self.manager._active_servers = mock.AsyncMock(return_value=self.servers)
        self.manager._watchdog_release = mock.AsyncMock(return_value=RELEASE)
        self.manager.install_to_node = mock.AsyncMock(return_value=(True, "installed"))
        self.manager.sync_whitelist_to_node = mock.AsyncMock(return_value=(True, "synced"))
        self.manager._store_status = mock.AsyncMock()
        self.manager.build_whitelist = mock.AsyncMock(side_effect=self._build_whitelist)
        self.reported = status("2.6.0")
        self.manager.get_node_status = mock.AsyncMock(side_effect=lambda server: dict(self.reported))

    async def _build_whitelist(self, db) -> list[str]:
        await asyncio.sleep(0.01)
        return list(WHITELIST)

    def confirm_version_on(self, count: int) -> None:
        self.manager._rollout_of("2.7.0").confirmed.update(range(1000, 1000 + count))

    async def test_whitelist_is_built_once_for_all_installs_of_a_pass(self):
        self.confirm_version_on(CANARY_CONFIRMATIONS)

        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            await self.manager.poll_status_all()

        self.assertEqual(self.manager.install_to_node.await_count, 30)
        self.manager.build_whitelist.assert_awaited_once()
        self.assertEqual(
            [call.args[1] for call in self.manager.sync_whitelist_to_node.await_args_list], [WHITELIST] * 30,
        )

    async def test_whitelist_is_not_built_when_nothing_is_installed(self):
        self.reported = status("2.7.0")

        await self.manager.poll_status_all()

        self.manager.build_whitelist.assert_not_awaited()

    async def test_next_pass_builds_the_whitelist_again(self):
        # Список за минуту между проходами мог измениться
        self.confirm_version_on(CANARY_CONFIRMATIONS)

        with self.assertLogs(antiddos_manager.logger, level="INFO"):
            await self.manager.poll_status_all()
            self.servers[:] = [node(server_id) for server_id in range(101, 131)]
            await self.manager.poll_status_all()

        self.assertEqual(self.manager.build_whitelist.await_count, 2)

    async def test_node_silent_on_the_metrics_poll_is_skipped(self):
        self.reported = status("2.7.0")
        self.silent = {2, 3}

        await self.manager.poll_status_all()

        polled = {call.args[0].id for call in self.manager.get_node_status.await_args_list}
        self.assertEqual(polled, set(range(1, 31)) - {2, 3})

    async def test_status_is_stored_against_the_row_read_at_the_start_of_the_pass(self):
        self.reported = status("2.7.0")

        await self.manager.poll_status_all()

        call = self.manager._store_status.await_args_list[0]
        self.assertIs(call.kwargs["known"], self.servers[0])
        self.assertTrue(call.kwargs["alert"])

    async def test_error_on_one_node_is_logged_and_does_not_stop_the_pass(self):
        self.reported = status("2.7.0")
        self.manager._store_status = mock.AsyncMock(side_effect=[RuntimeError("db gone")] + [None] * 29)

        with self.assertLogs(antiddos_manager.logger, level="WARNING") as logs:
            await self.manager.poll_status_all()

        self.assertEqual(self.manager._store_status.await_count, 30)
        self.assertIn("1 of 30 nodes raised", logs.output[0])


if __name__ == "__main__":
    unittest.main()
