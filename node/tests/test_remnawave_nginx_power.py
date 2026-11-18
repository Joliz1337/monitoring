"""Tests for stopping and starting the Remnawave nginx container.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Контейнер создаёт compose с `restart: always`: после `docker stop` он поднимается
снова при перезапуске Docker или сервера, поэтому остановленному контейнеру нода
меняет политику на `unless-stopped`. Порядок команд важен и проверен на настоящем
Docker: у контейнера в цикле рестартов `docker update` срывается («Cannot update
container»), пока nginx в очередной раз падает, — менять политику до остановки
значило бы не остановить его вовсе.
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402

from app.routers import remnawave as remnawave_router  # noqa: E402
from app.services import remnawave_nginx_manager as manager_module  # noqa: E402
from app.services.remnawave_nginx_manager import (  # noqa: E402
    DockerResult,
    InvalidInstallPathError,
    RemnawaveNginxManager,
)

INSTALL_PATH = "/opt/remnawave"
CRASH_LOG = "nginx: [emerg] bind() to 0.0.0.0:443 failed (98: Address already in use)"
STOP = ("stop", "remnawave-nginx")
SET_POLICY = ("update", "--restart", "unless-stopped", "remnawave-nginx")
UPDATE_RACE = "Error response from daemon: Cannot update container: runc did not terminate successfully"
OLD_CONTAINER, NEW_CONTAINER = "1f3c", "9a7e"


def _fake_docker(*, exists: bool = True, before: str = "exited|0", recreated: bool = False,
                 after: str = "running|0|0", failing: tuple[str, ...] = ()):
    """`before` — статус и рестарты до запуска, `after` — статус, рестарты и код выхода,
    которые видит проверка после него; `recreated` — compose заменил контейнер новым."""
    calls: list[tuple[str, ...]] = []
    state_reads = 0

    async def fake(*args: str, **_kwargs) -> DockerResult:
        nonlocal state_reads
        calls.append(args)
        if args[0] in failing:
            return DockerResult(1, "", UPDATE_RACE if args[0] == "update" else "permission denied")
        if args[0] == "inspect":
            if not exists:
                return DockerResult(1, "", "Error: No such object: remnawave-nginx")
            if "ExitCode" in args[2]:
                return DockerResult(0, after + "\n", "")
            if ".Id" in args[2]:
                state_reads += 1
                if state_reads == 1:
                    return DockerResult(0, f"{OLD_CONTAINER}|{before}\n", "")
                status, restarts, _exit_code = after.split("|")
                container = NEW_CONTAINER if recreated else OLD_CONTAINER
                return DockerResult(0, f"{container}|{status}|{restarts}\n", "")
            return DockerResult(0, "true|nginx:1.28\n", "")
        if args[0] == "logs":
            return DockerResult(0, "", CRASH_LOG)
        return DockerResult(0, "", "")

    return fake, calls


def _actions(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Вызовы, меняющие состояние контейнера (без чтения inspect и logs)."""
    return [c for c in calls if c[0] not in ("inspect", "logs")]


class PowerTestCase(unittest.TestCase):
    def setUp(self):
        self.manager = RemnawaveNginxManager()
        self.compose_up = mock.AsyncMock(return_value=(True, "started"))
        patcher = mock.patch.multiple(
            manager_module, _STARTUP_GRACE_SEC=0.05, _STARTUP_POLL_SEC=0.01,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, coroutine_factory, **docker_state):
        fake, calls = _fake_docker(**docker_state)
        with mock.patch.object(manager_module, "_docker", fake), \
                mock.patch.object(self.manager, "_compose_up", self.compose_up):
            result = asyncio.run(coroutine_factory())
        return result, calls


class StopTests(PowerTestCase):
    def test_container_is_stopped_first_and_only_then_gets_the_new_policy(self):
        (success, message), calls = self._run(self.manager.stop)
        self.assertTrue(success, message)
        self.assertEqual(_actions(calls), [STOP, SET_POLICY])

    def test_missing_container_is_not_an_error(self):
        (success, message), calls = self._run(self.manager.stop, exists=False)
        self.assertTrue(success)
        self.assertIn("не найден", message)
        self.assertEqual(_actions(calls), [])

    def test_failed_docker_stop_is_reported_and_the_policy_is_left_alone(self):
        (success, message), calls = self._run(self.manager.stop, failing=("stop",))
        self.assertFalse(success)
        self.assertIn("permission denied", message)
        self.assertEqual(_actions(calls), [STOP])

    def test_stopped_container_with_the_old_policy_is_a_success_that_says_so(self):
        # Отказ здесь разводил бы панель с нодой: nginx остановлен, а панель
        # считала бы его работающим. Оговорка о перезагрузке уходит в ответ
        with self.assertLogs(manager_module.logger, level="WARNING"):
            (success, message), calls = self._run(self.manager.stop, failing=("update",))
        self.assertTrue(success)
        self.assertIn("после перезагрузки сервера он запустится снова", message)
        self.assertEqual(_actions(calls), [STOP, SET_POLICY])


