"""Страница сервера, нода которого перестала отвечать (app/routers/proxy.py).

Голый unittest, без PostgreSQL и без сети: сессия, нода и хранилище копий подменены.

Раньше у лежащей ноды страница показывала только верхние карточки: графики
запрашивали окно «от сейчас» и были пустыми, живые метрики и три нижние
карточки (лимит полосы, адреса, доступы хостера) ждали таймаута ноды. Теперь
ноду, которую мониторинг считает офлайн, панель не спрашивает и отдаёт то, что
сохранила сама.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402

from app.models import Server  # noqa: E402
from app.routers import proxy  # noqa: E402
from app.services import network_transactions, node_answer_copy, server_status  # noqa: E402
from app.services.latest_metrics import LatestMetrics  # noqa: E402
from app.services.node_answer_copy import AnswerCopy, AnswerKind  # noqa: E402

SERVER_ID = 1
OFFLINE_THRESHOLD = 60
LAST_ANSWER = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=3)
COPY_SAVED_AT = LAST_ANSWER - timedelta(hours=5)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_server(*, silent: bool, is_active: bool = True, node_version: str = "10.36.1") -> Server:
    last_seen = LAST_ANSWER if silent else datetime.now(timezone.utc)
    return Server(
        id=SERVER_ID, name="node-1", url="https://10.0.0.1:9100",
        last_seen=last_seen, is_active=is_active, node_version=node_version,
    )


def node_answer() -> dict:
    return {"live_rates": {"window_sec": 1.0}, "cpu": {"usage_percent": 5.1}, "collected_at": LAST_ANSWER.timestamp()}


class FakeSession:
    def __init__(self, server: Server, haproxy_cache: str | None = None) -> None:
        self._server = server
        self._haproxy_cache = haproxy_cache
        self.commit = mock.AsyncMock()

    async def execute(self, statement):
        if "server_cache" in str(statement):
            row = None if self._haproxy_cache is None else SimpleNamespace(last_haproxy_data=self._haproxy_cache)
            return SimpleNamespace(scalar_one_or_none=lambda: row)
        return SimpleNamespace(scalar_one_or_none=lambda: self._server)


class SilentNodeCase(unittest.IsolatedAsyncioTestCase):
    """Общая обвязка: сервер, память панели, нода и хранилище копий — подмены."""

    async def asyncSetUp(self) -> None:
        self.latest = LatestMetrics()
        self.node = mock.AsyncMock(return_value={"from": "node"})
        self.copies: dict[AnswerKind, AnswerCopy] = {}
        self.kept: list[tuple[AnswerKind, dict]] = []
        self.keep_intervals: dict[AnswerKind, float] = {}
        self.patch(proxy, "get_latest_metrics", mock.Mock(return_value=self.latest))
        self.patch(proxy, "get_latest_snapshot", mock.AsyncMock(return_value=None))
        self.patch(proxy, "proxy_request", self.node)
        self.patch(server_status, "get_offline_threshold", mock.AsyncMock(return_value=OFFLINE_THRESHOLD))
        self.patch(node_answer_copy, "load_copy", self.load_copy)
        self.patch(node_answer_copy, "keep_copy", self.keep_copy)

    def patch(self, target, name: str, replacement) -> None:
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def load_copy(self, _db, _server_id: int, kind: AnswerKind):
        return self.copies.get(kind)

    async def keep_copy(
        self, _db, _server_id: int, kind: AnswerKind, answer: dict, min_interval_sec: float = 0.0,
    ) -> None:
        # Слепок, как у настоящего хранилища: оно сериализует ответ в момент вызова
        self.kept.append((kind, dict(answer)))
        self.keep_intervals[kind] = min_interval_sec

    def use(self, server: Server, haproxy_cache: str | None = None) -> None:
        self.db = FakeSession(server, haproxy_cache)


class MetricsOfSilentNodeTest(SilentNodeCase):
    async def test_live_metrics_come_from_panel_memory_without_asking_the_node(self):
        self.use(make_server(silent=True))
        self.latest.put(SERVER_ID, node_answer())

        metrics = await proxy.get_live_metrics(SERVER_ID, db=self.db, _={})

        self.node.assert_not_awaited()
        self.assertEqual(metrics["cpu"], {"usage_percent": 5.1})
        self.assertEqual(metrics["node_silent_since"], iso(LAST_ANSWER))

    async def test_answering_node_is_asked_for_live_metrics(self):
        self.use(make_server(silent=False))
        self.latest.put(SERVER_ID, node_answer())
        self.node.return_value = {"live_rates": {"window_sec": 1.0}, "cpu": {"usage_percent": 9.0}}

        metrics = await proxy.get_live_metrics(SERVER_ID, db=self.db, _={})

        self.node.assert_awaited_once()
        self.assertEqual(metrics["cpu"], {"usage_percent": 9.0})
        self.assertNotIn("node_silent_since", metrics)

    async def test_silent_node_missing_from_memory_is_still_asked(self):
        self.use(make_server(silent=True))

        await proxy.get_live_metrics(SERVER_ID, db=self.db, _={})

        self.node.assert_awaited_once()

    async def test_disabled_server_is_asked_live_because_nobody_polls_it(self):
        self.use(make_server(silent=True, is_active=False))
        self.latest.put(SERVER_ID, node_answer())

        await proxy.get_live_metrics(SERVER_ID, db=self.db, _={})

        self.node.assert_awaited_once()

    async def test_database_connection_is_released_before_the_node_is_asked(self):
        self.use(make_server(silent=False))
        commits_before_request: list[int] = []
        self.node.side_effect = lambda *_args, **_kwargs: commits_before_request.append(self.db.commit.await_count) or {}

        await proxy.get_live_metrics(SERVER_ID, db=self.db, _={})

        # Первый коммит — после чтения сервера, второй — после чтения порога офлайна
        self.assertEqual(commits_before_request, [2])

    async def test_memory_metrics_say_since_when_the_node_is_silent(self):
        self.use(make_server(silent=True))
        self.latest.put(SERVER_ID, node_answer())

        metrics = await proxy.get_metrics(SERVER_ID, db=self.db, _={})

        self.assertEqual(metrics["node_silent_since"], iso(LAST_ANSWER))

    async def test_memory_metrics_of_disabled_server_follow_the_same_rule_as_live_ones(self):
        # Отключённый сервер панель не опрашивает, а его нода может быть жива: живой
        # запрос к ней идёт без пометки. Пометь её ответ из памяти — плашка
        # «не отвечает» мигала бы при каждой смене источника
        self.use(make_server(silent=True, is_active=False))
        self.latest.put(SERVER_ID, node_answer())

        self.assertNotIn("node_silent_since", await proxy.get_metrics(SERVER_ID, db=self.db, _={}))

    async def test_memory_metrics_of_answering_node_carry_no_mark(self):
        self.use(make_server(silent=False))
        self.latest.put(SERVER_ID, node_answer())

        self.assertEqual(await proxy.get_metrics(SERVER_ID, db=self.db, _={}), node_answer())

    async def test_mark_does_not_leak_into_the_shared_answer(self):
        self.use(make_server(silent=True))
        self.latest.put(SERVER_ID, node_answer())

        await proxy.get_metrics(SERVER_ID, db=self.db, _={})

        self.assertNotIn("node_silent_since", self.latest.get(SERVER_ID))


class HistoryOfSilentNodeTest(SilentNodeCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.load_history = mock.AsyncMock(return_value={"data": []})
        self.patch(proxy, "load_history", self.load_history)

    async def frozen_at(self, server: Server):
        self.use(server)
        await proxy.get_metrics_history(SERVER_ID, period="1h", include_per_cpu=False, db=self.db, _={})
        return self.load_history.await_args.args[4]

    async def test_history_of_silent_node_is_frozen_at_its_last_answer(self):
        self.assertEqual(await self.frozen_at(make_server(silent=True)), LAST_ANSWER)

    async def test_history_of_answering_node_runs_up_to_now(self):
        self.assertIsNone(await self.frozen_at(make_server(silent=False)))

    async def test_history_of_disabled_server_is_frozen_too(self):
        # Отключённый сервер панель не опрашивает: новых точек не будет, показываем старые
        self.assertEqual(await self.frozen_at(make_server(silent=True, is_active=False)), LAST_ANSWER)


class AnswerCopyTest(SilentNodeCase):
    async def bandwidth(self) -> dict:
        return await proxy.get_bandwidth_limit(SERVER_ID, db=self.db, _={})

    async def test_answer_of_the_node_is_returned_and_copied(self):
        self.use(make_server(silent=False))
        self.node.return_value = {"enabled": True, "mbit": 500}

        answer = await self.bandwidth()

        self.assertEqual(answer, {"enabled": True, "mbit": 500})
        self.assertEqual(self.kept, [(AnswerKind.BANDWIDTH_LIMIT, {"enabled": True, "mbit": 500})])

    async def test_silent_node_is_not_asked_and_its_copy_is_returned(self):
        self.use(make_server(silent=True))
        self.copies[AnswerKind.BANDWIDTH_LIMIT] = AnswerCopy({"enabled": True, "mbit": 500}, COPY_SAVED_AT)

        answer = await self.bandwidth()

        self.node.assert_not_awaited()
        self.assertEqual(answer, {"enabled": True, "mbit": 500, "saved_at": iso(COPY_SAVED_AT)})
        self.assertEqual(self.kept, [])

    async def test_silent_node_without_a_copy_says_so_instead_of_timing_out(self):
        self.use(make_server(silent=True))

        with self.assertRaises(HTTPException) as raised:
            await self.bandwidth()

        self.node.assert_not_awaited()
        # 409, а не 502–504: те фронт сам повторяет трижды
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.headers, {proxy.NO_ANSWER_COPY_HEADER: "1"})

    async def test_failure_of_a_node_that_monitoring_counts_as_alive_is_not_replaced_by_a_copy(self):
        # Копия — только для ноды, которая офлайн по мониторингу. Разовый сбой
        # подменять нельзя: карточка осталась бы на копии с подписью «не отвечает»,
        # хотя нода жива, а отказ самой ноды выглядел бы как её молчание
        self.use(make_server(silent=False))
        self.copies[AnswerKind.BANDWIDTH_LIMIT] = AnswerCopy({"enabled": False, "mbit": 0}, COPY_SAVED_AT)

        for status in (404, 409, 500, 502, 504):
            with self.subTest(status=status):
                self.node.side_effect = HTTPException(status_code=status)

                with self.assertRaises(HTTPException) as raised:
                    await self.bandwidth()

                self.assertEqual(raised.exception.status_code, status)

    async def test_disabled_server_is_asked_live(self):
        self.use(make_server(silent=True, is_active=False))

        await self.bandwidth()

        self.node.assert_awaited_once()

    async def test_new_limit_is_copied_without_the_message_of_the_node(self):
        self.use(make_server(silent=False))
        self.node.return_value = {"enabled": True, "mbit": 300, "message": "Bandwidth limit 300 Mbit/s applied on eth0"}

        answer = await proxy.set_bandwidth_limit(SERVER_ID, {"enabled": True, "mbit": 300}, db=self.db, _={})

        self.assertEqual(answer["message"], "Bandwidth limit 300 Mbit/s applied on eth0")
        self.assertEqual(self.kept, [(AnswerKind.BANDWIDTH_LIMIT, {"enabled": True, "mbit": 300})])

    async def test_hoster_scan_of_silent_node_comes_from_the_copy(self):
        self.use(make_server(silent=True))
        self.copies[AnswerKind.HOSTER_SCAN] = AnswerCopy({"supported": True, "findings": [{"id": "qemu-ga"}]}, COPY_SAVED_AT)

        scan = await proxy.hoster_access_scan(SERVER_ID, db=self.db, _={})

        self.node.assert_not_awaited()
        self.assertEqual(scan["findings"], [{"id": "qemu-ga"}])
        self.assertEqual(scan["saved_at"], iso(COPY_SAVED_AT))

    async def test_hoster_scan_of_answering_node_is_copied(self):
        self.use(make_server(silent=False))
        self.node.return_value = {"supported": True, "findings": []}

        await proxy.hoster_access_scan(SERVER_ID, db=self.db, _={})

        self.assertEqual(self.kept, [(AnswerKind.HOSTER_SCAN, {"supported": True, "findings": []})])


class NetworkStateCopyTest(SilentNodeCase):
    NODE_STATE = {
        "backend": "netplan",
        "interfaces": [{"name": "eth0", "is_up": True, "is_default": True, "kind": "physical", "addresses": []}],
        "managed": [],
        "transaction": {"id": "tx-1", "status": "pending"},
        "history": [],
    }

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.fetch_state = mock.AsyncMock(side_effect=lambda _server: dict(self.NODE_STATE))
        self.patch(network_transactions, "fetch_state", self.fetch_state)
        self.patch(network_transactions, "access_address", mock.AsyncMock(return_value="10.0.0.1"))
        self.patch(network_transactions, "job_snapshot", mock.Mock(return_value={"phase": "confirming"}))

    async def state(self) -> dict:
        return await proxy.get_network_state(SERVER_ID, db=self.db, _={})

    async def test_state_of_answering_node_is_copied_without_the_panel_job(self):
        self.use(make_server(silent=False))

        state = await self.state()

        self.assertEqual(state["job"], {"phase": "confirming"})
        self.assertNotIn("saved_at", state)
        (kind, copied), = self.kept
        self.assertEqual(kind, AnswerKind.NETWORK_STATE)
        self.assertEqual(copied["access_address"], "10.0.0.1")
        self.assertNotIn("job", copied)

    async def test_silent_node_is_not_asked_and_its_copy_is_returned(self):
        self.use(make_server(silent=True))
        self.copies[AnswerKind.NETWORK_STATE] = AnswerCopy(dict(self.NODE_STATE), COPY_SAVED_AT)

        state = await self.state()

        self.fetch_state.assert_not_awaited()
        self.assertEqual(state["interfaces"], self.NODE_STATE["interfaces"])
        self.assertEqual(state["saved_at"], iso(COPY_SAVED_AT))

    async def test_copy_drops_the_old_transaction_but_keeps_the_running_panel_job(self):
        # Транзакцию из копии нода давно решила; а вот задачу панели карточка
        # должна видеть и дальше, иначе перестанет опрашивать её до конца
        self.use(make_server(silent=True))
        self.copies[AnswerKind.NETWORK_STATE] = AnswerCopy(dict(self.NODE_STATE), COPY_SAVED_AT)

        state = await self.state()

        self.assertIsNone(state["transaction"])
        self.assertEqual(state["job"], {"phase": "confirming"})

    async def test_failure_of_a_node_that_monitoring_counts_as_alive_is_reported(self):
        # Во время применения адресов связь с нодой пропадает на секунды: копия
        # в ответ на такой опрос показала бы старые адреса под видом «нода молчит»
        self.use(make_server(silent=False))
        self.copies[AnswerKind.NETWORK_STATE] = AnswerCopy(dict(self.NODE_STATE), COPY_SAVED_AT)
        self.fetch_state.side_effect = network_transactions.NodeUnreachableError("timeout", timeout=True)

        with self.assertRaises(HTTPException) as raised:
            await self.state()

        self.assertEqual(raised.exception.status_code, 504)

    async def test_without_a_copy_addresses_come_from_the_last_metrics(self):
        self.use(make_server(silent=True))
        self.latest.put(SERVER_ID, {
            "collected_at": LAST_ANSWER.timestamp(),
            "network": {"interfaces": [
                {"name": "eth0", "is_up": True, "is_virtual": False, "addresses": [
                    {"type": "ipv4", "address": "203.0.113.7", "netmask": "255.255.255.0"},
                ]},
            ]},
        })

        state = await self.state()

        self.assertTrue(state["from_metrics"])
        self.assertEqual(state["saved_at"], iso(LAST_ANSWER))
        self.assertEqual(
            [(address["address"], address["prefix"]) for address in state["interfaces"][0]["addresses"]],
            [("203.0.113.7", 24)],
        )
        self.assertEqual(self.kept, [])

    async def test_without_a_copy_and_metrics_there_is_nothing_to_show(self):
        self.use(make_server(silent=True))

        with self.assertRaises(HTTPException) as raised:
            await self.state()

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.headers, {proxy.NO_ANSWER_COPY_HEADER: "1"})


class HaproxyAndDnatPagesTest(SilentNodeCase):
    """«Настройки сервера» (HAProxy) и «DNAT-маршрутизация» молчащей ноды.

    Данные HAProxy панель и так держит в server_cache, но отдавала их без
    признака, что нода молчит: страница показывала старое как живое и билась
    в мёртвую ноду. Текст конфига и состояние DNAT не хранились вовсе.
    """

    HAPROXY_CACHE = json.dumps({
        "status": {"running": True}, "rules": {"rules": []}, "cached_at": "2026-11-15T13:10:00+00:00",
    })

    async def cached(self) -> dict:
        return await proxy.get_haproxy_cached(SERVER_ID, db=self.db, _={})

    async def test_cached_haproxy_data_say_since_when_the_node_is_silent(self):
        self.use(make_server(silent=True), haproxy_cache=self.HAPROXY_CACHE)

        data = await self.cached()

        self.assertEqual(data["status"], {"running": True})
        self.assertEqual(data["cached_at"], "2026-11-15T13:10:00+00:00")
        self.assertEqual(data["node_silent_since"], iso(LAST_ANSWER))

    async def test_cached_haproxy_data_of_answering_node_carry_no_mark(self):
        self.use(make_server(silent=False), haproxy_cache=self.HAPROXY_CACHE)

        self.assertNotIn("node_silent_since", await self.cached())

    async def test_silent_node_without_cached_haproxy_data_says_there_is_no_copy(self):
        self.use(make_server(silent=True))

        with self.assertRaises(HTTPException) as raised:
            await self.cached()

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.headers, {proxy.NO_ANSWER_COPY_HEADER: "1"})

    async def test_answering_node_without_cached_haproxy_data_is_still_warming_up(self):
        # 503 фронт повторяет сам: первый обход коллектора ещё не дошёл до ноды
        self.use(make_server(silent=False))

        with self.assertRaises(HTTPException) as raised:
            await self.cached()

        self.assertEqual(raised.exception.status_code, 503)

    async def test_haproxy_config_of_silent_node_comes_from_the_copy(self):
        self.use(make_server(silent=True))
        self.copies[AnswerKind.HAPROXY_CONFIG] = AnswerCopy({"content": "global\n", "path": "/etc/haproxy/haproxy.cfg"}, COPY_SAVED_AT)

        config = await proxy.get_haproxy_config(SERVER_ID, db=self.db, _={})

        self.node.assert_not_awaited()
        self.assertEqual(config["content"], "global\n")
        self.assertEqual(config["saved_at"], iso(COPY_SAVED_AT))

    async def test_haproxy_config_of_answering_node_is_copied(self):
        self.use(make_server(silent=False))
        self.node.return_value = {"content": "global\n", "path": "/etc/haproxy/haproxy.cfg"}

        await proxy.get_haproxy_config(SERVER_ID, db=self.db, _={})

        self.assertEqual(self.kept, [(AnswerKind.HAPROXY_CONFIG, {"content": "global\n", "path": "/etc/haproxy/haproxy.cfg"})])

    async def test_dnat_state_of_silent_node_comes_from_the_copy(self):
        self.use(make_server(silent=True))
        self.copies[AnswerKind.DNAT_STATE] = AnswerCopy({"rules": [{"name": "game"}]}, COPY_SAVED_AT)

        state = await proxy.get_dnat_state(SERVER_ID, db=self.db, _={})

        self.node.assert_not_awaited()
        self.assertEqual(state["rules"], [{"name": "game"}])
        self.assertEqual(state["saved_at"], iso(COPY_SAVED_AT))

    async def test_dnat_state_is_copied_no_more_than_once_a_minute(self):
        # Страница DNAT опрашивает состояние каждые пять секунд: переписывать
        # одну и ту же строку базы на каждый ответ незачем
        self.use(make_server(silent=False))
        self.node.return_value = {"rules": []}

        await proxy.get_dnat_state(SERVER_ID, db=self.db, _={})

        self.assertEqual(self.kept, [(AnswerKind.DNAT_STATE, {"rules": []})])
        self.assertGreaterEqual(self.keep_intervals[AnswerKind.DNAT_STATE], 60)


class DnatWritesRefreshTheCopyTest(SilentNodeCase):
    """После переприменения и снятия правил следующий ответ ноды идёт в копию сразу."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.expect_change = mock.Mock()
        self.patch(node_answer_copy, "expect_change", self.expect_change)
        self.use(make_server(silent=False))
        self.node.return_value = {"success": True}

    async def test_reapply_lets_the_next_state_into_the_copy(self):
        await proxy.reapply_dnat(SERVER_ID, db=self.db, _={})

        self.expect_change.assert_called_once_with(SERVER_ID, AnswerKind.DNAT_STATE)

    async def test_clear_lets_the_next_state_into_the_copy(self):
        self.db.execute = mock.AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: make_server(silent=False)))

        await proxy.clear_dnat(SERVER_ID, db=self.db, _={})

        self.expect_change.assert_called_once_with(SERVER_ID, AnswerKind.DNAT_STATE)


