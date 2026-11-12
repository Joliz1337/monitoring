"""Сбор IP клиентов Remnawave на большом парке (app/services/xray_stats_collector.py).

Голый unittest, без PostgreSQL и без сети: клиент Remnawave, сессия и часы подменены.

Нода стоит опросу больше секунды, а опрашивались они по три: парк в 2000 нод обходился
минут за пятнадцать при интервале сбора в пять. Свежесть IP при этом мерили от конца
цикла, и клиенты нод, опрошенных первыми, молча пропадали из снимка «онлайн сейчас»,
с карты клиентов и из проверки аномалий.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Iterable, Iterator
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import xray_stats_collector  # noqa: E402
from app.services.remnawave_api import RemnawaveAPIError  # noqa: E402
from app.services.xray_stats_collector import (  # noqa: E402
    IP_POLL_MAX_CONCURRENCY,
    IP_POLL_SECONDS_PER_NODE,
    XrayStatsCollector,
    ip_poll_concurrency,
)

CYCLE_START = datetime(2026, 11, 12, 8, 0, 0)
COLLECTION_INTERVAL = 300
HALF_OF_THE_INTERVAL = COLLECTION_INTERVAL / 2
FORMER_CONCURRENCY = 3
TARGET_FLEET = 2000
POLLING_SECONDS = 144.4
INGEST_SECONDS = 6.5
SETTINGS = SimpleNamespace(
    api_url="https://remnawave.example", api_token="token", cookie_secret=None, anomaly_enabled=False,
)

ONLINE_ON_FIRST_NODE = 7
LONG_GONE = 8
ONLINE_ON_LAST_NODE = 9


def user_ips(user_id: int, ip: str, last_seen: datetime) -> dict:
    return {"userId": user_id, "ips": [{"ip": ip, "lastSeen": last_seen.isoformat() + "Z"}]}


def polling_seconds(node_count: int, concurrency: int) -> float:
    return node_count * IP_POLL_SECONDS_PER_NODE / concurrency


class IpPollConcurrencyTest(unittest.TestCase):
    """Сколько нод опрашивать разом, чтобы опрос парка занимал около половины интервала сбора."""

    def test_small_fleet_keeps_the_former_three(self):
        self.assertEqual(ip_poll_concurrency(52, COLLECTION_INTERVAL), FORMER_CONCURRENCY)

    def test_target_fleet_fits_half_of_the_interval_without_extra_flows(self):
        concurrency = ip_poll_concurrency(TARGET_FLEET, COLLECTION_INTERVAL)

        self.assertLessEqual(polling_seconds(TARGET_FLEET, concurrency), HALF_OF_THE_INTERVAL)
        self.assertGreater(polling_seconds(TARGET_FLEET, concurrency - 1), HALF_OF_THE_INTERVAL)

    def test_target_fleet_leaves_headroom_below_the_cap(self):
        self.assertLess(ip_poll_concurrency(TARGET_FLEET, COLLECTION_INTERVAL), IP_POLL_MAX_CONCURRENCY)

    def test_huge_fleet_or_short_interval_stop_at_the_cap(self):
        self.assertEqual(ip_poll_concurrency(TARGET_FLEET, 60), IP_POLL_MAX_CONCURRENCY)
        self.assertEqual(ip_poll_concurrency(100_000, COLLECTION_INTERVAL), IP_POLL_MAX_CONCURRENCY)

    def test_zero_and_one_node_keep_the_former_three(self):
        self.assertEqual(ip_poll_concurrency(0, COLLECTION_INTERVAL), FORMER_CONCURRENCY)
        self.assertEqual(ip_poll_concurrency(1, 60), FORMER_CONCURRENCY)

    def test_more_nodes_or_a_shorter_interval_never_slow_the_polling_down(self):
        fleets = range(0, 6000, 50)
        by_interval = {
            interval: [ip_poll_concurrency(nodes, interval) for nodes in fleets] for interval in (60, 300, 900)
        }

        for flows in by_interval.values():
            self.assertEqual(flows, sorted(flows))
            self.assertGreaterEqual(flows[0], FORMER_CONCURRENCY)
            self.assertLessEqual(flows[-1], IP_POLL_MAX_CONCURRENCY)
        for shorter, longer in ((60, 300), (300, 900)):
            self.assertTrue(all(a >= b for a, b in zip(by_interval[shorter], by_interval[longer])))


class Clock:
    """Подмена часов коллектора (datetime и time): время идёт, только когда его двигает тест."""

    def __init__(self) -> None:
        self.moment = CYCLE_START

    def now(self, tz=None) -> datetime:
        return self.moment.replace(tzinfo=tz)

    def monotonic(self) -> float:
        return (self.moment - CYCLE_START).total_seconds()

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


class ScriptedRemnawave:
    """Подмена клиента Remnawave: каждая нода отвечает в назначенный ей момент цикла —
    списком клиентов или ошибкой."""

    def __init__(self, clock: Clock, answers: dict[str, tuple[datetime, list[dict] | Exception]]) -> None:
        self._clock = clock
        self._answers = answers

    async def get_all_nodes(self) -> list[dict]:
        return [{"uuid": uuid, "isConnected": True, "isDisabled": False} for uuid in self._answers]

    async def poll_users_ips(self, node_uuid: str) -> list[dict]:
        answered_at, answer = self._answers[node_uuid]
        self._clock.moment = answered_at
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def close(self) -> None:
        pass


class CountingRemnawave:
    """Подмена клиента Remnawave: считает, сколько нод опрашивается одновременно."""

    def __init__(self, connected: int, disconnected: int = 0) -> None:
        self._nodes = [
            {"uuid": f"node-{number}", "isConnected": number < connected, "isDisabled": False}
            for number in range(connected + disconnected)
        ]
        self._in_flight = 0
        self.max_in_flight = 0

    async def get_all_nodes(self) -> list[dict]:
        return self._nodes

    async def poll_users_ips(self, _node_uuid: str) -> list[dict]:
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        await asyncio.sleep(0)
        self._in_flight -= 1
        return []

    async def close(self) -> None:
        pass


class HeldRemnawave:
    """Подмена клиента Remnawave: опрос ноды висит, пока тест его не отпустит."""

    def __init__(self) -> None:
        self.node_list_requests = 0
        self.polling = asyncio.Event()
        self.release = asyncio.Event()

    async def get_all_nodes(self) -> list[dict]:
        self.node_list_requests += 1
        return [{"uuid": "node-1", "isConnected": True, "isDisabled": False}]

    async def poll_users_ips(self, _node_uuid: str) -> list[dict]:
        self.polling.set()
        await self.release.wait()
        return []

    async def close(self) -> None:
        pass


class ActiveUsersSession:
    """Подмена async_session: на запрос кэша пользователей отдаёт ACTIVE."""

    def __init__(self, user_ids: Iterable[int]) -> None:
        self._user_ids = user_ids

    async def __aenter__(self) -> "ActiveUsersSession":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def execute(self, _statement) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: [(user_id,) for user_id in self._user_ids])


class CollectionCycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.collector = XrayStatsCollector()
        self.collector._collection_interval = COLLECTION_INTERVAL
        self.collector._get_settings = mock.AsyncMock(return_value=SETTINGS)
        self.collector._get_ignored_user_ids = mock.AsyncMock(return_value=set())
        self.collector._save_stats = mock.AsyncMock()
        self.ingest = mock.AsyncMock()

    @contextmanager
    def remnawave(self, api, active_user_ids: Iterable[int] = ()) -> Iterator[None]:
        with mock.patch.object(xray_stats_collector, "get_remnawave_api", return_value=api), \
                mock.patch.object(xray_stats_collector, "async_session", lambda: ActiveUsersSession(active_user_ids)), \
                mock.patch.object(xray_stats_collector, "ingest_cycle", self.ingest), \
                mock.patch.object(xray_stats_collector, "datetime", self.clock), \
                mock.patch.object(xray_stats_collector, "time", self.clock):
            yield

    def run_cycle(self, api, active_user_ids: Iterable[int] = ()) -> None:
        with self.remnawave(api, active_user_ids):
            asyncio.run(self.collector._collect_ips_from_api())

    @property
    def snapshot(self) -> dict:
        self.collector._save_stats.assert_awaited_once()
        return self.collector._save_stats.await_args.args[0]

    @property
    def history(self) -> dict:
        return self.ingest.await_args.args[1]


class CycleSnapshotTest(CollectionCycleTestCase):
    def test_clients_of_the_node_polled_first_stay_online_when_the_cycle_outlasts_the_interval(self):
        first_answer = CYCLE_START
        last_answer = CYCLE_START + timedelta(seconds=3 * COLLECTION_INTERVAL)
        api = ScriptedRemnawave(self.clock, {
            "polled-first": (first_answer, [
                user_ips(ONLINE_ON_FIRST_NODE, "1.1.1.1", first_answer - timedelta(seconds=10)),
                user_ips(LONG_GONE, "2.2.2.2", first_answer - timedelta(seconds=6 * COLLECTION_INTERVAL)),
            ]),
            "polled-last": (last_answer, [
                user_ips(ONLINE_ON_LAST_NODE, "3.3.3.3", last_answer - timedelta(seconds=10)),
            ]),
        })

        self.run_cycle(api, {ONLINE_ON_FIRST_NODE, LONG_GONE, ONLINE_ON_LAST_NODE})

        self.assertEqual(
            set(self.snapshot),
            {(ONLINE_ON_FIRST_NODE, "1.1.1.1"), (ONLINE_ON_LAST_NODE, "3.3.3.3")},
        )
        self.assertEqual(self.snapshot[(ONLINE_ON_FIRST_NODE, "1.1.1.1")].node_uuids, {"polled-first"})
        self.assertIn((LONG_GONE, "2.2.2.2", "polled-first"), self.history)

    def test_failed_nodes_do_not_take_the_clients_of_a_healthy_node_with_them(self):
        api = ScriptedRemnawave(self.clock, {
            "refused": (CYCLE_START, RemnawaveAPIError("IP fetch job failed")),
            "crashed": (CYCLE_START, RuntimeError("connection reset")),
            "healthy": (CYCLE_START, [user_ips(ONLINE_ON_LAST_NODE, "3.3.3.3", CYCLE_START)]),
        })

        self.run_cycle(api, {ONLINE_ON_LAST_NODE})

        self.assertEqual(set(self.snapshot), {(ONLINE_ON_LAST_NODE, "3.3.3.3")})


class PollingPaceTest(CollectionCycleTestCase):
    def polled_at_once(self, connected: int, disconnected: int = 0) -> int:
        api = CountingRemnawave(connected, disconnected)
        self.run_cycle(api)
        return api.max_in_flight

    def test_small_fleet_is_polled_three_nodes_at_once_as_before(self):
        self.assertEqual(self.polled_at_once(52), FORMER_CONCURRENCY)

    def test_target_fleet_is_polled_as_wide_as_its_size_requires(self):
        self.assertEqual(self.polled_at_once(TARGET_FLEET), ip_poll_concurrency(TARGET_FLEET, COLLECTION_INTERVAL))

    def test_shorter_collection_interval_widens_the_polling(self):
        self.collector._collection_interval = 60

        self.assertEqual(self.polled_at_once(500), ip_poll_concurrency(500, 60))

    def test_disconnected_nodes_do_not_widen_the_polling(self):
        self.assertEqual(self.polled_at_once(52, disconnected=TARGET_FLEET), FORMER_CONCURRENCY)


class OverlappingCyclesTest(CollectionCycleTestCase):
    def test_cycle_does_not_start_over_one_that_is_still_polling(self):
        # На большом парке обход идёт минутами: плановый цикл, начатый поверх ручного
        # «собрать сейчас», удвоил бы нагрузку на панель Remnawave пользователя
        async def manual_then_scheduled() -> int:
            api = HeldRemnawave()
            with self.remnawave(api):
                manual = asyncio.create_task(self.collector._collect_ips_from_api())
                await api.polling.wait()

                await asyncio.wait_for(self.collector._collect_ips_from_api(), timeout=1)
                node_list_requests = api.node_list_requests

                api.release.set()
                await manual
            return node_list_requests

        self.assertEqual(asyncio.run(manual_then_scheduled()), 1)

    def test_next_cycle_runs_once_the_previous_one_is_over(self):
        async def two_cycles_in_a_row() -> int:
            api = HeldRemnawave()
            api.release.set()
            with self.remnawave(api):
                await self.collector._collect_ips_from_api()
                await self.collector._collect_ips_from_api()
            return api.node_list_requests

        self.assertEqual(asyncio.run(two_cycles_in_a_row()), 2)

    def test_cycle_that_crashed_does_not_block_the_next_one(self):
        self.collector._get_settings = mock.AsyncMock(side_effect=[RuntimeError("database is down"), SETTINGS])

        with self.assertRaises(RuntimeError):
            self.run_cycle(CountingRemnawave(1))
        api = CountingRemnawave(1)
        self.run_cycle(api)

        self.assertEqual(api.max_in_flight, 1)


class CycleSummaryTest(CollectionCycleTestCase):
    def summary(self, api) -> list[str]:
        with self.assertLogs(xray_stats_collector.logger, level="INFO") as captured:
            self.run_cycle(api)
        return [record.getMessage() for record in captured.records]

    def test_single_summary_line_tells_how_long_the_polling_and_the_whole_cycle_took(self):
        api = ScriptedRemnawave(self.clock, {
            "polled-first": (CYCLE_START, []),
            "polled-last": (CYCLE_START + timedelta(seconds=POLLING_SECONDS), []),
        })
        self.ingest.side_effect = lambda *_args: self.clock.advance(INGEST_SECONDS)

        self.assertEqual(self.summary(api), [
            f"IP collection: 2 nodes polled in {POLLING_SECONDS:.1f}s ({FORMER_CONCURRENCY} at once, 0 failed), "
            "0 users with IPs, 0 (user,ip) pairs, 0 history rows, 0 ACTIVE in cache, "
            f"cycle {POLLING_SECONDS + INGEST_SECONDS:.1f}s"
        ])

    def test_summary_counts_the_nodes_that_did_not_answer(self):
        # Клиенты такой ноды на этот цикл выпадают из «онлайн»: без счёта в сводке
        # причину просевших цифр было бы видно только на уровне DEBUG
        api = ScriptedRemnawave(self.clock, {
            "refused": (CYCLE_START, RemnawaveAPIError("IP fetch job failed")),
            "crashed": (CYCLE_START, RuntimeError("connection reset")),
            "healthy": (CYCLE_START, []),
        })

        [line] = self.summary(api)

        self.assertIn(f"3 nodes polled in 0.0s ({FORMER_CONCURRENCY} at once, 2 failed)", line)


if __name__ == "__main__":
    unittest.main()
