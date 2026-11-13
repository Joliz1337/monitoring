"""Тесты цикла сбора на парке с медленными нодами (app/services/metrics_collector.py).

Голый unittest, без PostgreSQL и без сети: сессия базы и клиент ноды подменены.

Цикл сбора ждал ответа всех нод разом: одна нода, отвечающая дольше интервала,
задерживала снапшоты всему парку и отнимала у него такты. На живом парке из
52 нод так проходило 28 циклов из 4400 за шесть часов; на 2000 нод при той же
доле медленных ответов — каждый пятый. Цикл ждёт отставших не дольше своего
срока, а их ответы забирает цикл, который застал их готовыми.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.dialects import postgresql  # noqa: E402

from app.services import metrics_collector  # noqa: E402
from app.services.latest_metrics import LatestMetrics  # noqa: E402
from app.services.metrics_collector import CollectionReport, CyclePhases, MetricsCollector  # noqa: E402

# Срок ожидания отставших — доля интервала: короткий интервал держит тесты быстрыми
COLLECT_INTERVAL = 0.1
CYCLE_MUST_FINISH_IN = 1.0


def node(server_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=server_id, name=f"node-{server_id}", url=f"https://10.0.0.{server_id}:9100",
        pki_enabled=True, proxy_url=None, api_key=None, node_capabilities=None,
        has_xray_node=False, remnawave_nginx_detected=False,
        last_seen=None, node_version="10.35.1", tracked_ports=None,
    )


def node_id(url: str) -> int:
    return int(url.removeprefix("https://10.0.0.").split(":")[0])


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeResponse:
    status_code = 200

    def json(self) -> dict:
        return {"cpu": {"usage_percent": 12.5, "load_avg_1": 0.5}}


class FakeNodes:
    """Парк нод: молчащая нода не отвечает, пока её не отпустят."""

    def __init__(self) -> None:
        self.asked: list[int] = []
        self.dropped: list[int] = []
        self._silent: dict[int, asyncio.Event] = {}

    def silence(self, server_id: int) -> None:
        self._silent[server_id] = asyncio.Event()

    def release(self, server_id: int) -> None:
        self._silent.pop(server_id).set()

    async def get(self, url: str, **kwargs) -> FakeResponse:
        server_id = node_id(url)
        self.asked.append(server_id)
        gate = self._silent.get(server_id)
        if gate is not None:
            try:
                await gate.wait()
            except asyncio.CancelledError:
                self.dropped.append(server_id)
                raise
        return FakeResponse()


class FakeResult:
    rowcount = 0

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> "FakeResult":
        return self

    def all(self) -> list:
        return self._rows


class FakeDb:
    """База без PostgreSQL: отдаёт серверы и запоминает, чьи снапшоты записал каждый цикл."""

    def __init__(self, servers: list[SimpleNamespace]) -> None:
        self.servers = servers
        self.snapshot_batches: list[list[int]] = []

    def __call__(self) -> "FakeDb":
        return self

    async def __aenter__(self) -> "FakeDb":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def connection(self) -> "FakeDb":
        return self

    async def execute(self, statement, params=None) -> FakeResult:
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if sql.startswith("INSERT INTO metrics_snapshots"):
            self.snapshot_batches.append(sorted(row["server_id"] for row in params))
        return FakeResult(self.servers if sql.startswith("SELECT") else [])

    async def commit(self) -> None:
        return None


class StragglerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = FakeClock()
        self.nodes = FakeNodes()
        self.db = FakeDb([node(1), node(2), node(3)])
        traffic = mock.Mock(start=mock.AsyncMock(), stop=mock.AsyncMock(), flush=mock.AsyncMock())
        traffic.observe.return_value = SimpleNamespace(rx_bytes_per_sec=0.0, tx_bytes_per_sec=0.0)
        weights_runner = mock.Mock(start=mock.AsyncMock(), stop=mock.AsyncMock())
        for name, replacement in (
            ("async_session", self.db),
            ("get_node_client", mock.Mock(return_value=self.nodes)),
            ("get_latest_metrics", mock.Mock(return_value=LatestMetrics())),
            ("get_traffic_ingest", mock.Mock(return_value=traffic)),
            ("get_load_weights_runner", mock.Mock(return_value=weights_runner)),
        ):
            patcher = mock.patch.object(metrics_collector, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.collector = MetricsCollector(clock=self.clock)
        self.collector._collect_interval = COLLECT_INTERVAL
        # Чистка истории к ожиданию нод отношения не имеет
        self.collector._last_cleanup = float("inf")
        self.addAsyncCleanup(self.collector.stop)

    async def cycle(self) -> None:
        try:
            await asyncio.wait_for(self.collector._collect_all_servers(), CYCLE_MUST_FINISH_IN)
        except asyncio.TimeoutError:
            self.fail("the cycle waited for the silent node")
        self.clock.now += COLLECT_INTERVAL

    def close_report_window(self) -> None:
        self.clock.now += MetricsCollector.SLOW_CYCLE_REPORT_SECONDS
        self.collector._note_cycle_duration(COLLECT_INTERVAL / 2)


class SilentNodeTest(StragglerCase):
    async def test_cycle_stores_the_rest_of_the_fleet_without_waiting_for_a_silent_node(self):
        self.nodes.silence(3)
        await self.cycle()
        self.assertEqual(self.db.snapshot_batches, [[1, 2]])

    async def test_silent_node_is_not_asked_again_while_its_poll_is_in_flight(self):
        self.nodes.silence(3)
        await self.cycle()
        await self.cycle()
        self.assertEqual(self.nodes.asked.count(3), 1)

    async def test_late_answer_is_stored_by_the_cycle_that_finds_it_ready(self):
        self.nodes.silence(3)
        await self.cycle()
        self.nodes.release(3)
        await self.cycle()
        self.assertEqual(self.db.snapshot_batches, [[1, 2], [1, 2, 3]])

    async def test_node_is_asked_again_once_its_late_answer_is_taken(self):
        self.nodes.silence(3)
        await self.cycle()
        self.nodes.release(3)
        await self.cycle()
        await self.cycle()
        self.assertEqual(self.nodes.asked.count(3), 2)
        self.assertEqual(self.db.snapshot_batches[-1], [1, 2, 3])

    async def test_poll_of_a_removed_server_is_dropped(self):
        self.nodes.silence(3)
        await self.cycle()
        self.db.servers = [server for server in self.db.servers if server.id != 3]
        await self.cycle()
        await asyncio.sleep(0)
        self.assertEqual(self.nodes.dropped, [3])

    async def test_stopping_the_collector_drops_polls_in_flight(self):
        self.nodes.silence(3)
        await self.cycle()
        await self.collector.stop()
        self.assertEqual(self.nodes.dropped, [3])


class LatePollsInLogTest(StragglerCase):
    async def test_node_that_answered_late_is_named_in_the_window_report(self):
        self.nodes.silence(3)
        await self.cycle()
        self.nodes.release(3)
        await self.cycle()

        with self.assertLogs(metrics_collector.logger, level="WARNING") as captured:
            self.close_report_window()

        [report] = [record.getMessage() for record in captured.records]
        self.assertIn("node-3: 1", report)

    async def test_fleet_that_answers_in_time_leaves_the_log_silent(self):
        await self.cycle()
        await self.cycle()
        with self.assertNoLogs(metrics_collector.logger, level="WARNING"):
            self.close_report_window()


class BusyEventLoopTest(StragglerCase):
    """Такт, начатый позже своей отметки, — след занятого event loop панели.

    В длительность цикла такое опоздание не входит: цикл просыпается поздно, а
    сам проходит быстро. Без отдельного счёта оно выглядит как редкие точки на
    графиках без единой строки в логе.
    """

    async def test_tick_started_late_gets_into_the_window_report(self):
        self.collector._running = True
        with mock.patch.object(CollectionReport, "LATE_TICK_SECONDS", COLLECT_INTERVAL / 5):
            loop_task = asyncio.create_task(self.collector._collection_loop())
            await asyncio.sleep(COLLECT_INTERVAL * 1.5)
            # Сборщик спит до следующей отметки, а event loop занят дольше, чем до неё
            time.sleep(COLLECT_INTERVAL)
            await asyncio.sleep(COLLECT_INTERVAL)
            self.collector._running = False
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)

            with self.assertLogs(metrics_collector.logger, level="WARNING") as captured:
                self.close_report_window()

        self.assertTrue(any("ticks started late" in record.getMessage() for record in captured.records))


class CollectionReportTest(unittest.TestCase):
    WINDOW = 600.0
    INTERVAL = 5.0

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.report = CollectionReport(self.WINDOW, self.clock)

    def close_window(self) -> list[str]:
        self.clock.now += self.WINDOW
        return self.report.note_cycle(1.0, self.INTERVAL)

    def test_late_polls_are_counted_per_node_with_the_longest_one(self):
        for seconds in (5.2, 10.0, 6.1):
            self.report.note_late_poll("node-3", seconds)
        self.report.note_late_poll("node-7", 5.5)

        [line] = self.close_window()

        self.assertIn("4 in the last 10 min", line)
        self.assertIn("node-3: 3 (up to 10.0s)", line)
        self.assertIn("node-7: 1 (up to 5.5s)", line)

    def test_only_the_worst_nodes_are_named_on_a_big_fleet(self):
        for number in range(CollectionReport.NAMED_NODES + 4):
            self.report.note_late_poll(f"node-{number}", 6.0)

        [line] = self.close_window()

        self.assertEqual(line.count("(up to"), CollectionReport.NAMED_NODES)
        self.assertIn("and 4 more", line)

    def test_late_polls_are_not_carried_into_the_next_window(self):
        self.report.note_late_poll("node-3", 6.0)
        self.close_window()
        self.assertEqual(self.close_window(), [])

    def test_slow_cycle_report_says_where_the_time_went(self):
        self.report.note_cycle(9.0, self.INTERVAL, CyclePhases(nodes=3.5, cleanup=4.0))

        [line] = self.close_window()

        self.assertIn("slowest 9.0s", line)
        self.assertIn("nodes 3.5s", line)
        self.assertIn("storing 1.5s", line)
        self.assertIn("cleanup 4.0s", line)

    def test_ticks_started_late_are_counted_with_the_worst_delay(self):
        for delay in (0.01, 2.4, 3.8):
            self.report.note_tick_delay(delay)

        [line] = self.close_window()

        self.assertIn("ticks started late: 2 in the last 10 min", line)
        self.assertIn("worst 3.8s", line)

    def test_punctual_ticks_leave_the_report_silent(self):
        for delay in (0.0, 0.02, 0.3):
            self.report.note_tick_delay(delay)
        self.assertEqual(self.close_window(), [])


if __name__ == "__main__":
    unittest.main()