class StateFromMetricsTest(unittest.TestCase):
    """Интерфейсы из ответа с метриками — в форме состояния сети."""

    def interfaces(self, *reported: dict) -> list[dict]:
        state = network_transactions.state_from_metrics({"network": {"interfaces": list(reported)}})
        return [] if state is None else state["interfaces"]

    def test_addresses_get_prefix_and_family(self):
        eth0, = self.interfaces({"name": "eth0", "is_up": True, "addresses": [
            {"type": "ipv4", "address": "203.0.113.7", "netmask": "255.255.252.0"},
            {"type": "ipv6", "address": "2001:db8::7", "netmask": "ffff:ffff:ffff:ffff::"},
        ]})

        self.assertEqual(
            [(a["address"], a["prefix"], a["family"]) for a in eth0["addresses"]],
            [("203.0.113.7", 22, "ipv4"), ("2001:db8::7", 64, "ipv6")],
        )
        self.assertTrue(eth0["is_up"])
        self.assertEqual(eth0["kind"], "physical")

    def test_address_without_netmask_is_a_single_host(self):
        eth0, = self.interfaces({"name": "eth0", "is_up": True, "addresses": [
            {"type": "ipv4", "address": "203.0.113.7"},
            {"type": "ipv6", "address": "2001:db8::7", "netmask": None},
        ]})

        self.assertEqual([a["prefix"] for a in eth0["addresses"]], [32, 128])

    def test_virtual_interfaces_loopback_and_link_local_are_left_out(self):
        interfaces = self.interfaces(
            {"name": "lo", "is_up": True, "addresses": [{"type": "ipv4", "address": "127.0.0.1", "netmask": "255.0.0.0"}]},
            {"name": "docker0", "is_up": True, "is_virtual": True, "addresses": [{"type": "ipv4", "address": "172.17.0.1"}]},
            {"name": "eth0", "is_up": True, "addresses": [
                {"type": "ipv6", "address": "fe80::1%eth0", "netmask": "ffff:ffff:ffff:ffff::"},
                {"type": "mac", "address": "52:54:00:12:34:56"},
                {"type": "ipv4", "address": "not-an-address"},
                {"type": "ipv4", "address": "203.0.113.7", "netmask": "255.255.255.0"},
            ]},
        )

        self.assertEqual([interface["name"] for interface in interfaces], ["eth0"])
        self.assertEqual([a["address"] for a in interfaces[0]["addresses"]], ["203.0.113.7"])

    def test_nothing_useful_in_metrics_gives_no_state(self):
        for metrics in (None, {}, {"network": None}, {"network": {"interfaces": []}}, {"network": {"interfaces": ["eth0"]}}):
            with self.subTest(metrics=metrics):
                self.assertIsNone(network_transactions.state_from_metrics(metrics))

    def test_state_is_marked_as_built_from_metrics(self):
        state = network_transactions.state_from_metrics({"network": {"interfaces": [
            {"name": "eth0", "is_up": False, "addresses": []},
        ]}})

        self.assertTrue(state["supported"])
        self.assertTrue(state["from_metrics"])
        self.assertIsNone(state["transaction"])


if __name__ == "__main__":
    unittest.main()
