"""Тесты доставки блок-листа на большой парк (app/services/blocklist_manager.py).

Без PostgreSQL и без сети: сессия, правила, источники и запросы к нодам
подменены. Проверяется, что ожидание в очередях не расходует время ноды, что
список, который нода уже приняла, не отправляется повторно, что суточный цикл
не гоняет его впустую и что общие списки собираются один раз на всех, кто их
ждёт.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import gc
import json
import os
import sys
import unittest
import weakref
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional, Sequence
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from sqlalchemy.dialects import postgresql  # noqa: E402

    from app.services import blocklist_manager as bm  # noqa: E402
    from app.services.latest_metrics import get_latest_metrics  # noqa: E402
    from app.services.ping_block import PingBlockScope  # noqa: E402
except ImportError as e:  # рантайм панели (sqlalchemy, asyncpg) не установлен
    raise unittest.SkipTest(f"blocklist_manager requires the panel runtime: {e}")


# Потолок времени на ноду в тестах очередей: доставка у подменённой ноды
# мгновенная, а ждать слот приходится втрое дольше потолка
NODE_BUDGET = 0.2
QUEUE_WAIT = NODE_BUDGET * 3


def node(server_id: int, online: bool = True, capabilities: Optional[str] = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=server_id,
        name=f"node-{server_id}",
        folder=None,
        url=f"https://node-{server_id}:9100",
        last_seen=datetime.now(timezone.utc) if online else None,
        node_capabilities=capabilities,
    )


class FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)

    def scalars(self) -> "FakeResult":
        return self

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Сессия без базы: на запрос к источникам отдаёт заготовленные источники,
    на любой другой — заготовленные серверы."""

    def __init__(self, servers: list, sources: Sequence = ()) -> None:
        self._servers = servers
        self._sources = sources

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def execute(self, statement) -> FakeResult:
        if "blocklist_sources" in str(statement):
            return FakeResult(list(self._sources))
        return FakeResult(self._servers)

    async def commit(self) -> None:
        pass


