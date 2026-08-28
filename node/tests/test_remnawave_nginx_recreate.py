"""Tests for the post-recreate startup check in the Remnawave nginx manager.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

`nginx -t` кандидата не биндит порты: занятый порт валит nginx уже в
работающем контейнере, restart: always тут же поднимает его снова, и exec
попадает в окно между смертью и рестартом без единой строки причины.
Проверяем, что нода дожидается переживших старт контейнеров, а упавшие
распознаёт по статусу или счётчику рестартов и забирает их лог в отчёт.
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import remnawave_nginx_manager as manager_module  # noqa: E402
from app.services.remnawave_nginx_manager import DockerResult, RemnawaveNginxManager  # noqa: E402

CRASH_LOG = "nginx: [emerg] bind() to 0.0.0.0:443 failed (98: Address already in use)"


def _fake_docker(inspect_states: list[str]):
    """`_docker`, отдающий состояния контейнера по очереди (последнее — навсегда)."""
    calls: list[tuple[str, ...]] = []

    async def fake(*args: str, **_kwargs) -> DockerResult:
        calls.append(args)
        if args[0] == "inspect":
            state = inspect_states.pop(0) if len(inspect_states) > 1 else inspect_states[0]
            return DockerResult(0, state + "\n", "")
        if args[0] == "logs":
            return DockerResult(0, "", CRASH_LOG)
        raise AssertionError(f"неожиданный вызов docker {args}")

    return fake, calls


class ContainerSurvivedStartTests(unittest.TestCase):
    def setUp(self):
        self.manager = RemnawaveNginxManager()
        patcher = mock.patch.multiple(
            manager_module, _STARTUP_GRACE_SEC=0.05, _STARTUP_POLL_SEC=0.01,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, inspect_states: list[str]):
        fake, calls = _fake_docker(inspect_states)
        with mock.patch.object(manager_module, "_docker", fake):
            result = asyncio.run(self.manager._container_survived_start())
        return result, calls

    def test_running_container_without_restarts_survives(self):
        (survived, report), calls = self._run(["running|0|0"])
        self.assertTrue(survived)
        self.assertEqual(report, "")
        self.assertGreater(len(calls), 1, "статус должен опрашиваться несколько раз")
        self.assertFalse(any(c[0] == "logs" for c in calls))

    def test_restart_loop_is_a_failure_even_while_running(self):
        # restart: always успел поднять контейнер заново — виден только счётчик
        (survived, report), _ = self._run(["running|1|1"])
        self.assertFalse(survived)
        self.assertIn("перезапусков 1", report)
        self.assertIn(CRASH_LOG, report)

    def test_container_that_died_during_grace_reports_logs(self):
        (survived, report), calls = self._run(["running|0|0", "exited|0|1"])
        self.assertFalse(survived)
        self.assertIn("статус exited", report)
        self.assertIn("код выхода 1", report)
        self.assertIn(CRASH_LOG, report)
        self.assertEqual(calls[-1][:2], ("logs", "--tail"))

    def test_missing_container_is_a_failure(self):
        async def fake(*args: str, **_kwargs) -> DockerResult:
            return DockerResult(1, "", "Error: No such object: remnawave-nginx")

        with mock.patch.object(manager_module, "_docker", fake):
            survived, report = asyncio.run(self.manager._container_survived_start())
        self.assertFalse(survived)
        self.assertIn("не найден", report)


if __name__ == "__main__":
    unittest.main()
