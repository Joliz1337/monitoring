"""Тесты рассылки белого списка и закрытого ping (app/services/blocklist_manager.py).

Без PostgreSQL и без сети: сборка политики и сама рассылка подменяются, проверяется
решение «рассылать или нет» — хэш уже разосланного, снимки долгих синков
блок-листа, потолок времени на ноду, порядок и разбор ответов ноды.

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
    from app.services.ping_block import (  # noqa: E402
        MIN_NODE_VERSION_PING_BLOCK,
        PingBlockMode,
        PingBlockScope,
        node_supports_ping_block,
    )
except ImportError as e:  # рантайм панели (sqlalchemy, asyncpg) не установлен
    raise unittest.SkipTest(f"blocklist_manager requires the panel runtime: {e}")


IPS_A = {"in": ["1.1.1.1", "2.2.2.2"], "out": ["1.1.1.1"]}
IPS_B = {"in": ["1.1.1.1", "3.3.3.3"], "out": ["1.1.1.1"]}
SCOPE_OFF = PingBlockScope()
SCOPE_ALL = PingBlockScope(mode=PingBlockMode.ALL)


def policy(ips: dict, scope: PingBlockScope = SCOPE_OFF, blocked: frozenset = frozenset()) -> "bm.AllowPolicy":
    return bm.AllowPolicy(ips=ips, ping_scope=scope, ping_blocked_ids=blocked)


POLICY_A = policy(IPS_A)
POLICY_B = policy(IPS_B)
POLICY_A_NO_PING = policy(IPS_A, SCOPE_ALL, frozenset({3}))


def server(server_id: int, folder=None) -> SimpleNamespace:
    return SimpleNamespace(id=server_id, name=f"node-{server_id}", folder=folder, url="https://node:9100")


class AllowDigestTest(unittest.TestCase):
    def test_same_policy_same_digest(self):
        reordered = policy({"out": list(IPS_A["out"]), "in": list(IPS_A["in"])})
        self.assertEqual(POLICY_A.digest(), reordered.digest())

    def test_new_node_ip_changes_digest(self):
        self.assertNotEqual(POLICY_A.digest(), POLICY_B.digest())

    def test_ping_toggle_changes_digest(self):
        # Иначе переключатель в панели не дошёл бы до нод: список-то тот же
        self.assertNotEqual(POLICY_A.digest(), POLICY_A_NO_PING.digest())

    def test_server_moved_into_selected_folder_changes_digest(self):
        # Настройка та же, а ноде, попавшей в выбранную папку, ping закрыть надо
        scope = PingBlockScope(mode=PingBlockMode.SELECTED, folders=frozenset({"EU"}))
        before = policy(IPS_A, scope, frozenset({1}))
        after = policy(IPS_A, scope, frozenset({1, 2}))
        self.assertNotEqual(before.digest(), after.digest())


class PingBlockScopeTest(unittest.TestCase):
    def test_off_covers_nobody(self):
        self.assertFalse(SCOPE_OFF.covers(1, "EU"))

    def test_all_covers_everybody(self):
        self.assertTrue(SCOPE_ALL.covers(1, None))

    def test_selected_folder_covers_its_servers(self):
        scope = PingBlockScope(mode=PingBlockMode.SELECTED, folders=frozenset({"EU"}))
        self.assertTrue(scope.covers(1, "EU"))
        self.assertFalse(scope.covers(1, "US"))
        self.assertFalse(scope.covers(1, None))

    def test_selected_server_covered_in_any_folder(self):
        scope = PingBlockScope(mode=PingBlockMode.SELECTED, server_ids=frozenset({7}))
        self.assertTrue(scope.covers(7, "US"))
        self.assertTrue(scope.covers(7, None))
        self.assertFalse(scope.covers(8, None))

    def test_off_keeps_selection_but_ignores_it(self):
        # Выключили и включили обратно — выбор папок не пропадает
        scope = PingBlockScope(mode=PingBlockMode.OFF, folders=frozenset({"EU"}))
        self.assertFalse(scope.covers(1, "EU"))

    def test_roundtrip(self):
        scope = PingBlockScope(
            mode=PingBlockMode.SELECTED, folders=frozenset({"EU", "Asia"}), server_ids=frozenset({3, 1})
        )
        self.assertEqual(scope.to_dict(), {"mode": "selected", "folders": ["Asia", "EU"], "server_ids": [1, 3]})
        self.assertEqual(PingBlockScope.from_dict(scope.to_dict()), scope)

    def test_blocks_ping_uses_server_folder(self):
        scope = PingBlockScope(mode=PingBlockMode.SELECTED, folders=frozenset({"EU"}))
        p = policy(IPS_A, scope)
        self.assertTrue(p.blocks_ping(server(1, "EU")))
        self.assertFalse(p.blocks_ping(server(2, "US")))


class NodeVersionGateTest(unittest.TestCase):
    def test_min_version_supported(self):
        self.assertTrue(node_supports_ping_block(MIN_NODE_VERSION_PING_BLOCK))
        self.assertTrue(node_supports_ping_block("11.0.0"))

    def test_older_or_unknown_not_supported(self):
        self.assertFalse(node_supports_ping_block("10.30.9"))
        self.assertFalse(node_supports_ping_block(None))
        self.assertFalse(node_supports_ping_block(""))


class PushIfChangedTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = bm.BlocklistManager()
        self.current = POLICY_A
        self.pushed: list[bm.AllowPolicy] = []

        async def build():
            return self.current

        async def push(policy):
            self.pushed.append(policy)

        self.manager.build_allow_policy = build
        self.manager._push_allowlist = push

    async def test_first_check_pushes(self):
        # После рестарта панель не знает, что лежит на нодах — рассылает
        await self.manager.push_allowlist_if_changed()
        self.assertEqual(self.pushed, [POLICY_A])

    async def test_unchanged_policy_is_not_pushed_again(self):
        await self.manager.push_allowlist_if_changed()
        await self.manager.push_allowlist_if_changed()
        self.assertEqual(len(self.pushed), 1)

    async def test_changed_list_is_pushed(self):
        await self.manager.push_allowlist_if_changed()
        self.current = POLICY_B
        await self.manager.push_allowlist_if_changed()
        self.assertEqual(self.pushed, [POLICY_A, POLICY_B])

    async def test_ping_toggle_is_pushed(self):
        await self.manager.push_allowlist_if_changed()
        self.current = POLICY_A_NO_PING
        await self.manager.push_allowlist_if_changed()
        self.assertEqual(self.pushed, [POLICY_A, POLICY_A_NO_PING])

    async def test_stale_snapshot_during_push_forces_repush(self):
        # Долгий синк блок-листа закончился посреди рассылки со старым
        # снимком — часть нод получила его поверх свежего списка
        stale = POLICY_B.digest()

        async def push(policy):
            self.pushed.append(policy)
            self.manager._note_allow_sent(stale)

        self.manager._push_allowlist = push
        await self.manager.push_allowlist_if_changed()
        await self.manager.push_allowlist_if_changed()
        self.assertEqual(self.pushed, [POLICY_A, POLICY_A])


class AllowlistLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_burst_of_requests_gives_one_check(self):
        # Массовое удаление серверов шлёт запрос на каждый — рассылка одна
        manager = bm.BlocklistManager()
        manager._running = True
        checks = []

        async def check():
            checks.append(1)

        manager.push_allowlist_if_changed = check
        # Окно ожидания с большим запасом над разбросом запросов — иначе на
        # нагруженной машине запрос выпадал бы за окно и тест плавал
        with mock.patch.multiple(
            bm, ALLOWLIST_START_DELAY=0, ALLOWLIST_PUSH_DEBOUNCE=0.5, ALLOWLIST_CHECK_INTERVAL=60
        ):
            task = asyncio.create_task(manager._allowlist_loop())
            for _ in range(5):
                manager.request_allowlist_push()
                await asyncio.sleep(0.01)
            await asyncio.sleep(1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(checks), 1)


class NoteAllowSentTest(unittest.TestCase):
    def setUp(self):
        self.manager = bm.BlocklistManager()
        self.manager._allowlist_pushed_hash = POLICY_A.digest()

    def test_matching_snapshot_changes_nothing(self):
        self.manager._note_allow_sent(POLICY_A.digest())
        self.assertFalse(self.manager._allowlist_requested.is_set())
        self.assertIsNotNone(self.manager._allowlist_pushed_hash)

    def test_different_snapshot_requests_repush(self):
        self.manager._note_allow_sent(POLICY_B.digest())
        self.assertTrue(self.manager._allowlist_requested.is_set())
        self.assertIsNone(self.manager._allowlist_pushed_hash)

    def test_snapshot_equal_to_push_in_flight_changes_nothing(self):
        # Новая нода: её синк и рассылка по парку несут одну и ту же политику
        self.manager._allowlist_inflight_hash = POLICY_B.digest()
        self.manager._note_allow_sent(POLICY_B.digest())
        self.assertFalse(self.manager._allowlist_requested.is_set())


class PushOneTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = bm.BlocklistManager()
        self.calls: list[str] = []
        self.ping_result = (True, "Ping blocked")

        async def sync_allow(server, ips, direction):
            self.calls.append(f"allow-{direction}")
            return True, "Synced", {}

        async def sync_ping(server, block):
            self.calls.append(f"ping-{block}")
            return self.ping_result

        self.manager.sync_allow_to_node = sync_allow
        self.manager.sync_ping_to_node = sync_ping
        self.server = server(3)

    async def test_ping_goes_after_allowlist(self):
        # Правило ping исключает белый список — сначала он должен лечь на ноду
        self.assertEqual(await self.manager._push_allow_one(self.server, POLICY_A_NO_PING), (3, True))
        self.assertEqual(self.calls, ["allow-in", "allow-out", "ping-True"])

    async def test_ping_failure_marks_node_failed(self):
        self.ping_result = (False, "HTTP 500")
        self.assertEqual(await self.manager._push_allow_one(self.server, POLICY_A), (3, False))

    async def test_failed_direction_stops_node(self):
        async def sync_allow(server, ips, direction):
            self.calls.append(f"allow-{direction}")
            return False, "HTTP 500", {}

        self.manager.sync_allow_to_node = sync_allow
        self.assertEqual(await self.manager._push_allow_one(self.server, POLICY_A), (3, False))
        self.assertEqual(self.calls, ["allow-in"])

    async def test_hung_node_is_reported_failed(self):
        # Повисшее SOCKS-рукопожатие не соблюдает таймауты httpx —
        # без своего потолка рассылка ждала бы эту ноду вечно
        async def hang(server, policy):
            await asyncio.sleep(10)
            return True

        self.manager._send_allow = hang
        with mock.patch.object(bm, "ALLOW_PUSH_BUDGET", 0.01):
            self.assertEqual(await self.manager._push_allow_one(self.server, POLICY_A), (3, False))


class SyncPingToNodeTest(unittest.IsolatedAsyncioTestCase):
    async def send(self, status_code: int) -> tuple[bool, str]:
        client = SimpleNamespace(post=mock.AsyncMock(return_value=SimpleNamespace(status_code=status_code)))
        server = SimpleNamespace(url="https://node:9100", name="node")
        with mock.patch.object(bm, "get_node_client", return_value=client), \
                mock.patch.object(bm, "node_auth_headers", return_value={}):
            result = await bm.BlocklistManager().sync_ping_to_node(server, True)
        client.post.assert_awaited_once()
        self.assertEqual(client.post.await_args.kwargs["json"], {"enabled": True})
        return result

    async def test_old_agent_is_not_a_failure(self):
        # Повтор из очереди ответил бы тем же 404 — долг не закрылся бы никогда
        ok, _ = await self.send(404)
        self.assertTrue(ok)

    async def test_node_error_is_failure(self):
        ok, message = await self.send(500)
        self.assertFalse(ok)
        self.assertEqual(message, "HTTP 500")


class FirstErrorTest(unittest.TestCase):
    def test_ping_error_is_reported(self):
        result = {
            "in": {"success": True, "message": "Synced"},
            "out": {"success": True, "message": "Synced"},
            "ping": {"success": False, "message": "Timeout"},
        }
        self.assertEqual(bm.BlocklistManager._first_error(result), "Timeout")


if __name__ == "__main__":
    unittest.main()
