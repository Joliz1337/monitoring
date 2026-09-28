"""Тесты рассылки белого списка блок-листа (app/services/blocklist_manager.py).

Без PostgreSQL и без сети: сборка списка и сама рассылка подменяются, проверяется
решение «рассылать или нет» — хэш уже разосланного, снимки долгих синков
блок-листа, потолок времени на ноду.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services import blocklist_manager as bm  # noqa: E402
except ImportError as e:  # рантайм панели (sqlalchemy, asyncpg) не установлен
    raise unittest.SkipTest(f"blocklist_manager requires the panel runtime: {e}")


LIST_A = {"in": ["1.1.1.1", "2.2.2.2"], "out": ["1.1.1.1"]}
LIST_B = {"in": ["1.1.1.1", "3.3.3.3"], "out": ["1.1.1.1"]}


class AllowDigestTest(unittest.TestCase):
    def test_same_lists_same_digest(self):
        reordered = {"out": list(LIST_A["out"]), "in": list(LIST_A["in"])}
        self.assertEqual(bm.BlocklistManager.allow_digest(LIST_A), bm.BlocklistManager.allow_digest(reordered))

    def test_new_node_ip_changes_digest(self):
        self.assertNotEqual(bm.BlocklistManager.allow_digest(LIST_A), bm.BlocklistManager.allow_digest(LIST_B))


class PushIfChangedTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = bm.BlocklistManager()
        self.current = LIST_A
        self.pushed: list[dict] = []

        async def build():
            return self.current

        async def push(allow):
            self.pushed.append(allow)

        self.manager.build_allow_lists = build
        self.manager._push_allowlist = push

    async def test_first_check_pushes(self):
        # После рестарта панель не знает, что лежит на нодах — рассылает
        await self.manager.push_allowlist_if_changed()
        self.assertEqual(self.pushed, [LIST_A])

    async def test_unchanged_list_is_not_pushed_again(self):
        await self.manager.push_allowlist_if_changed()
        await self.manager.push_allowlist_if_changed()
        self.assertEqual(len(self.pushed), 1)

    async def test_changed_list_is_pushed(self):
        await self.manager.push_allowlist_if_changed()
        self.current = LIST_B
        await self.manager.push_allowlist_if_changed()
        self.assertEqual(self.pushed, [LIST_A, LIST_B])

    async def test_stale_snapshot_during_push_forces_repush(self):
        # Долгий синк блок-листа закончился посреди рассылки со старым
        # снимком — часть нод получила его поверх свежего списка
        stale = bm.BlocklistManager.allow_digest(LIST_B)

        async def push(allow):
            self.pushed.append(allow)
            self.manager._note_allow_sent(stale)

        self.manager._push_allowlist = push
        await self.manager.push_allowlist_if_changed()
        await self.manager.push_allowlist_if_changed()
        self.assertEqual(self.pushed, [LIST_A, LIST_A])


class AllowlistLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_burst_of_requests_gives_one_check(self):
        # Массовое удаление серверов шлёт запрос на каждый — рассылка одна
        manager = bm.BlocklistManager()
        manager._running = True
        checks = []

        async def check():
            checks.append(1)

        manager.push_allowlist_if_changed = check
        with mock.patch.multiple(
            bm, ALLOWLIST_START_DELAY=0, ALLOWLIST_PUSH_DEBOUNCE=0.05, ALLOWLIST_CHECK_INTERVAL=60
        ):
            task = asyncio.create_task(manager._allowlist_loop())
            for _ in range(5):
                manager.request_allowlist_push()
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(checks), 1)


class NoteAllowSentTest(unittest.TestCase):
    def setUp(self):
        self.manager = bm.BlocklistManager()
        self.manager._allowlist_pushed_hash = bm.BlocklistManager.allow_digest(LIST_A)

    def test_matching_snapshot_changes_nothing(self):
        self.manager._note_allow_sent(bm.BlocklistManager.allow_digest(LIST_A))
        self.assertFalse(self.manager._allowlist_requested.is_set())
        self.assertIsNotNone(self.manager._allowlist_pushed_hash)

    def test_different_snapshot_requests_repush(self):
        self.manager._note_allow_sent(bm.BlocklistManager.allow_digest(LIST_B))
        self.assertTrue(self.manager._allowlist_requested.is_set())
        self.assertIsNone(self.manager._allowlist_pushed_hash)

    def test_snapshot_equal_to_push_in_flight_changes_nothing(self):
        # Новая нода: её синк и рассылка по парку несут один и тот же список
        self.manager._allowlist_inflight_hash = bm.BlocklistManager.allow_digest(LIST_B)
        self.manager._note_allow_sent(bm.BlocklistManager.allow_digest(LIST_B))
        self.assertFalse(self.manager._allowlist_requested.is_set())


class PushOneTest(unittest.IsolatedAsyncioTestCase):
    async def test_hung_node_is_reported_failed(self):
        # Повисшее SOCKS-рукопожатие не соблюдает таймауты httpx —
        # без своего потолка рассылка ждала бы эту ноду вечно
        manager = bm.BlocklistManager()

        async def hang(server, allow):
            await asyncio.sleep(10)
            return True

        manager._send_allow = hang
        server = SimpleNamespace(id=7, name="stuck")
        with mock.patch.object(bm, "ALLOW_PUSH_BUDGET", 0.01):
            self.assertEqual(await manager._push_allow_one(server, LIST_A), (7, False))

    async def test_failed_direction_stops_node(self):
        manager = bm.BlocklistManager()
        calls = []

        async def sync_allow(server, ips, direction):
            calls.append(direction)
            return False, "HTTP 500", {}

        manager.sync_allow_to_node = sync_allow
        server = SimpleNamespace(id=3, name="broken")
        self.assertEqual(await manager._push_allow_one(server, LIST_A), (3, False))
        self.assertEqual(calls, ["in"])


if __name__ == "__main__":
    unittest.main()
