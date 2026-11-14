"""Тесты команд анти-DDoS на весь парк (app/services/antiddos_manager.py, app/routers/antiddos.py).

«Включить аварийный на всех», установка сторожа на парк и мастер-переключатель
автодетекта отвечали `{"success": true, "started": true}` и на этом всё: итог по
нодам считался и тут же выбрасывался. Команда, не дошедшая до половины парка,
выглядела так же, как выполненная. Теперь ход и итог последней команды лежат в
`last_action` ответа GET /antiddos/status, а не прошедшие ноды можно повторить.

Голый unittest, без PostgreSQL и без сети: ноды подменены.

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

from fastapi import HTTPException  # noqa: E402

from app.models import AntiDdosSettings, Server  # noqa: E402
from app.routers import antiddos as antiddos_router  # noqa: E402
from app.services import antiddos_manager  # noqa: E402
from app.services.antiddos_manager import FAILED_NODES_SHOWN, AntiDdosManager, FleetActionKind  # noqa: E402
from app.services.node_capabilities import ACCESS_NONE, ACCESS_WRITE, Capability  # noqa: E402

NODE_STATUS = {"mode": "on", "source": "manual", "since": 1, "reason": "", "watchdog": "on"}


def node(server_id: int) -> Server:
    return Server(id=server_id, name=f"node-{server_id}", url=f"https://10.0.0.{server_id}:9100")


def capabilities(**granted: str) -> str:
    """Карта NODE_CAPABILITIES, как её шлёт нода: закрыто всё, кроме перечисленного."""
    return json.dumps({**{c.value: ACCESS_NONE for c in Capability}, **granted}, sort_keys=True)


class FleetCase(unittest.IsolatedAsyncioTestCase):
    """Парк, где часть нод отказывает: `failing` — id → причина."""

    FLEET_SIZE = 5

    async def asyncSetUp(self) -> None:
        self.servers = [node(server_id) for server_id in range(1, self.FLEET_SIZE + 1)]
        self.failing: dict[int, str] = {}
        self.calls: list[tuple] = []

        self.manager = AntiDdosManager()
        self.manager._active_servers = mock.AsyncMock(side_effect=lambda: list(self.servers))
        self.manager._store_status = mock.AsyncMock()
        self.manager.set_node_emergency = self._set_emergency
        self.manager.set_node_watchdog = self._set_watchdog
        self.manager.install_to_node = self._install

    def _answer(self, call: tuple, server: Server) -> tuple[bool, str]:
        self.calls.append((*call, server.id))
        if server.id in self.failing:
            return False, self.failing[server.id]
        return True, "ok"

    async def _set_emergency(self, server: Server, enabled: bool) -> tuple[bool, str, Optional[dict]]:
        ok, message = self._answer(("emergency", enabled), server)
        return ok, message, NODE_STATUS if ok else None

    async def _set_watchdog(self, server: Server, enabled: bool) -> tuple[bool, str]:
        return self._answer(("watchdog", enabled), server)

    async def _install(self, server: Server) -> tuple[bool, str]:
        return self._answer(("install",), server)

    async def finished(self) -> dict:
        await asyncio.gather(*self.manager._fleet_tasks)
        return self.manager.last_action()

    async def run_action(self, kind: FleetActionKind, enabled: Optional[bool] = None) -> dict:
        await self.manager.start_fleet_action(kind, enabled)
        return await self.finished()


class LastActionTest(FleetCase):
    async def test_request_gets_the_record_of_the_started_action(self):
        started = await self.manager.start_fleet_action(FleetActionKind.EMERGENCY, True)
        await self.finished()

        self.assertEqual(
            {key: started[key] for key in ("action", "enabled", "total", "ok", "failed", "finished_at")},
            {"action": "emergency", "enabled": True, "total": 5, "ok": 0, "failed": [], "finished_at": None},
        )
        self.assertIsNotNone(started["started_at"])

    async def test_action_applied_everywhere_ends_without_failures(self):
        action = await self.run_action(FleetActionKind.EMERGENCY, True)

        self.assertEqual((action["ok"], action["failed_count"], action["failed"]), (5, 0, []))
        self.assertIsNotNone(action["finished_at"])

    async def test_nodes_that_did_not_take_it_are_listed_with_the_reason(self):
        self.failing = {2: "timeout", 4: "watchdog not installed"}

        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            action = await self.run_action(FleetActionKind.EMERGENCY, True)

        self.assertEqual((action["total"], action["ok"], action["failed_count"]), (5, 3, 2))
        self.assertEqual(action["failed"], [
            {"server_id": 2, "server_name": "node-2", "message": "timeout"},
            {"server_id": 4, "server_name": "node-4", "message": "watchdog not installed"},
        ])

    async def test_failures_are_logged_once_with_counts_and_first_names(self):
        self.failing = {2: "timeout", 4: "watchdog not installed"}

        with self.assertLogs(antiddos_manager.logger, level="WARNING") as logs:
            await self.run_action(FleetActionKind.EMERGENCY, False)

        (line,) = logs.output
        self.assertIn("emergency (enabled=False): 2 of 5 nodes failed", line)
        self.assertIn("node-2 (timeout)", line)

    async def test_clean_run_logs_no_warning(self):
        with self.assertNoLogs(antiddos_manager.logger, level="WARNING"):
            await self.run_action(FleetActionKind.WATCHDOG, True)

    async def test_progress_is_visible_while_the_action_runs(self):
        release = asyncio.Event()

        async def slow_on_third(server: Server, enabled: bool) -> tuple[bool, str]:
            if server.id == 3:
                await release.wait()
            return True, "ok"

        self.manager.set_node_watchdog = slow_on_third
        await self.manager.start_fleet_action(FleetActionKind.WATCHDOG, False)
        await asyncio.sleep(0.01)

        running = self.manager.last_action()
        release.set()

        self.assertEqual((running["ok"], running["finished_at"]), (4, None))
        self.assertEqual((await self.finished())["ok"], 5)

    async def test_node_call_that_raises_is_a_failure_and_the_rest_go_on(self):
        async def raising_on_first(server: Server, enabled: bool) -> tuple[bool, str]:
            if server.id == 1:
                raise RuntimeError("connection reset")
            return True, "ok"

        self.manager.set_node_watchdog = raising_on_first

        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            action = await self.run_action(FleetActionKind.WATCHDOG, True)

        self.assertEqual(action["ok"], 4)
        self.assertEqual(action["failed"], [{"server_id": 1, "server_name": "node-1", "message": "connection reset"}])

    async def test_nodes_that_closed_the_section_are_not_counted(self):
        self.servers[4].node_capabilities = capabilities()

        action = await self.run_action(FleetActionKind.EMERGENCY, True)

        self.assertEqual((action["total"], action["ok"]), (4, 4))

    async def test_install_skips_nodes_that_do_not_grant_exec(self):
        # Скрипт сторожа нода исполняет от root: без exec она ответила бы отказом
        self.servers[3].node_capabilities = capabilities(antiddos=ACCESS_WRITE)
        self.servers[4].node_capabilities = capabilities(antiddos=ACCESS_WRITE, exec=ACCESS_WRITE)

        action = await self.run_action(FleetActionKind.INSTALL)

        self.assertEqual((action["total"], action["ok"]), (4, 4))
        self.assertEqual([call[-1] for call in self.calls], [1, 2, 3, 5])

    async def test_node_without_exec_still_takes_the_other_actions(self):
        self.servers[3].node_capabilities = capabilities(antiddos=ACCESS_WRITE)

        action = await self.run_action(FleetActionKind.EMERGENCY, True)

        self.assertEqual(action["total"], 5)

    async def test_each_action_calls_its_own_node_endpoint(self):
        await self.run_action(FleetActionKind.EMERGENCY, True)
        await self.run_action(FleetActionKind.WATCHDOG, False)
        await self.run_action(FleetActionKind.INSTALL)

        self.assertEqual(
            [self.calls[0][:-1], self.calls[5][:-1], self.calls[10][:-1]],
            [("emergency", True), ("watchdog", False), ("install",)],
        )
        self.assertEqual(self.manager.last_action()["action"], "install")

    async def test_emergency_state_returned_by_the_node_is_stored(self):
        await self.run_action(FleetActionKind.EMERGENCY, True)

        self.assertEqual(self.manager._store_status.await_count, 5)
        self.manager._store_status.assert_any_await(1, NODE_STATUS)


class BigFleetTest(FleetCase):
    FLEET_SIZE = 250

    async def test_list_of_failed_nodes_is_capped_but_the_count_is_exact(self):
        self.failing = {server.id: "timeout" for server in self.servers}

        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            action = await self.run_action(FleetActionKind.INSTALL)

        self.assertEqual((len(action["failed"]), action["failed_count"]), (FAILED_NODES_SHOWN, 250))

    async def test_retry_covers_the_failed_nodes_that_did_not_fit_the_list(self):
        self.failing = {server.id: "timeout" for server in self.servers}
        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            await self.run_action(FleetActionKind.INSTALL)
        self.failing = {}

        await self.manager.retry_failed()

        self.assertEqual((await self.finished())["ok"], 250)


class RetryFailedTest(FleetCase):
    async def fail_on(self, failing: dict[int, str], kind=FleetActionKind.EMERGENCY, enabled: Optional[bool] = True):
        self.failing = failing
        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            await self.run_action(kind, enabled)
        self.calls.clear()

    async def test_only_the_failed_nodes_get_the_action_again(self):
        await self.fail_on({2: "timeout", 4: "timeout"})
        self.failing = {}

        started = await self.manager.retry_failed()
        action = await self.finished()

        self.assertEqual(self.calls, [("emergency", True, 2), ("emergency", True, 4)])
        self.assertEqual((started["total"], action["ok"], action["failed"]), (2, 2, []))

    async def test_retry_repeats_the_same_action_with_the_same_value(self):
        await self.fail_on({3: "timeout"}, FleetActionKind.WATCHDOG, False)

        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            await self.manager.retry_failed()
            action = await self.finished()

        self.assertEqual(self.calls, [("watchdog", False, 3)])
        self.assertEqual((action["action"], action["enabled"], action["failed_count"]), ("watchdog", False, 1))

    async def test_there_is_nothing_to_retry_before_any_action(self):
        self.assertIsNone(await self.manager.retry_failed())

    async def test_there_is_nothing_to_retry_after_a_clean_run(self):
        await self.run_action(FleetActionKind.EMERGENCY, True)

        self.assertIsNone(await self.manager.retry_failed())

    async def test_running_action_cannot_be_retried(self):
        release = asyncio.Event()

        async def stuck(server: Server, enabled: bool) -> tuple[bool, str]:
            await release.wait()
            return False, "timeout"

        self.manager.set_node_watchdog = stuck
        await self.manager.start_fleet_action(FleetActionKind.WATCHDOG, True)
        await asyncio.sleep(0)

        retried = await self.manager.retry_failed()
        release.set()
        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            await self.finished()

        self.assertIsNone(retried)

    async def test_server_removed_since_the_action_is_not_retried(self):
        await self.fail_on({2: "timeout", 4: "timeout"})
        self.failing = {}
        self.servers = [server for server in self.servers if server.id != 4]

        await self.manager.retry_failed()
        await self.finished()

        self.assertEqual(self.calls, [("emergency", True, 2)])


class FakeDb:
    def __init__(self, servers: list[Server]) -> None:
        self._servers = servers

    async def execute(self, statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self._servers)))

    async def commit(self) -> None:
        return None

    async def refresh(self, entity) -> None:
        return None


class RouterTest(FleetCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        patcher = mock.patch.object(antiddos_router, "get_antiddos_manager", lambda: self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_emergency_all_answers_with_the_started_action(self):
        answer = await antiddos_router.emergency_all(antiddos_router.EmergencyAllRequest(enabled=True), {})
        await self.finished()

        self.assertEqual((answer["success"], answer["started"]), (True, True))
        self.assertEqual((answer["action"]["action"], answer["action"]["enabled"], answer["action"]["total"]),
                         ("emergency", True, 5))

    async def test_install_all_answers_with_the_started_action(self):
        answer = await antiddos_router.install_all({})
        await self.finished()

        self.assertEqual((answer["action"]["action"], answer["action"]["enabled"]), ("install", None))

    async def test_status_carries_the_last_action_and_the_rollout(self):
        self.failing = {2: "timeout"}
        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            await self.run_action(FleetActionKind.EMERGENCY, True)
        self.manager._rollout_of("2.7.0").confirmed.update({1, 3})

        answer = await antiddos_router.get_status(FakeDb(self.servers), {})

        self.assertEqual(answer["total"], 5)
        self.assertEqual(answer["last_action"]["failed"], [
            {"server_id": 2, "server_name": "node-2", "message": "timeout"},
        ])
        self.assertEqual(
            {key: answer["rollout"][key] for key in ("version", "confirmed", "failed", "halted")},
            {"version": "2.7.0", "confirmed": 2, "failed": 0, "halted": False},
        )

    async def test_status_before_any_action_has_empty_slots(self):
        answer = await antiddos_router.get_status(FakeDb(self.servers), {})

        self.assertEqual((answer["last_action"], answer["rollout"]), (None, None))

    async def test_retry_endpoint_restarts_the_failed_nodes(self):
        self.failing = {2: "timeout"}
        with self.assertLogs(antiddos_manager.logger, level="WARNING"):
            await self.run_action(FleetActionKind.EMERGENCY, True)
        self.failing = {}

        answer = await antiddos_router.retry_failed({})
        action = await self.finished()

        self.assertEqual((answer["started"], answer["action"]["total"]), (True, 1))
        self.assertEqual((action["ok"], action["failed_count"]), (1, 0))

    async def test_retry_endpoint_refuses_when_there_is_nothing_to_retry(self):
        with self.assertRaises(HTTPException) as raised:
            await antiddos_router.retry_failed({})

        self.assertEqual(raised.exception.status_code, 409)

    async def update_settings(self, **payload) -> None:
        settings = AntiDdosSettings(
            enabled=True, whitelist_push_interval=3600, status_poll_interval=60, watchdog_default_enabled=True,
        )
        self.manager.get_or_create_settings = mock.AsyncMock(return_value=settings)
        await antiddos_router.update_settings(antiddos_router.UpdateSettings(**payload), FakeDb([]), {})

    async def test_master_switch_runs_as_a_tracked_watchdog_action(self):
        await self.update_settings(enabled=False)
        action = await self.finished()

        self.assertEqual((action["action"], action["enabled"], action["ok"]), ("watchdog", False, 5))
        self.assertEqual(self.calls[0], ("watchdog", False, 1))

    async def test_other_settings_do_not_touch_the_fleet(self):
        await self.update_settings(status_poll_interval=30)

        self.assertIsNone(self.manager.last_action())


if __name__ == "__main__":
    unittest.main()