class DeliveryTestCase(unittest.IsolatedAsyncioTestCase):
    """Менеджер, у которого вместо базы и нод — поля теста.

    Что лежит в правилах и источниках, задают `global_rules`, `source_ips`,
    `own_rules` и `policy`; строки серверов и источников, которые отдаёт
    база, — `servers` и `sources`; что ушло на ноды, копится в `sent`."""

    def setUp(self) -> None:
        self.servers: list[SimpleNamespace] = []
        self.sources: list[SimpleNamespace] = []
        self.global_rules = {"in": ["1.1.1.1"], "out": ["2.2.2.2"]}
        self.source_ips = {"in": ["8.8.8.8", "9.9.9.9"], "out": []}
        self.own_rules: dict[tuple[int, str], list[str]] = {}
        self.policy = bm.AllowPolicy(
            ips={"in": ["5.5.5.5"], "out": ["5.5.5.5"]},
            ping_scope=PingBlockScope(),
            ping_blocked_ids=frozenset(),
        )
        self.sent: list[tuple] = []
        self.block_reply = (True, "Synced", {})

        self.manager = bm.BlocklistManager()
        self.manager.build_allow_policy = self._allow_policy
        self.manager._prune_last_good = mock.AsyncMock()
        self.manager.get_global_rules = self._global_rules
        self.manager.get_auto_list_ips = self._source_ips
        self.manager.get_server_rules = self._own_rules
        self.manager.sync_to_node = self._send_block
        self.manager.sync_allow_to_node = self._send_allow
        self.manager.sync_ping_to_node = self._send_ping

        for target, replacement in (
            ("async_session", lambda: FakeSession(self.servers, self.sources)),
            ("enqueue", mock.AsyncMock()),
        ):
            patcher = mock.patch.object(bm, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def add_node(self, server_id: int, **kwargs) -> SimpleNamespace:
        server = node(server_id, **kwargs)
        self.servers.append(server)
        return server

    def add_source(self, source_id: int) -> SimpleNamespace:
        """Источник, содержимое которого при скачивании — `source_ips["in"]`."""
        source = SimpleNamespace(
            id=source_id, name="feed", url="https://lists/feed",
            last_hash=None, last_updated=None, ip_count=0, error_message=None,
        )
        self.sources.append(source)
        self.manager.fetch_github_list = self._fetch_source
        return source

    async def _fetch_source(
        self, url: str, timeout: float = 30.0, use_cache: bool = True
    ) -> tuple[bool, list[str], str]:
        return True, list(self.source_ips["in"]), ""

    def lists_sent(self) -> list[tuple]:
        """Списки, ушедшие на ноды с прошлого вызова (ping сюда не входит)."""
        sent = [entry for entry in self.sent if entry[0] != "ping"]
        self.sent.clear()
        return sent

    @property
    def builds(self) -> int:
        """Сколько раз собирались общие списки."""
        return self.manager._prune_last_good.await_count

    async def _allow_policy(self) -> "bm.AllowPolicy":
        return self.policy

    async def _global_rules(self, db, direction: str = "in", list_type: str = "block") -> list[str]:
        return list(self.global_rules[direction])

    async def _source_ips(self, db, direction: str = "in") -> list[str]:
        return list(self.source_ips[direction])

    async def _own_rules(self, server_id: int, db, direction: str = "in") -> list[str]:
        return list(self.own_rules.get((server_id, direction), []))

    async def _send_block(self, server, body: bytes, ip_count: int) -> tuple[bool, str, dict]:
        self.sent.append(("block", server.id, json.loads(body)["direction"]))
        return self.block_reply

    async def _send_allow(self, server, ips: list[str], direction: str = "in") -> tuple[bool, str, dict]:
        self.sent.append(("allow", server.id, direction))
        return True, "Synced", {}

    async def _send_ping(self, server, block: bool) -> tuple[bool, str]:
        self.sent.append(("ping", server.id, block))
        return True, "Ping allowed"


class SyncBudgetTest(unittest.TestCase):
    def test_budget_grows_with_the_lists(self):
        # Фиксированный потолок обрубал бы синк крупного списка на середине
        manager = bm.BlocklistManager()
        small = {"in": {"count": 10}, "out": {"count": 0}}
        large = {"in": {"count": 1_000_000}, "out": {"count": 0}}
        self.assertGreater(manager._sync_budget(large), manager._sync_budget(small))


class QueueWaitTest(DeliveryTestCase):
    """Бюджет ноды — время её доставки, а не время в очередях панели."""

    async def asyncSetUp(self) -> None:
        self.server = self.add_node(1)
        self.shared = await self.manager.build_shared_lists()
        self.manager._sync_budget = lambda shared: NODE_BUDGET

    async def sync_while_holding(self, semaphore: asyncio.Semaphore, slots: int) -> dict:
        """Синк ноды, которой нужный слот достаётся только через QUEUE_WAIT."""
        for _ in range(slots):
            await semaphore.acquire()
        sync = asyncio.create_task(self.manager._sync_one_server_safe(self.server, self.shared))
        await asyncio.sleep(QUEUE_WAIT)
        for _ in range(slots):
            semaphore.release()
        return await sync

    async def test_waiting_for_an_http_slot_does_not_consume_the_budget(self):
        # 2000 нод на 50 слотов: хвост очереди ждёт слот дольше, чем длится сама
        # доставка, и получал ложный Timeout, так и не отправив ни байта
        result = await self.sync_while_holding(self.manager._http_sem, bm.BlocklistManager.HTTP_CONCURRENCY)
        self.assertTrue(result["success"], result)

    async def test_waiting_for_an_own_body_slot_does_not_consume_the_budget(self):
        # Нодам со своими правилами тело собирается отдельно, не больше двух
        # разом — третья ждёт, пока первые две отправят свои списки
        self.own_rules[(1, "in")] = ["7.7.7.7"]
        result = await self.sync_while_holding(self.manager._extra_body_sem, 2)
        self.assertTrue(result["success"], result)

    async def test_no_more_than_two_own_bodies_at_once(self):
        # Отдельное тело на миллионах адресов — десятки мегабайт: копиться
        # в памяти по числу нод со своими правилами им нельзя
        nodes = [self.add_node(server_id) for server_id in range(2, 8)]
        for server in nodes:
            self.own_rules[(server.id, "in")] = ["7.7.7.7"]
        own_bodies = 0
        peak = 0

        async def slow_block(server, body: bytes, ip_count: int) -> tuple[bool, str, dict]:
            nonlocal own_bodies, peak
            is_own = b"7.7.7.7" in body
            own_bodies += is_own
            peak = max(peak, own_bodies)
            await asyncio.sleep(0.02)
            own_bodies -= is_own
            return True, "Synced", {}

        self.manager.sync_to_node = slow_block
        del self.manager._sync_budget
        results = await asyncio.gather(*(
            self.manager._sync_one_server_safe(server, self.shared) for server in nodes
        ))
        self.assertTrue(all(result["success"] for result in results))
        self.assertEqual(peak, 2)

    async def test_node_hanging_after_its_slot_is_still_cut_off(self):
        async def hang(server, body, ip_count):
            await asyncio.sleep(30)

        self.manager.sync_to_node = hang
        result = await self.manager._sync_one_server_safe(self.server, self.shared)
        self.assertFalse(result["success"])
        self.assertEqual(result["in"]["message"], "Timeout")


BLOCK_IN, ALLOW_IN = ("block", 1, "in"), ("allow", 1, "in")
BLOCK_OUT, ALLOW_OUT = ("block", 1, "out"), ("allow", 1, "out")
ALL_LISTS = [BLOCK_IN, ALLOW_IN, BLOCK_OUT, ALLOW_OUT]


class UnchangedListsTest(DeliveryTestCase):
    """Список, который нода уже приняла, повторно на неё не уходит."""

    async def asyncSetUp(self) -> None:
        self.server = self.add_node(1)
        self.first_policy = self.policy
        self.second_policy = bm.AllowPolicy(
            ips={"in": ["5.5.5.5", "6.6.6.6"], "out": ["5.5.5.5"]},
            ping_scope=PingBlockScope(),
            ping_blocked_ids=frozenset(),
        )
        await self.deliver()
        self.assertEqual(self.lists_sent(), ALL_LISTS)

    async def deliver(self, server: Optional[SimpleNamespace] = None) -> dict:
        shared = await self.manager.build_shared_lists()
        return await self.manager._sync_one_server_safe(server or self.server, shared)

    async def test_unchanged_lists_are_not_sent_again(self):
        result = await self.deliver()
        self.assertEqual(self.lists_sent(), [])
        self.assertTrue(result["success"])
        self.assertEqual(result["in"]["message"], bm.UNCHANGED_MESSAGE)
        self.assertEqual(result["in"]["ip_count"], 3)

    async def test_new_global_rule_resends_only_its_block_list(self):
        self.global_rules["out"].append("3.3.3.3")
        await self.deliver()
        self.assertEqual(self.lists_sent(), [BLOCK_OUT])

    async def test_changed_source_resends_only_its_block_list(self):
        self.source_ips["in"].append("4.4.4.4")
        await self.deliver()
        self.assertEqual(self.lists_sent(), [BLOCK_IN])

    async def test_changed_allow_list_goes_without_the_block_list(self):
        # Адреса нод меняются чаще блок-листа: слать ради нового адреса
        # блок-лист на сотни тысяч записей незачем
        self.policy = self.second_policy
        result = await self.deliver()
        self.assertEqual(self.lists_sent(), [ALLOW_IN])
        self.assertEqual(result["in"]["message"], bm.UNCHANGED_MESSAGE)
        self.assertEqual(result["in"]["allow"]["message"], "Synced")

    async def test_own_rule_resends_the_list_to_that_node_only(self):
        neighbour = self.add_node(2)
        await self.deliver(neighbour)
        self.sent.clear()

        self.own_rules[(1, "in")] = ["7.7.7.7"]
        await self.deliver()
        await self.deliver(neighbour)
        self.assertEqual(self.lists_sent(), [BLOCK_IN])

    async def test_order_of_own_rules_does_not_matter(self):
        # База отдаёт правила без порядка — перестановка не повод слать заново
        self.own_rules[(1, "in")] = ["7.7.7.7", "6.6.6.6"]
        await self.deliver()
        self.sent.clear()

        self.own_rules[(1, "in")] = ["6.6.6.6", "7.7.7.7"]
        await self.deliver()
        self.assertEqual(self.lists_sent(), [])

    async def test_failed_delivery_is_forgotten(self):
        # Нода могла успеть применить новый список до ошибки — что на ней лежит,
        # неизвестно, и возврат к прежнему содержимому тоже надо отправить
        self.global_rules["in"].append("3.3.3.3")
        self.block_reply = (False, "HTTP 500", {})
        result = await self.deliver()
        self.assertFalse(result["success"])
        self.sent.clear()

        self.global_rules["in"].remove("3.3.3.3")
        self.block_reply = (True, "Synced", {})
        await self.deliver()
        self.assertEqual(self.lists_sent(), [BLOCK_IN])

    async def test_rejected_allow_list_is_forgotten(self):
        self.policy = self.second_policy
        self.manager.sync_allow_to_node = mock.AsyncMock(return_value=(False, "HTTP 500", {}))
        result = await self.deliver()
        self.assertFalse(result["success"])
        self.sent.clear()

        self.policy = self.first_policy
        self.manager.sync_allow_to_node = self._send_allow
        await self.deliver()
        self.assertEqual(self.lists_sent(), [ALLOW_IN])

    async def test_timed_out_delivery_is_forgotten(self):
        async def hang(server, body, ip_count):
            await asyncio.sleep(30)

        self.global_rules["in"].append("3.3.3.3")
        self.manager.sync_to_node = hang
        self.manager._sync_budget = lambda shared: 0.05
        result = await self.deliver()
        self.assertEqual(result["in"]["message"], "Timeout")

        self.global_rules["in"].remove("3.3.3.3")
        self.manager.sync_to_node = self._send_block
        del self.manager._sync_budget
        await self.deliver()
        self.assertEqual(self.lists_sent(), [BLOCK_IN])


class OneDeliveryPerNodeTest(DeliveryTestCase):
    """Полный синк, синк одной ноды и очередь долгов могут прийти к ноде разом.

    Вперемешку они оставили бы на ноде список одного, а в памяти панели —
    хэш другого, и расхождение уже никто бы не заметил."""

    SEND_TIME = 0.05

    async def asyncSetUp(self) -> None:
        self.server = self.add_node(1)
        self.in_flight = 0
        self.peak = 0
        self.incoming_lists: list[list[str]] = []
        self.manager.sync_to_node = self._slow_block

    async def _slow_block(self, server, body: bytes, ip_count: int) -> tuple[bool, str, dict]:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(self.SEND_TIME)
        self.in_flight -= 1
        payload = json.loads(body)
        if payload["direction"] == "in":
            self.incoming_lists.append(payload["ips"])
        return True, "Synced", {}

    async def test_deliveries_to_one_node_do_not_overlap(self):
        shared = await self.manager.build_shared_lists()
        await asyncio.gather(*(
            self.manager._sync_one_server_safe(self.server, shared) for _ in range(3)
        ))
        self.assertEqual(self.peak, 1)

    async def test_other_nodes_are_not_held_up(self):
        neighbour = self.add_node(2)
        shared = await self.manager.build_shared_lists()
        await asyncio.gather(
            self.manager._sync_one_server_safe(self.server, shared),
            self.manager._sync_one_server_safe(neighbour, shared),
        )
        self.assertEqual(self.peak, 2)

    async def test_delivery_that_waited_reads_the_rules_saved_meanwhile(self):
        # Пока полный синк стоял в очереди к ноде, оператор добавил ей правило:
        # со снимком правил, снятым до очереди, синк стёр бы его с ноды
        shared = await self.manager.build_shared_lists()
        first = asyncio.create_task(self.manager._sync_one_server_safe(self.server, shared))
        second = asyncio.create_task(self.manager._sync_one_server_safe(self.server, shared))
        await asyncio.sleep(self.SEND_TIME / 2)
        self.own_rules[(1, "in")] = ["7.7.7.7"]
        await asyncio.gather(first, second)
        self.assertIn("7.7.7.7", self.incoming_lists[-1])


class WeakLists(dict):
    """Общие списки, за освобождением которых можно проследить: обычный dict
    слабых ссылок не допускает."""


class SharedListsCacheTest(DeliveryTestCase):
    """Общие списки собираются один раз на всех, кто их ждёт.

    Ожившая нода синхронизируется отдельным вызовом; при массовом
    восстановлении каждая пересобирала бы списки на сотни тысяч адресов."""

    SHORT_TTL = 0.05

    async def test_concurrent_requests_share_one_build(self):
        results = await asyncio.gather(*(self.manager._shared_lists() for _ in range(20)))
        self.assertEqual(self.builds, 1)
        self.assertTrue(all(shared is results[0] for shared in results))

    async def test_lists_are_reused_while_fresh(self):
        first = await self.manager._shared_lists()
        self.assertIs(await self.manager._shared_lists(), first)
        self.assertEqual(self.builds, 1)

    async def test_lists_are_rebuilt_after_the_ttl(self):
        with mock.patch.object(bm, "SHARED_LISTS_TTL_SECONDS", self.SHORT_TTL):
            await self.manager._shared_lists()
            await asyncio.sleep(self.SHORT_TTL * 3)
            await self.manager._shared_lists()
        self.assertEqual(self.builds, 2)

    async def test_expired_lists_do_not_stay_in_memory(self):
        # Списки на миллионы адресов — сотни мегабайт: держать их до
        # следующего синка (сутки) нельзя
        async def build() -> WeakLists:
            return WeakLists()

        self.manager.build_shared_lists = build
        with mock.patch.object(bm, "SHARED_LISTS_TTL_SECONDS", self.SHORT_TTL):
            lists = weakref.ref(await self.manager._shared_lists())
            await asyncio.sleep(self.SHORT_TTL * 3)
        gc.collect()
        self.assertIsNone(lists())

    async def test_failed_build_reaches_every_waiter_and_is_not_kept(self):
        unavailable = bm.BlocklistSourceUnavailableError("feed", "https://lists/feed", "Timeout")

        async def broken_source(db, direction: str = "in") -> list[str]:
            await asyncio.sleep(0)
            raise unavailable

        self.manager.get_auto_list_ips = broken_source
        results = await asyncio.gather(
            *(self.manager._shared_lists() for _ in range(3)), return_exceptions=True
        )
        self.assertEqual(results, [unavailable] * 3)
        self.assertEqual(self.builds, 1)

        self.manager.get_auto_list_ips = self._source_ips
        self.assertIn("in", await self.manager._shared_lists())

    async def test_cancelled_waiter_does_not_abort_the_build_for_others(self):
        release = asyncio.Event()

        async def slow_policy() -> "bm.AllowPolicy":
            await release.wait()
            return self.policy

        self.manager.build_allow_policy = slow_policy
        gone = asyncio.create_task(self.manager._shared_lists())
        waiting = asyncio.create_task(self.manager._shared_lists())
        await asyncio.sleep(0.01)
        gone.cancel()
        await asyncio.sleep(0.01)
        release.set()

        self.assertIn("in", await waiting)
        self.assertEqual(self.builds, 1)

    async def test_invalidation_drops_the_lists(self):
        await self.manager._shared_lists()
        self.manager.invalidate_shared_lists()
        await self.manager._shared_lists()
        self.assertEqual(self.builds, 2)

    async def test_lists_built_across_a_change_are_not_kept(self):
        # Сборка, начатая до правки, могла прочитать старые правила: кто пришёл
        # после правки, её не ждёт, и в кэш она не попадает, даже если
        # закончилась позже свежей
        finish_stale, finish_fresh = asyncio.Event(), asyncio.Event()
        finishes = iter((finish_stale, finish_fresh))

        async def slow_policy() -> "bm.AllowPolicy":
            await next(finishes).wait()
            return self.policy

        self.manager.build_allow_policy = slow_policy
        before = asyncio.create_task(self.manager._shared_lists())
        await asyncio.sleep(0.01)
        self.manager.invalidate_shared_lists()
        after = asyncio.create_task(self.manager._shared_lists())
        await asyncio.sleep(0.01)
        finish_fresh.set()
        fresh = await asyncio.wait_for(after, timeout=1.0)
        finish_stale.set()
        stale = await asyncio.wait_for(before, timeout=1.0)

        self.assertIsNot(stale, fresh)
        self.assertEqual(self.builds, 2)
        self.assertIs(await self.manager._shared_lists(), fresh)

    async def test_allowlist_change_drops_the_lists(self):
        # Сервер добавили, удалили или сменили ему адрес — в кэше старый
        # белый список
        await self.manager._shared_lists()
        self.manager.request_allowlist_push()
        await self.manager._shared_lists()
        self.assertEqual(self.builds, 2)


class SyncFlowsTest(DeliveryTestCase):
    """Кто из трёх путей синка шлёт списки целиком, а кто — только изменившееся."""

    async def asyncSetUp(self) -> None:
        self.server = self.add_node(1)

    async def test_mass_recovery_builds_the_shared_lists_once(self):
        await asyncio.gather(*(self.manager.sync_single_node_by_id(1) for _ in range(10)))
        self.assertEqual(self.builds, 1)

    async def test_recovery_and_debt_queue_share_one_build(self):
        # Ожившую ноду разом подхватывают и авто-восстановление, и очередь долгов
        await asyncio.gather(
            self.manager.sync_single_node_by_id(1),
            self.manager.sync_nodes_by_ids([1]),
        )
        self.assertEqual(self.builds, 1)
        self.assertEqual(self.lists_sent(), ALL_LISTS)

    async def test_node_back_online_gets_the_lists_in_full(self):
        # Что осталось на ноде после простоя, панель не знает: её могли
        # переустановить или откатить из снимка
        await self.manager.sync_single_node_by_id(1)
        self.sent.clear()

        result = await self.manager.sync_single_node_by_id(1)
        self.assertTrue(result["success"])
        self.assertEqual(self.lists_sent(), ALL_LISTS)

    async def test_delivery_in_flight_does_not_count_once_the_node_is_unknown(self):
        # Пока списки шли ноде, серверу сменили адрес: начатая доставка дойдёт
        # до прежней машины, и засчитать её новой нельзя
        release = asyncio.Event()

        async def slow_block(server, body, ip_count):
            await release.wait()
            return await self._send_block(server, body, ip_count)

        self.manager.sync_to_node = slow_block
        in_flight = asyncio.create_task(self.manager.sync_nodes_by_ids([1]))
        await asyncio.sleep(0.05)
        reconfigured = asyncio.create_task(self.manager.sync_single_node_by_id(1))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(in_flight, reconfigured)

        self.assertEqual(self.lists_sent(), ALL_LISTS * 2)

    async def test_node_seen_offline_gets_the_lists_in_full_from_the_queue(self):
        await self.manager.sync_all_nodes()
        self.server.last_seen = None
        queued = await self.manager.sync_all_nodes()
        self.assertTrue(queued[1]["queued"])
        self.sent.clear()

        self.server.last_seen = datetime.now(timezone.utc)
        self.assertEqual(await self.manager.sync_nodes_by_ids([1]), {1: None})
        self.assertEqual(self.lists_sent(), ALL_LISTS)

    async def test_full_sync_skips_nodes_that_have_the_lists(self):
        await self.manager.sync_all_nodes()
        self.sent.clear()

        results = await self.manager.sync_all_nodes()
        self.assertTrue(results[1]["success"])
        self.assertEqual(self.lists_sent(), [])

    async def test_full_sync_reads_the_rules_anew(self):
        # Роутер зовёт полный синк сразу после правки правила или источника —
        # списки, собранные минуту назад, эту правку не содержат
        await self.manager._shared_lists()
        self.global_rules["in"].append("3.3.3.3")
        await self.manager.sync_all_nodes()
        self.assertEqual(self.builds, 2)
        self.assertEqual(self.lists_sent(), ALL_LISTS)

    async def test_debt_queue_resends_only_what_the_node_lacks(self):
        # Долг за сорвавшийся ping не повод слать заново списки на сотни тысяч адресов
        self.manager.sync_ping_to_node = mock.AsyncMock(return_value=(False, "HTTP 500"))
        results = await self.manager.sync_all_nodes()
        self.assertFalse(results[1]["success"])
        self.sent.clear()

        self.manager.sync_ping_to_node = self._send_ping
        self.assertEqual(await self.manager.sync_nodes_by_ids([1]), {1: None})
        self.assertEqual(self.sent, [("ping", 1, False)])


class DailyTickTest(DeliveryTestCase):
    """Суточный цикл не обходит парк, если нодам нечего досылать."""

    async def asyncSetUp(self) -> None:
        self.server = self.add_node(1)
        self.sources_changed = False
        self.manager.refresh_all_sources = self._refresh_all_sources
        await self.manager.sync_all_nodes()
        self.sent.clear()

    async def _refresh_all_sources(self) -> tuple[dict, bool]:
        return {}, self.sources_changed

    async def test_changed_source_goes_to_the_nodes(self):
        self.source_ips["in"].append("4.4.4.4")
        self.sources_changed = True
        await self.manager._refresh_and_sync()
        self.assertEqual(self.lists_sent(), [BLOCK_IN])

    async def test_unchanged_sources_do_not_touch_the_nodes(self):
        await self.manager._refresh_and_sync()
        self.assertEqual(self.sent, [])

    async def test_online_node_without_the_lists_gets_them(self):
        # Новая нода, сбой прошлой доставки, рестарт панели — про такую ноду
        # неизвестно, что на ней лежит
        self.add_node(2)
        await self.manager._refresh_and_sync()
        self.assertEqual(
            self.lists_sent(),
            [("block", 2, "in"), ("allow", 2, "in"), ("block", 2, "out"), ("allow", 2, "out")],
        )

    async def test_offline_node_is_left_to_its_return(self):
        self.add_node(2, online=False)
        await self.manager._refresh_and_sync()
        self.assertEqual(self.sent, [])

    async def test_node_that_closed_the_section_is_not_a_reason_to_sync(self):
        self.add_node(2, capabilities='{"ipset":"no"}')
        await self.manager._refresh_and_sync()
        self.assertEqual(self.sent, [])

    async def test_sync_cancelled_as_a_whole_is_retried(self):
        # Правку правила роутер уже отдал полному синку, а тот сорвался до
        # нод: источники с тех пор не менялись, но ноды правку так и не получили
        async def broken_source(db, direction: str = "in") -> list[str]:
            raise bm.BlocklistSourceUnavailableError("feed", "https://lists/feed", "Timeout")

        self.global_rules["in"].append("3.3.3.3")
        self.manager.get_auto_list_ips = broken_source
        await self.manager.sync_all_nodes()
        self.assertEqual(self.sent, [])

        self.manager.get_auto_list_ips = self._source_ips
        await self.manager._refresh_and_sync()
        self.assertEqual(self.lists_sent(), [BLOCK_IN])

    async def test_source_change_recorded_by_a_refresh_that_crashed_is_not_lost(self):
        # Обновление записало новый хэш источника и упало на следующем: при
        # повторе этот источник уже «не менялся», а ноды его так и не получили
        self.add_source(7)
        self.source_ips["in"].append("4.4.4.4")
        await self.manager.refresh_source(7)

        await self.manager._refresh_and_sync()
        self.assertEqual(self.lists_sent(), [BLOCK_IN])

    async def test_source_that_stays_over_the_node_limit_does_not_start_a_sync_every_day(self):
        self.add_source(7)
        self.source_ips["in"].append("4.4.4.4")
        with mock.patch.object(bm, "NODE_MAX_IPSET_ENTRIES", 2):
            await self.manager.refresh_source(7)
            await self.manager._refresh_and_sync()
            self.sent.clear()

            await self.manager.refresh_source(7)
            await self.manager._refresh_and_sync()
        self.assertEqual(self.sent, [])

    async def test_source_change_during_a_fleet_sync_waits_for_the_next_pass(self):
        # Синк, собравший списки до смены источника, её не доставил — закрыть
        # ею долг перед парком он не вправе
        self.add_source(7)
        release = asyncio.Event()

        async def slow_block(server, body, ip_count):
            await release.wait()
            return await self._send_block(server, body, ip_count)

        self.manager.sync_to_node = slow_block
        self.global_rules["out"].append("3.3.3.3")
        running = asyncio.create_task(self.manager.sync_all_nodes())
        await asyncio.sleep(0.05)
        self.source_ips["in"].append("4.4.4.4")
        await self.manager.refresh_source(7)
        release.set()
        await running
        self.assertEqual(self.lists_sent(), [BLOCK_OUT])

        await self.manager._refresh_and_sync()
        self.assertEqual(self.lists_sent(), [BLOCK_IN])


class AllowlistPushTest(DeliveryTestCase):
    """Отдельная рассылка белого списка меняет то, что лежит на ноде.

    Без учёта этого первый же полный синк после нового адреса ноды заново
    отправил бы всему парку блок-лист, который никто не менял."""

    NEW_POLICY = bm.AllowPolicy(
        ips={"in": ["5.5.5.5", "6.6.6.6"], "out": ["5.5.5.5", "6.6.6.6"]},
        ping_scope=PingBlockScope(),
        ping_blocked_ids=frozenset(),
    )

    async def asyncSetUp(self) -> None:
        self.server = self.add_node(1)
        await self.manager.sync_all_nodes()
        self.manager._allowlist_requested.clear()
        self.sent.clear()

    async def test_pushed_allowlist_does_not_resend_the_block_list(self):
        self.policy = self.NEW_POLICY
        self.assertEqual(await self.manager._push_allow_one(self.server, self.policy), (1, True))
        self.assertEqual(self.lists_sent(), [ALLOW_IN, ALLOW_OUT])

        await self.manager.sync_all_nodes()
        self.assertEqual(self.lists_sent(), [])

    async def test_failed_push_leaves_the_allow_lists_unknown(self):
        # Часть списка могла лечь на ноду до сбоя: даже если политика вернётся
        # к прежней, долг из очереди должен отправить белый список заново.
        # Блок-лист рассылка не трогала — он остаётся принятым
        self.manager.sync_ping_to_node = mock.AsyncMock(return_value=(False, "HTTP 500"))
        self.assertEqual(await self.manager._push_allow_one(self.server, self.NEW_POLICY), (1, False))
        self.sent.clear()

        self.manager.sync_ping_to_node = self._send_ping
        self.assertEqual(await self.manager.sync_nodes_by_ids([1]), {1: None})
        self.assertEqual(self.lists_sent(), [ALLOW_IN, ALLOW_OUT])

    async def test_timed_out_push_leaves_the_allow_lists_unknown(self):
        async def hang(server, ips, direction="in"):
            await asyncio.sleep(30)

        self.manager.sync_allow_to_node = hang
        with mock.patch.object(bm, "ALLOW_PUSH_BUDGET", 0.05):
            self.assertEqual(await self.manager._push_allow_one(self.server, self.NEW_POLICY), (1, False))

        self.manager.sync_allow_to_node = self._send_allow
        self.assertEqual(await self.manager.sync_nodes_by_ids([1]), {1: None})
        self.assertEqual(self.lists_sent(), [ALLOW_IN, ALLOW_OUT])

    async def test_node_offline_at_push_gets_the_lists_in_full_on_return(self):
        self.server.last_seen = None
        await self.manager._push_allowlist(self.NEW_POLICY)
        self.assertEqual(self.sent, [])

        self.server.last_seen = datetime.now(timezone.utc)
        self.assertEqual(await self.manager.sync_nodes_by_ids([1]), {1: None})
        self.assertEqual(self.lists_sent(), ALL_LISTS)

    async def test_push_does_not_wait_for_the_block_list_in_flight(self):
        # Блок-лист на сотни тысяч адресов нода применяет секундами, а новый
        # адрес ноды парк должен узнать сразу. Снимок полного синка, который
        # мог лечь на ноду поверх свежего списка, закрывает повторная рассылка
        release = asyncio.Event()

        async def slow_block(server, body, ip_count):
            await release.wait()
            return True, "Synced", {}

        self.manager.sync_to_node = slow_block
        self.manager._allowlist_pushed_hash = self.NEW_POLICY.digest()
        self.global_rules["out"].append("3.3.3.3")
        delivery = asyncio.create_task(self.manager.sync_all_nodes())
        await asyncio.sleep(0.05)

        pushed = await asyncio.wait_for(
            self.manager._push_allow_one(self.server, self.NEW_POLICY), timeout=1.0
        )
        self.assertEqual(pushed, (1, True))

        release.set()
        await delivery
        self.assertTrue(self.manager._allowlist_requested.is_set())

    async def test_snapshot_of_the_policy_being_replaced_requests_repush(self):
        # Полный синк со старой политикой закончился, пока шла рассылка новой:
        # ноды, до которых рассылка уже дошла, он мог вернуть к старому списку
        self.manager._allowlist_pushed_hash = self.policy.digest()
        self.manager._allowlist_inflight_hash = self.NEW_POLICY.digest()

        await self.manager.sync_all_nodes()

        self.assertTrue(self.manager._allowlist_requested.is_set())
        self.assertIsNone(self.manager._allowlist_pushed_hash)

    async def test_policy_change_noticed_by_the_periodic_check_drops_the_shared_lists(self):
        # Адрес ноды сменился без единой правки в панели — в собранных минуту
        # назад общих списках остался бы старый белый список
        self.manager._push_allowlist = mock.AsyncMock()
        await self.manager.push_allowlist_if_changed()
        await self.manager._shared_lists()
        builds = self.builds

        self.policy = self.NEW_POLICY
        await self.manager.push_allowlist_if_changed()
        shared = await self.manager._shared_lists()
        self.assertEqual(self.builds, builds + 1)
        self.assertIs(shared["allow_policy"], self.NEW_POLICY)

    async def test_unchanged_policy_keeps_the_shared_lists(self):
        self.manager._push_allowlist = mock.AsyncMock()
        await self.manager.push_allowlist_if_changed()
        await self.manager._shared_lists()
        builds = self.builds

        await self.manager.push_allowlist_if_changed()
        await self.manager._shared_lists()
        self.assertEqual(self.builds, builds)


class SourceRefreshTest(DeliveryTestCase):
    """Скачанный заново источник сбрасывает общие списки, только если его
    содержимое в синке изменилось."""

    async def asyncSetUp(self) -> None:
        self.add_source(7)
        await self.manager.refresh_source(7)
        await self.manager._shared_lists()

    async def builds_after_refresh(self) -> int:
        before = self.builds
        await self.manager.refresh_source(7)
        await self.manager._shared_lists()
        return self.builds - before

    async def test_changed_source_drops_the_shared_lists(self):
        self.source_ips["in"].append("4.4.4.4")
        self.assertEqual(await self.builds_after_refresh(), 1)

    async def test_unchanged_source_keeps_the_shared_lists(self):
        self.assertEqual(await self.builds_after_refresh(), 0)

    async def test_source_grown_over_the_node_limit_drops_the_shared_lists(self):
        # Такой источник в синк больше не идёт — в собранных списках он ещё есть
        self.source_ips["in"].append("4.4.4.4")
        with mock.patch.object(bm, "NODE_MAX_IPSET_ENTRIES", 2):
            self.assertEqual(await self.builds_after_refresh(), 1)

    async def test_source_that_stays_over_the_node_limit_is_not_a_change(self):
        self.source_ips["in"].append("4.4.4.4")
        with mock.patch.object(bm, "NODE_MAX_IPSET_ENTRIES", 2):
            await self.builds_after_refresh()
            self.assertEqual(await self.builds_after_refresh(), 0)

    async def test_source_back_under_the_node_limit_is_a_change(self):
        # Пока источник не помещался, его хэш не обновлялся: вернувшись
        # к прежнему содержимому, он выглядел бы неизменившимся, хотя в синке
        # его всё это время не было
        self.source_ips["in"].append("4.4.4.4")
        with mock.patch.object(bm, "NODE_MAX_IPSET_ENTRIES", 2):
            await self.builds_after_refresh()
            self.source_ips["in"].remove("4.4.4.4")
            before = self.builds
            _, _, _, changed = await self.manager.refresh_source(7)
            await self.manager._shared_lists()
        self.assertTrue(changed)
        self.assertEqual(self.builds - before, 1)


class RecordingSession(FakeSession):
    """Сессия без базы, которая запоминает текст запросов."""

    def __init__(self, rows: list) -> None:
        super().__init__(rows)
        self.queries: list[str] = []

    async def execute(self, statement) -> FakeResult:
        self.queries.append(" ".join(str(statement.compile(dialect=postgresql.dialect())).split()))
        return await super().execute(statement)


class StableOrderTest(unittest.IsolatedAsyncioTestCase):
    """Содержимое списка сверяется по хэшу тела запроса, а без ORDER BY база
    отдаёт строки как лежат: источник, обновлённый суточным циклом, переезжает
    в таблице, и тот же список в другом порядке ушёл бы на весь парк заново."""

    def setUp(self) -> None:
        self.manager = bm.BlocklistManager()
        self.session = RecordingSession([])

    async def test_sources_are_read_in_a_stable_order(self):
        await self.manager.get_auto_list_ips(self.session, "in")
        self.assertIn("ORDER BY blocklist_sources.id", self.session.queries[0])

    async def test_global_rules_are_read_in_a_stable_order(self):
        await self.manager.get_global_rules(self.session, "in")
        self.assertIn("ORDER BY blocklist_rules.id", self.session.queries[0])


# Заведомо не занятый другими тестами id в общем на процесс хранилище ответов
NODE_WITH_METRICS = 990001


class AllowPolicyNodeAddressesTest(unittest.IsolatedAsyncioTestCase):
    """Адреса интерфейсов нод для белого списка — из последних ответов в памяти.

    Колонка `servers.last_metrics` обновляется редко: новый адрес ноды из неё
    доезжал бы до белого списка парка с большим опозданием."""

    async def asyncSetUp(self) -> None:
        self.metrics = {"network": {"interfaces": []}}
        self.session = RecordingSession([
            SimpleNamespace(
                id=NODE_WITH_METRICS, url="https://5.9.0.10:9100", folder=None,
                dedicated_cert=False, last_metrics='{"saved": "long ago"}',
            ),
            SimpleNamespace(
                id=NODE_WITH_METRICS + 1, url="https://5.9.0.20:9100", folder="EU",
                dedicated_cert=True, last_metrics=None,
            ),
        ])
        self.collected: list[tuple] = []
        self.url_lists: list[str] = []
        url_lists = SimpleNamespace(collect=mock.AsyncMock(side_effect=lambda: list(self.url_lists)))

        latest = get_latest_metrics()
        latest.put(NODE_WITH_METRICS, self.metrics)
        self.addCleanup(latest.forget, NODE_WITH_METRICS)

        for target, replacement in (
            ("async_session", lambda: self.session),
            ("collect_node_ips", self._collect_node_ips),
            ("resolve_panel_ip", mock.AsyncMock(return_value="9.9.9.9")),
            ("load_scope", mock.AsyncMock(return_value=PingBlockScope())),
            ("get_allowlist_sources", lambda: url_lists),
        ):
            patcher = mock.patch.object(bm, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.manager = bm.BlocklistManager()
        self.manager.get_global_rules = mock.AsyncMock(return_value=[])
        self.policy = await self.manager.build_allow_policy()

    async def _collect_node_ips(self, nodes) -> set[str]:
        self.collected = list(nodes)
        return {"5.9.0.10", "5.9.0.11", "5.9.0.20"}

    async def test_interface_addresses_come_from_the_latest_answers_in_memory(self):
        self.assertEqual(self.collected, [
            ("https://5.9.0.10:9100", self.metrics, False),
            ("https://5.9.0.20:9100", None, True),
        ])
        self.assertIs(self.collected[0][1], self.metrics)

    async def test_saved_answers_are_not_read_from_the_database(self):
        self.assertFalse([query for query in self.session.queries if "last_metrics" in query])

    async def test_node_and_panel_addresses_reach_both_directions(self):
        expected = ["5.9.0.10", "5.9.0.11", "5.9.0.20", "9.9.9.9"]
        self.assertEqual(self.policy.ips, {"in": expected, "out": expected})

    async def test_lists_by_url_reach_incoming_only(self):
        # Списки CDN — про тех, кто приходит к нодам
        self.url_lists.extend(["104.16.0.0/13", "173.245.48.0/20"])

        policy = await self.manager.build_allow_policy()

        self.assertEqual(
            [entry for entry in policy.ips["in"] if entry not in policy.ips["out"]],
            ["104.16.0.0/13", "173.245.48.0/20"],
        )


if __name__ == "__main__":
    unittest.main()