class StartTests(PowerTestCase):
    def _start(self, **docker_state):
        return self._run(lambda: self.manager.start(INSTALL_PATH), **docker_state)

    def test_stopped_container_is_brought_up_through_compose(self):
        (success, message), calls = self._start(before="exited|0", after="running|0|0")
        self.assertTrue(success, message)
        self.compose_up.assert_awaited_once_with(INSTALL_PATH)
        self.assertEqual(_actions(calls), [])

    def test_restart_counter_of_the_previous_run_does_not_count(self):
        # Счётчик остановленного контейнера остался от прошлого цикла рестартов;
        # запуск его обнуляет
        (success, message), _ = self._start(before="exited|6", after="running|0|0")
        self.assertTrue(success, message)

    def test_container_already_at_work_keeps_its_old_restart_counter(self):
        # Здоровый nginx, когда-то перезапущенный Docker: старые рестарты — не падение
        (success, message), calls = self._start(before="running|4", after="running|4|0")
        self.assertTrue(success, message)
        self.assertEqual(_actions(calls), [])

    def test_working_container_recreated_by_compose_starts_counting_from_zero(self):
        # compose пересоздал работавший контейнер (изменился docker-compose.yml):
        # обнулённый счётчик у нового контейнера — не падение, гасить его нельзя
        (success, message), calls = self._start(before="running|4", recreated=True, after="running|0|0")
        self.assertTrue(success, message)
        self.assertEqual(_actions(calls), [])

    def test_recreated_container_that_keeps_crashing_is_stopped(self):
        (success, message), calls = self._start(before="running|4", recreated=True, after="running|2|0")
        self.assertFalse(success)
        self.assertEqual(_actions(calls), [STOP, SET_POLICY])

    def test_compose_failure_is_reported(self):
        self.compose_up.return_value = (False, "no configuration file provided: not found")
        (success, message), calls = self._start()
        self.assertFalse(success)
        self.assertIn("no configuration file provided", message)
        self.assertEqual(_actions(calls), [])

    def test_nginx_that_dies_on_start_is_stopped_again(self):
        # Порт занят: nginx падает, политика перезапуска гоняла бы его по кругу
        (success, message), calls = self._start(before="exited|0", after="restarting|2|1")
        self.assertFalse(success)
        self.assertIn("остановлен обратно", message)
        self.assertIn(CRASH_LOG, message)
        self.assertEqual(_actions(calls), [STOP, SET_POLICY])

    def test_restart_loop_is_not_mistaken_for_a_running_container(self):
        # Docker числит контейнер в цикле рестартов запущенным
        for before, after in (("restarting|7", "restarting|9|1"), ("running|7", "running|8|0")):
            with self.subTest(before=before, after=after):
                (success, message), calls = self._start(before=before, after=after)
                self.assertFalse(success)
                self.assertIn(CRASH_LOG, message)
                self.assertEqual(_actions(calls), [STOP, SET_POLICY])

    def test_failure_to_stop_the_crashed_container_is_not_hidden(self):
        (success, message), _ = self._start(after="restarting|2|1", failing=("stop",))
        self.assertFalse(success)
        self.assertIn("остановить его не удалось", message)
        self.assertNotIn("остановлен обратно", message)

    def test_unsafe_install_path_is_rejected_before_any_command(self):
        fake, calls = _fake_docker()
        with mock.patch.object(manager_module, "_docker", fake), \
                self.assertRaises(InvalidInstallPathError):
            asyncio.run(self.manager.start("/opt/remnawave; rm -rf /"))
        self.assertEqual(calls, [])


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.manager = mock.Mock()
        patcher = mock.patch.object(
            remnawave_router, "get_remnawave_nginx_manager", return_value=self.manager,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_both_routes_are_registered_as_writes(self):
        methods = {route.path: route.methods for route in remnawave_router.router.routes}
        self.assertEqual(methods["/api/remnawave/nginx/stop"], {"POST"})
        self.assertEqual(methods["/api/remnawave/nginx/start"], {"POST"})

    def test_stop_passes_the_manager_answer_through(self):
        self.manager.stop = mock.AsyncMock(return_value=(False, "permission denied"))
        answer = asyncio.run(remnawave_router.nginx_stop())
        self.assertEqual((answer.success, answer.message), (False, "permission denied"))

    def test_start_passes_the_install_path(self):
        self.manager.start = mock.AsyncMock(return_value=(True, "started"))
        answer = asyncio.run(remnawave_router.nginx_start(path="/srv/remnawave"))
        self.manager.start.assert_awaited_once_with("/srv/remnawave")
        self.assertTrue(answer.success)

    def test_start_with_unsafe_path_is_a_bad_request(self):
        self.manager.start = mock.AsyncMock(side_effect=InvalidInstallPathError("/x;y"))
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(remnawave_router.nginx_start(path="/x;y"))
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
