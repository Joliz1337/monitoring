"""Тесты хранения последних ответов нод коллектором (app/services/metrics_collector.py).

Голый unittest, без PostgreSQL и без сети: сессия базы и клиент ноды подменены.

Последний ответ ноды (~18 КБ JSON) переписывался в `servers.last_metrics` у всего
парка каждый цикл сбора — основной объём журнала WAL. Читатели берут ответ из
памяти процесса, а в базу он уходит редко и не всем парком за один цикл.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.dialects import postgresql  # noqa: E402

from app.services import metrics_collector  # noqa: E402
from app.services.latest_metrics import LatestMetrics  # noqa: E402
from app.services.metrics_collector import MetricsCollector  # noqa: E402

COLLECT_INTERVAL = 10.0


def node(server_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=server_id, name=f"node-{server_id}", url=f"https://10.0.0.{server_id}:9100",
        pki_enabled=True, proxy_url=None, api_key=None, node_capabilities=None,
        has_xray_node=False, remnawave_nginx_detected=False,
        last_seen=None, node_version="10.35.0", tracked_ports=None,
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def node_id(url: str) -> int:
    return int(url.removeprefix("https://10.0.0.").split(":")[0])


class FakeNodes:
    """Парк нод: каждая отвечает на опрос метрик новым ответом с номером опроса."""

    def __init__(self) -> None:
        self.polls = 0
        self.malformed: set[int] = set()

    async def get(self, url: str, **kwargs) -> FakeResponse:
        self.polls += 1
        cpu = None if node_id(url) in self.malformed else {"usage_percent": 12.5, "load_avg_1": 0.5}
        return FakeResponse({"cpu": cpu, "poll": self.polls})


class FakeResult:
    rowcount = 0

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows

    def scalars(self) -> "FakeResult":
        return self

    def scalar(self) -> None:
        return None

    def scalar_one_or_none(self) -> None:
        return None


class FakeDb:
    """База без PostgreSQL: отдаёт серверы и запоминает всё, что в неё пишут."""

    SAVED_ANSWERS_SQL = "SELECT servers.id, servers.last_metrics FROM servers"

    def __init__(self, servers: list[SimpleNamespace]) -> None:
        self.servers = servers
        # None — запрос сохранённых ответов падает
        self.saved_answers_rows: list[tuple[int, str | None]] | None = []
        self.statements: list[tuple[str, object]] = []
        self.failing_commits = 0

    def __call__(self) -> "FakeDb":
        return self

    async def __aenter__(self) -> "FakeDb":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def connection(self) -> "FakeDb":
        return self

    async def execute(self, statement, params=None) -> FakeResult:
        # Как executemany у SQLAlchemy: колонка, чьё имя есть среди ключей
        # параметров, попадает в SET сама, даже если в values() её не называли
        first = params[0] if isinstance(params, list) else params
        compiled = statement.compile(dialect=postgresql.dialect(), column_keys=sorted(first) if first else None)
        sql = " ".join(str(compiled).split())
        self.statements.append((sql, params))
        if sql == self.SAVED_ANSWERS_SQL:
            if self.saved_answers_rows is None:
                raise ConnectionError("database is down")
            return FakeResult(self.saved_answers_rows)
        return FakeResult(self.servers if sql.startswith("SELECT") and "FROM servers" in sql else [])

    async def commit(self) -> None:
        if self.failing_commits:
            self.failing_commits -= 1
            raise ConnectionError("database is down")

    def server_updates(self, *, with_answer: bool) -> list[tuple[str, list[dict]]]:
        """UPDATE строк серверов: переписывающие сохранённый ответ ноды или нет."""
        return [
            (sql, params) for sql, params in self.statements
            if sql.startswith("UPDATE servers") and ("last_metrics" in sql) == with_answer
        ]

    def updated_ids(self, *, with_answer: bool) -> list[int]:
        return sorted(row["sid"] for _, params in self.server_updates(with_answer=with_answer) for row in params)

    def saved_answers(self) -> dict[int, dict]:
        return {
            row["sid"]: json.loads(row["last_metrics"])
            for _, params in self.server_updates(with_answer=True) for row in params
        }


class CollectorCase(unittest.IsolatedAsyncioTestCase):
    SERVER_IDS: tuple[int, ...] = (1, 2, 3)

    async def asyncSetUp(self) -> None:
        self.clock = FakeClock()
        self.latest = LatestMetrics()
        self.nodes = FakeNodes()
        self.db = FakeDb([node(server_id) for server_id in self.SERVER_IDS])
        traffic = mock.Mock(start=mock.AsyncMock(), stop=mock.AsyncMock(), flush=mock.AsyncMock())
        traffic.observe.return_value = SimpleNamespace(rx_bytes_per_sec=0.0, tx_bytes_per_sec=0.0)
        weights_runner = mock.Mock(start=mock.AsyncMock(), stop=mock.AsyncMock())
        for name, replacement in (
            ("async_session", self.db),
            ("get_node_client", mock.Mock(return_value=self.nodes)),
            ("get_latest_metrics", mock.Mock(return_value=self.latest)),
            ("get_traffic_ingest", mock.Mock(return_value=traffic)),
            ("get_load_weights_runner", mock.Mock(return_value=weights_runner)),
        ):
            patcher = mock.patch.object(metrics_collector, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.collector = MetricsCollector(clock=self.clock)
        # Чистка истории к хранению ответов отношения не имеет
        self.collector._last_cleanup = float("inf")

    async def cycle(self) -> None:
        await self.collector._collect_all_servers()
        self.clock.now += COLLECT_INTERVAL


class LatestAnswerInMemoryTest(CollectorCase):
    async def test_answer_of_a_polled_node_is_readable_from_memory(self):
        await self.cycle()
        self.assertEqual(self.latest.get(1)["cpu"]["usage_percent"], 12.5)

    async def test_next_poll_replaces_the_answer(self):
        await self.cycle()
        first = self.latest.get(1)["poll"]
        await self.cycle()
        self.assertGreater(self.latest.get(1)["poll"], first)

    async def test_malformed_answer_does_not_replace_the_last_good_one(self):
        await self.cycle()
        good = self.latest.get(1)
        self.nodes.malformed.add(1)
        with self.assertLogs(metrics_collector.logger, level="WARNING"):
            await self.cycle()
        self.assertIs(self.latest.get(1), good)


class SavingAnswersTest(CollectorCase):
    STATUS_COLUMNS = ("last_seen", "last_error", "error_code", "node_version", "tracked_ports", "node_capabilities")

    async def test_first_answer_after_start_is_saved_to_the_database(self):
        await self.cycle()
        self.assertEqual(self.db.saved_answers(), {server_id: self.latest.get(server_id) for server_id in (1, 2, 3)})

    async def test_saved_answer_goes_in_the_same_update_as_the_status(self):
        # Второй UPDATE той же строки за цикл — лишняя версия строки в таблице
        await self.cycle()

        [(answer_sql, _)] = self.db.server_updates(with_answer=True)
        for column in self.STATUS_COLUMNS:
            self.assertIn(f"{column}=", answer_sql)
        self.assertEqual(self.db.updated_ids(with_answer=False), [])

    async def test_next_cycle_updates_status_without_rewriting_the_answer(self):
        await self.cycle()
        self.db.statements.clear()
        await self.cycle()

        self.assertEqual(self.db.updated_ids(with_answer=True), [])
        self.assertEqual(self.db.updated_ids(with_answer=False), [1, 2, 3])
        [(status_sql, _)] = self.db.server_updates(with_answer=False)
        for column in self.STATUS_COLUMNS:
            self.assertIn(f"{column}=", status_sql)

    async def test_answer_lost_with_a_failed_write_is_saved_by_the_next_cycle(self):
        self.db.failing_commits = 1
        with self.assertRaises(ConnectionError):
            await self.cycle()
        self.db.statements.clear()
        await self.cycle()

        self.assertEqual(self.db.updated_ids(with_answer=True), [1, 2, 3])

    async def test_answer_is_not_serialized_when_it_is_not_being_saved(self):
        # json.dumps ответа всего парка — сотни миллисекунд event loop на цикл
        await self.cycle()
        with mock.patch.object(metrics_collector.json, "dumps", wraps=json.dumps) as dumps:
            await self.cycle()

        serialized = [call.args[0] for call in dumps.call_args_list]
        self.assertFalse([value for value in serialized if isinstance(value, dict) and "poll" in value])

    async def test_fleet_walks_do_not_read_saved_answers_back_from_the_database(self):
        # Сбор метрик, кэш HAProxy и проверка Xray поднимают весь парк из базы
        await self.cycle()
        await self.collector._cache_haproxy_data()
        await self.collector._check_xray_on_all_servers()

        fleet_selects = [sql for sql, _ in self.db.statements if sql.startswith("SELECT") and "FROM servers" in sql]
        self.assertEqual(len(fleet_selects), 3)
        self.assertFalse([sql for sql in fleet_selects if "last_metrics" in sql])


class PanelStartTest(CollectorCase):
    SERVER_IDS = ()

    async def test_start_brings_saved_answers_back_into_memory(self):
        # До первого опроса читателям нужен ответ и тех нод, что сейчас лежат
        self.db.saved_answers_rows = [(1, json.dumps({"cpu": {"usage_percent": 70}})), (2, None)]

        await self.collector.start()
        self.addAsyncCleanup(self.collector.stop)

        self.assertEqual(self.latest.get(1), {"cpu": {"usage_percent": 70}})
        self.assertIsNone(self.latest.get(2))

    async def test_database_failure_does_not_break_the_panel_start(self):
        self.db.saved_answers_rows = None

        with self.assertLogs(metrics_collector.logger, level="ERROR"):
            await self.collector.start()
        self.addAsyncCleanup(self.collector.stop)

        self.assertTrue(self.collector._running)


class PanelStopTest(CollectorCase):
    async def test_stop_saves_the_answers_that_are_newer_than_the_database(self):
        await self.cycle()
        await self.cycle()
        self.db.statements.clear()

        await self.collector.stop()

        self.assertEqual(self.db.saved_answers(), {server_id: self.latest.get(server_id) for server_id in (1, 2, 3)})

    async def test_stop_does_not_rewrite_answers_the_database_already_has(self):
        await self.cycle()
        self.db.statements.clear()

        await self.collector.stop()

        self.assertEqual(self.db.server_updates(with_answer=True), [])

    async def test_database_failure_does_not_break_the_shutdown(self):
        await self.cycle()
        await self.cycle()
        self.db.failing_commits = 1

        with self.assertLogs(metrics_collector.logger, level="ERROR"):
            await self.collector.stop()


class StaggeredSavingTest(CollectorCase):
    SERVER_IDS = tuple(range(1, 241, 20))

    async def saved_in_each_cycle_of_one_period(self) -> list[list[int]]:
        saved_per_cycle = []
        for _ in range(int(metrics_collector.LAST_METRICS_PERSIST_SECONDS / COLLECT_INTERVAL)):
            self.db.statements.clear()
            await self.cycle()
            saved_per_cycle.append(self.db.updated_ids(with_answer=True))
        return saved_per_cycle

    async def test_every_answer_is_saved_once_per_period(self):
        await self.cycle()
        saved_per_cycle = await self.saved_in_each_cycle_of_one_period()

        saved = sorted(server_id for cycle in saved_per_cycle for server_id in cycle)
        self.assertEqual(saved, list(self.SERVER_IDS))

    async def test_fleet_is_not_saved_in_a_single_cycle(self):
        # Весь парк одним циклом — тот же залп в WAL, только раз в период
        await self.cycle()
        saved_per_cycle = await self.saved_in_each_cycle_of_one_period()

        self.assertLessEqual(max(map(len, saved_per_cycle)), len(self.SERVER_IDS) / 4)


if __name__ == "__main__":
    unittest.main()
