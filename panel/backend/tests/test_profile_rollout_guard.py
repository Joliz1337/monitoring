"""Тесты защиты от повторной раскатки профилей HAProxy и Remnawave nginx.

Голый unittest, без PostgreSQL и без сети: сессия БД и сама раскатка подменены.

Раскатка помечает серверы «pending» и идёт по десять нод. На большом профиле она
длится дольше 30 секунд, и цикл досинхронизации принимал ещё не обработанные
серверы за «ожившие»: слал им конфиг второй раз, с принудительным reload HAProxy.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import haproxy_profile_sync, remnawave_nginx_sync  # noqa: E402
from app.services.rollout_guard import RolloutGuard  # noqa: E402

PROFILE_ID = 7


def pending_server(server_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=server_id, name=f"node-{server_id}", last_seen=datetime.now(timezone.utc),
        active_haproxy_profile_id=PROFILE_ID, active_remnawave_nginx_profile_id=PROFILE_ID,
    )


class FakeSession:
    """Сессия, отдающая заранее заданные «pending»-серверы и профиль."""

    def __init__(self, servers: list[SimpleNamespace]):
        self._servers = servers

    async def execute(self, _query):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self._servers)))

    async def get(self, _model, profile_id: int):
        return SimpleNamespace(id=profile_id, config_content="")


class RolloutGuardTest(unittest.TestCase):
    def test_servers_are_busy_only_while_the_rollout_runs(self):
        guard = RolloutGuard()
        with guard.hold([1, 2]):
            self.assertEqual(guard.idle([1, 2, 3]), [3])
        self.assertEqual(guard.idle([1, 2, 3]), [1, 2, 3])

    def test_failed_rollout_releases_its_servers(self):
        guard = RolloutGuard()
        with self.assertRaises(RuntimeError):
            with guard.hold([1]):
                raise RuntimeError("node api down")
        self.assertEqual(guard.idle([1]), [1])

    def test_finishing_one_rollout_does_not_release_servers_of_another(self):
        guard = RolloutGuard()
        with guard.hold([1, 2]):
            with guard.hold([2, 3]):
                pass
            self.assertEqual(guard.idle([1, 2, 3]), [3])


class RetryDuringRolloutTest(unittest.IsolatedAsyncioTestCase):
    async def _retried_ids(self, module, retry, busy: list[int]) -> list[int]:
        db = FakeSession([pending_server(1), pending_server(2)])
        with mock.patch.object(module, "sync_profile_to_servers", mock.AsyncMock(return_value=[])) as sync:
            with module.rollout_guard.hold(busy):
                await retry(db)
        return [sid for call in sync.await_args_list for sid in call.kwargs["server_ids"]]

    async def test_haproxy_retry_leaves_servers_of_a_running_rollout_alone(self):
        retried = await self._retried_ids(
            haproxy_profile_sync, haproxy_profile_sync.retry_pending_haproxy_syncs, busy=[1],
        )
        self.assertEqual(retried, [2])

    async def test_haproxy_retry_takes_pending_servers_nobody_is_rolling_out(self):
        retried = await self._retried_ids(
            haproxy_profile_sync, haproxy_profile_sync.retry_pending_haproxy_syncs, busy=[],
        )
        self.assertEqual(retried, [1, 2])

    async def test_nginx_retry_leaves_servers_of_a_running_rollout_alone(self):
        retried = await self._retried_ids(
            remnawave_nginx_sync, remnawave_nginx_sync.retry_pending_remnawave_nginx_syncs, busy=[1, 2],
        )
        self.assertEqual(retried, [])


if __name__ == "__main__":
    unittest.main()
