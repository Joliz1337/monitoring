"""«Обновить всё»: панель обновляется первой, ноды — после неё.

Запустив своё обновление, бэкенд уходит в перезапуск, поэтому список нод лежит
в базе, а рассылку делает бэкенд, поднявшийся после обновления. Проверяется,
что каждая нода получает команду, что отказ одной не срывает остальных, что
перезапуск посреди рассылки её не теряет и что сорвавшееся без перезапуска
обновление панели не оставляет ноды ждать вечно.
"""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from fastapi import HTTPException  # noqa: E402

    from app.routers import system as system_router  # noqa: E402
    from app.services import node_rollout  # noqa: E402
except ImportError as e:  # рантайм панели не установлен
    raise unittest.SkipTest(f"node rollout requires the panel runtime: {e}")


class PendingStore:
    """Строка panel_settings в памяти."""

    def __init__(self):
        self.rollout = None
        self.writes = []

    async def read(self):
        return self.rollout

    async def write(self, rollout):
        self.rollout = rollout
        self.writes.append(rollout)


class RolloutTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = PendingStore()
        for patcher in (
            mock.patch.object(node_rollout, "_read_pending", self.store.read),
            mock.patch.object(node_rollout, "_write_pending", self.store.write),
            mock.patch.object(node_rollout, "ROLLOUT_BATCH", 2),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.launched = []

    async def launch(self, server_id, target_ref):
        self.launched.append((server_id, target_ref))


class RunPendingTests(RolloutTestCase):
    async def test_every_scheduled_node_is_asked_to_update_once(self):
        self.assertEqual(await node_rollout.schedule([3, 1, 2, 1], "dev"), 3)
        self.assertEqual(await node_rollout.pending_count(), 3)

        await node_rollout.run_pending(self.launch)

        self.assertEqual(self.launched, [(3, "dev"), (1, "dev"), (2, "dev")])
        self.assertEqual(await node_rollout.pending_count(), 0)

    async def test_refusal_of_one_node_does_not_stop_the_rest(self):
        await node_rollout.schedule([1, 2, 3], "main")

        async def launch(server_id, target_ref):
            if server_id == 1:
                raise HTTPException(status_code=502)
            await self.launch(server_id, target_ref)

        await node_rollout.run_pending(launch)

        self.assertEqual(self.launched, [(2, "main"), (3, "main")])
        self.assertIsNone(self.store.rollout)

    async def test_restart_in_the_middle_keeps_the_rest_for_the_next_start(self):
        await node_rollout.schedule([1, 2, 3, 4, 5], "dev")

        async def launch_until_restart(server_id, target_ref):
            if server_id == 3:
                raise asyncio.CancelledError()  # бэкенд остановили на второй пачке
            await self.launch(server_id, target_ref)

        with self.assertRaises(asyncio.CancelledError):
            await node_rollout.run_pending(launch_until_restart)
        self.assertEqual(self.store.rollout.server_ids, (3, 4, 5))

        await node_rollout.run_pending(self.launch)

        self.assertEqual([server_id for server_id, _ in self.launched], [1, 2, 4, 3, 4, 5])
        self.assertIsNone(self.store.rollout)

    async def test_nothing_scheduled_means_nothing_happens(self):
        await node_rollout.run_pending(self.launch)
        self.assertEqual(self.launched, [])
        self.assertEqual(self.store.writes, [])

    async def test_empty_list_erases_the_rest_of_a_previous_rollout(self):
        await node_rollout.schedule([1, 2], "dev")
        self.assertEqual(await node_rollout.schedule([], "dev"), 0)
        await node_rollout.run_pending(self.launch)
        self.assertEqual(self.launched, [])

    async def test_second_run_during_a_rollout_does_not_double_it(self):
        await node_rollout.schedule([1, 2, 3], "dev")
        gate = asyncio.Event()

        async def slow_launch(server_id, target_ref):
            await gate.wait()
            await self.launch(server_id, target_ref)

        first = asyncio.create_task(node_rollout.run_pending(slow_launch))
        await asyncio.sleep(0)
        await node_rollout.run_pending(slow_launch)
        gate.set()
        await first

        self.assertEqual(sorted(server_id for server_id, _ in self.launched), [1, 2, 3])


class StartTests(RolloutTestCase):
    async def asyncSetUp(self):
        self.addAsyncCleanup(node_rollout.stop)

    async def test_backend_start_picks_up_the_list(self):
        await node_rollout.schedule([7], "dev")
        node_rollout.start(self.launch, delay=0)
        await node_rollout._task
        self.assertEqual(self.launched, [(7, "dev")])

    async def test_stop_before_the_delay_leaves_the_list_in_place(self):
        await node_rollout.schedule([7], "dev")
        node_rollout.start(self.launch, delay=60)
        await node_rollout.stop()
        self.assertEqual(self.launched, [])
        self.assertEqual(await node_rollout.pending_count(), 1)


class UpdateEverythingEndpointTests(RolloutTestCase):
    def setUp(self):
        super().setUp()
        self.panel_updates = []

        async def panel_update(target_ref=None):
            self.panel_updates.append(target_ref)

        for patcher in (
            mock.patch.object(system_router, "run_panel_update_in_container", panel_update),
            mock.patch.object(system_router.update_channel, "current_branch", return_value="dev"),
            mock.patch.dict(system_router._update_status, {"in_progress": False}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def call(self, server_ids):
        response = await system_router.update_everything(
            system_router.UpdateEverythingRequest(server_ids=server_ids), _={},
        )
        await asyncio.sleep(0)  # дать стартовать фоновой задаче обновления панели
        return response

    async def test_nodes_are_remembered_and_only_the_panel_starts_updating(self):
        response = await self.call([4, 5])

        self.assertEqual((response["target"], response["nodes"]), ("dev", 2))
        self.assertEqual(self.panel_updates, [None])
        self.assertEqual(self.store.rollout, node_rollout.PendingRollout("dev", (4, 5)))
        self.assertEqual(self.launched, [])

    async def test_running_panel_update_refuses_a_second_one(self):
        system_router._update_status["in_progress"] = True
        with self.assertRaises(HTTPException) as refused:
            await self.call([4])
        self.assertEqual(refused.exception.status_code, 409)
        self.assertIsNone(self.store.rollout)

    async def test_unfinished_rollout_is_not_overwritten(self):
        await node_rollout.schedule([1, 2, 3], "dev")
        with self.assertRaises(HTTPException) as refused:
            await self.call([9])
        self.assertEqual(refused.exception.status_code, 409)
        self.assertEqual(self.store.rollout.server_ids, (1, 2, 3))
        self.assertEqual(self.panel_updates, [])


class PanelUpdateFailureTests(RolloutTestCase):
    """Обновление панели сорвалось, не дойдя до перезапуска: тот же процесс
    рассылает обновление нодам сам."""

    async def asyncSetUp(self):
        self.addAsyncCleanup(node_rollout.stop)

    async def run_panel_update(self, exit_code):
        client = mock.MagicMock()
        client.containers.run.return_value.wait.return_value = {"StatusCode": exit_code}
        client.containers.run.return_value.logs.return_value = b"[ERROR] Failed to clone repository"
        with mock.patch.object(system_router, "get_docker_client", return_value=client), \
                mock.patch.object(system_router.upstream_commit, "resolve", mock.AsyncMock(return_value=None)), \
                mock.patch.object(system_router, "launch_node_update", self.launch):
            await system_router.run_panel_update_in_container("dev")
            if node_rollout._task is not None:
                await node_rollout._task

    async def test_failed_panel_update_still_updates_the_nodes(self):
        await node_rollout.schedule([1, 2], "dev")
        await self.run_panel_update(exit_code=1)
        self.assertEqual(self.launched, [(1, "dev"), (2, "dev")])

    async def test_launcher_skips_a_server_removed_meanwhile(self):
        session = mock.MagicMock()
        session.__aenter__ = mock.AsyncMock(return_value=session)
        session.__aexit__ = mock.AsyncMock(return_value=False)
        result = mock.MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = mock.AsyncMock(return_value=result)
        started = mock.AsyncMock()
        with mock.patch.object(system_router, "async_session", return_value=session), \
                mock.patch.object(system_router, "start_node_update", started):
            await system_router.launch_node_update(404, "dev")
        started.assert_not_awaited()

    async def test_launcher_updates_a_live_server_the_same_way_the_button_does(self):
        server = SimpleNamespace(id=5, is_active=True)
        session = mock.MagicMock()
        session.__aenter__ = mock.AsyncMock(return_value=session)
        session.__aexit__ = mock.AsyncMock(return_value=False)
        session.commit = mock.AsyncMock()
        result = mock.MagicMock()
        result.scalar_one_or_none.return_value = server
        session.execute = mock.AsyncMock(return_value=result)
        started = mock.AsyncMock()
        with mock.patch.object(system_router, "async_session", return_value=session), \
                mock.patch.object(system_router, "start_node_update", started):
            await system_router.launch_node_update(5, "dev")
        started.assert_awaited_once_with(session, server, {"target_version": "dev"})


if __name__ == "__main__":
    unittest.main()
