"""Tests for the runner that pushes disabled addresses to relays.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Исполнитель будит каждый цикл сбора метрик: запрос на релей без нужды — нагрузка
на весь парк, пропущенный — сервер, который нода вернула по сроку, пока панель
считала его отключённым. Перезапуск панели не должен ни вернуть всё разом, ни
прислать уже известные отключения второй раз.
"""

import asyncio
import json
import os
import sys
import unittest
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

from app.models import Server  # noqa: E402
from app.services import backend_quarantine_runner as runner_module  # noqa: E402
from app.services.backend_quarantine import (  # noqa: E402
    Event,
    EventKind,
    Mode,
    Pair,
    PairState,
    ReasonKind,
    RestoreCause,
)
from app.services.backend_quarantine_runner import (  # noqa: E402
    NOTIFY_DELAY_SEC,
    QuarantineRunner,
    pairs_delete,
    pairs_select,
)
from app.services.backend_quarantine_settings import QuarantineSettings, parse_settings  # noqa: E402
from app.services.loss_registry import LossRegistry  # noqa: E402

RELAY_ID = 10
TARGET = "10.0.0.5:443"
PATH = "/api/haproxy/runtime/quarantine"


def relay(server_id: int = RELAY_ID, node_version: str = "10.36.0", online: bool = True,
          capabilities: str | None = None) -> Server:
    seen = datetime.now(timezone.utc) - (timedelta() if online else timedelta(hours=1))
    return Server(id=server_id, name=f"relay{server_id}", url=f"https://relay{server_id}.example:9100", api_key="key",
                  node_version=node_version, last_seen=seen, node_capabilities=capabilities,
                  pki_enabled=False, proxy_url=None, is_active=True)


def settings(mode: str = "enforce", **fields) -> QuarantineSettings:
    return parse_settings(json.dumps({"mode": mode, **fields}))


class FakeClock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeStorage:
    def __init__(self, *servers: Server):
        self.servers = {server.id: server for server in servers}
        self.rows: dict[tuple[int, str], dict] = {}
        self.server_loads = 0
        self.saves = 0
        self.stored_settings = QuarantineSettings()

    async def load_servers(self, server_ids: set[int]) -> dict[int, Server]:
        self.server_loads += 1
        return {server_id: self.servers[server_id] for server_id in server_ids if server_id in self.servers}

    async def load_pairs(self) -> dict[tuple[int, str], dict]:
        return {key: dict(row) for key, row in self.rows.items()}

    async def save_pairs(self, rows: dict[tuple[int, str], dict]) -> None:
        self.saves += 1
        self.rows = {key: dict(row) for key, row in rows.items()}

    async def load_settings(self) -> QuarantineSettings:
        return self.stored_settings


class RunnerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = FakeClock()
        self.registry = LossRegistry(clock=self.clock)
        self.storage = FakeStorage(relay())
        self.requests: list[httpx.Request] = []
        self.notified: list[Event] = []
        self.status = 200
        self.skip_reason: str | None = None
        self.network_down: set[str] = set()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.node))
        self.runner = self.make_runner()
        self.runner.set_settings(settings())

    async def asyncTearDown(self):
        await self.client.aclose()

    def node(self, request: httpx.Request) -> httpx.Response:
        if request.url.host in self.network_down:
            raise httpx.ConnectError("unreachable", request=request)
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "refused"})
        results = []
        for target in json.loads(request.content)["targets"]:
            applied = [] if self.skip_reason else [{"backend": "backend_tcp_lb", "server": "srv1", "sessions": 7}]
            skipped = [{"backend": "backend_tcp_lb", "reason": self.skip_reason}] if self.skip_reason else []
            results.append({**target, "found": True, "applied": applied, "skipped": skipped})
        return httpx.Response(200, json={"results": results})

    def make_runner(self) -> QuarantineRunner:
        async def notifier(events: list[Event], current: QuarantineSettings) -> None:
            self.notified += events

        # Без задержки сводки: что и когда решил исполнитель, видно сразу после цикла
        return QuarantineRunner(
            storage=self.storage, client_factory=lambda server: self.client, clock=self.clock,
            registry=self.registry, notifier=notifier, notify_delay=0.0,
        )

    def observe(self, *entries: tuple[str, float, int], server_id: int = RELAY_ID) -> None:
        """Замеры релея: (ip, потери, падения)."""
        self.registry.update(server_id, f"relay{server_id}", {"loss_probe": [
            {"ip": ip, "port": 443, "loss_pct": loss, "rtt_ms": 40.0, "samples": 60, "downs": downs}
            for ip, loss, downs in entries
        ]})

    async def cycle(self, advance: float = 0.0) -> None:
        self.clock.now += advance
        # Реестр считает снимок свежим 90 с: релей отвечает панели каждый цикл
        for snapshot in list(self.registry._snapshots.values()):
            self.registry._snapshots[snapshot.server_id] = type(snapshot)(
                snapshot.server_id, snapshot.name, snapshot.readings, snapshot.addresses, self.clock.now,
            )
        await self.runner.run_once()

    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]

    def state(self, server_id: int = RELAY_ID, target: str = TARGET) -> PairState | None:
        pair = self.runner.tracker.pairs.get((server_id, target))
        return pair.state if pair else None


class IdleTests(RunnerTestCase):
    async def test_switched_off_runner_touches_nothing(self):
        self.runner.set_settings(settings("off"))
        self.observe(("10.0.0.5", 80.0, 9))
        await self.cycle()
        self.assertEqual((self.requests, self.storage.server_loads, self.storage.saves, self.notified), ([], 0, 0, []))

    async def test_healthy_fleet_costs_no_requests(self):
        self.observe(("10.0.0.5", 0.0, 0), ("10.0.0.6", 1.0, 0))
        for _ in range(5):
            await self.cycle(advance=10)
        self.assertEqual((self.requests, self.storage.server_loads), ([], 0))


class PushTests(RunnerTestCase):
    async def test_decision_goes_to_the_relay_and_its_answer_disables(self):
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        [request] = self.requests
        self.assertEqual((request.method, request.url.host, request.url.path), ("PUT", "relay10.example", PATH))
        self.assertEqual(json.loads(request.content), {
            "targets": [{"ip": "10.0.0.5", "port": 443}], "ttl_seconds": 180,
            "max_share_pct": 50, "drop_sessions": True,
        })
        self.assertEqual(self.state(), PairState.DISABLED)
        [event] = self.notified
        self.assertEqual((event.kind, event.target, event.sessions, event.term_sec), (EventKind.DISABLED, TARGET, 7, 600))

    async def test_panel_settings_travel_with_the_list(self):
        self.runner.set_settings(settings(max_share_pct=30, drop_sessions=False))
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        body = self.bodies()[0]
        self.assertEqual((body["max_share_pct"], body["drop_sessions"]), (30, False))

    async def test_unchanged_list_is_resent_once_a_minute(self):
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        for _ in range(5):
            await self.cycle(advance=10)
        self.assertEqual(len(self.requests), 1)
        await self.cycle(advance=10)
        self.assertEqual(len(self.requests), 2)

    async def test_changed_list_goes_at_once_worst_first(self):
        self.observe(("10.0.0.5", 0.0, 3), ("10.0.0.6", 0.0, 0), ("10.0.0.7", 0.0, 0), ("10.0.0.8", 0.0, 0))
        await self.cycle()
        self.observe(("10.0.0.5", 0.0, 3), ("10.0.0.6", 40.0, 3), ("10.0.0.7", 0.0, 0), ("10.0.0.8", 0.0, 0))
        await self.cycle(advance=10)
        self.assertEqual(self.bodies()[1]["targets"], [{"ip": "10.0.0.6", "port": 443}, {"ip": "10.0.0.5", "port": 443}])

    async def test_changed_settings_go_at_once(self):
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        self.runner.set_settings(settings(max_share_pct=30))
        await self.cycle(advance=10)
        self.assertEqual([body["max_share_pct"] for body in self.bodies()], [50, 30])

    async def test_requested_push_goes_at_once(self):
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        self.runner.request_push(RELAY_ID)
        await self.cycle(advance=10)
        self.assertEqual(len(self.requests), 2)
        await self.cycle(advance=10)
        self.assertEqual(len(self.requests), 2)

    async def test_requested_push_for_a_relay_without_disables_is_ignored(self):
        self.runner.request_push(RELAY_ID)
        await self.cycle()
        self.assertEqual(self.requests, [])

    async def test_returned_address_empties_the_list_once(self):
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        self.observe(("10.0.0.5", 0.0, 0))
        for _ in range(80):
            await self.cycle(advance=10)
        self.assertEqual([body["targets"] for body in self.bodies()[-2:]],
                         [[{"ip": "10.0.0.5", "port": 443}], []])
        sent = len(self.requests)
        for _ in range(12):
            await self.cycle(advance=10)
        self.assertEqual(len(self.requests), sent)
        self.assertEqual([event.kind for event in self.notified], [EventKind.DISABLED, EventKind.RESTORED])

    async def test_ipv6_target_is_split_at_the_last_colon(self):
        self.registry.update(RELAY_ID, "relay10", {"loss_probe": [
            {"ip": "2001:db8::1", "port": 8443, "loss_pct": 0.0, "rtt_ms": 40.0, "samples": 60, "downs": 3},
        ]})
        await self.cycle()
        self.assertEqual(self.bodies()[0]["targets"], [{"ip": "2001:db8::1", "port": 8443}])


class RefusalTests(RunnerTestCase):
    async def blocked_with(self) -> str | None:
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        pair = self.runner.tracker.pairs[(RELAY_ID, TARGET)]
        self.assertEqual(pair.state, PairState.BLOCKED)
        return pair.node.get("error")

    async def test_node_guard_blocks_without_notice(self):
        self.skip_reason = "last_server"
        self.assertEqual(await self.blocked_with(), "last_server")
        self.assertEqual(self.notified, [])

    async def test_blocked_pair_applies_when_the_node_allows(self):
        self.skip_reason = "share_limit"
        await self.blocked_with()
        self.skip_reason = None
        await self.cycle(advance=60)
        self.assertEqual(self.state(), PairState.DISABLED)

    async def test_old_node_is_not_asked(self):
        self.storage.servers[RELAY_ID] = relay(node_version="10.35.0")
        self.assertEqual(await self.blocked_with(), "unsupported")
        self.assertEqual(self.requests, [])

    async def test_offline_node_is_not_asked(self):
        self.storage.servers[RELAY_ID] = relay(online=False)
        self.assertEqual(await self.blocked_with(), "offline")
        self.assertEqual(self.requests, [])

    async def test_node_closed_for_haproxy_writes_is_not_asked(self):
        self.storage.servers[RELAY_ID] = relay(capabilities='{"haproxy":"ro"}')
        self.assertEqual(await self.blocked_with(), "denied")
        self.assertEqual(self.requests, [])

    async def test_deleted_relay_is_offline(self):
        del self.storage.servers[RELAY_ID]
        self.assertEqual(await self.blocked_with(), "offline")

    async def test_404_marks_the_node_for_ten_minutes(self):
        self.status = 404
        self.assertEqual(await self.blocked_with(), "unsupported")
        for _ in range(59):
            await self.cycle(advance=10)
        self.assertEqual(len(self.requests), 1)
        self.status = 200
        await self.cycle(advance=10)
        self.assertEqual((len(self.requests), self.state()), (2, PairState.DISABLED))

    async def test_failing_relay_is_retried_once_a_minute(self):
        self.status = 503
        await self.blocked_with()
        for _ in range(5):
            await self.cycle(advance=10)
        self.assertEqual((len(self.requests), self.storage.server_loads), (1, 1))
        await self.cycle(advance=10)
        self.assertEqual(len(self.requests), 2)

    async def test_requested_push_does_not_wait_for_the_retry(self):
        self.status = 503
        await self.blocked_with()
        self.status = 200
        self.runner.request_push(RELAY_ID)
        await self.cycle(advance=10)
        self.assertEqual(self.state(), PairState.DISABLED)

    async def test_403_is_denied(self):
        self.status = 403
        self.assertEqual(await self.blocked_with(), "denied")

    async def test_node_error_status_is_error(self):
        self.status = 503
        self.assertEqual(await self.blocked_with(), "error")

    async def test_unreachable_relay_does_not_stall_the_others(self):
        self.storage.servers[11] = relay(11)
        self.network_down.add("relay10.example")
        self.observe(("10.0.0.5", 0.0, 3))
        self.observe(("10.0.0.5", 0.0, 3), server_id=11)
        await self.cycle()
        self.assertEqual((self.state(RELAY_ID), self.state(11)), (PairState.BLOCKED, PairState.DISABLED))
        self.assertEqual(self.runner.tracker.pairs[(RELAY_ID, TARGET)].node, {"error": "offline"})


class ObserveModeTests(RunnerTestCase):
    async def test_reports_without_touching_relays(self):
        self.runner.set_settings(settings("observe"))
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        self.assertEqual((self.requests, self.storage.server_loads), ([], 0))
        [event] = self.notified
        self.assertEqual((event.kind, event.observe), (EventKind.DISABLED, True))


class DigestTests(RunnerTestCase):
    """Адреса одного сервера отключаются в соседних циклах: без задержки это пачка
    сообщений подряд, с задержкой — одна сводка."""

    CALM = tuple((f"10.0.0.{i}", 0.0, 0) for i in range(7, 10))

    async def asyncSetUp(self):
        self.digests: list[list[Event]] = []
        await super().asyncSetUp()

    def make_runner(self) -> QuarantineRunner:
        async def notifier(events: list[Event], current: QuarantineSettings) -> None:
            self.digests.append(list(events))

        return QuarantineRunner(
            storage=self.storage, client_factory=lambda server: self.client, clock=self.clock,
            registry=self.registry, notifier=notifier,
        )

    async def disable(self, *ips: str, advance: float = 0.0) -> None:
        self.observe(*((ip, 0.0, 3) for ip in ips), *self.CALM)
        await self.cycle(advance=advance)

    def targets(self) -> list[list[str]]:
        return [[event.target for event in digest] for digest in self.digests]

    async def test_events_of_neighbouring_cycles_go_as_one_digest(self):
        await self.disable("10.0.0.5")
        await self.disable("10.0.0.5", "10.0.0.6", advance=10)
        await self.cycle(advance=NOTIFY_DELAY_SEC - 11)
        self.assertEqual(self.digests, [])
        await self.cycle(advance=1)
        self.assertEqual(self.targets(), [["10.0.0.5:443", "10.0.0.6:443"]])

    async def test_digest_is_sent_once(self):
        await self.disable("10.0.0.5")
        for _ in range(30):
            await self.cycle(advance=10)
        self.assertEqual(len(self.digests), 1)

    async def test_next_digest_waits_for_its_own_delay(self):
        await self.disable("10.0.0.5")
        await self.cycle(advance=NOTIFY_DELAY_SEC)
        await self.disable("10.0.0.5", "10.0.0.6", advance=10)
        await self.cycle(advance=NOTIFY_DELAY_SEC - 1)
        self.assertEqual(self.targets(), [["10.0.0.5:443"]])
        await self.cycle(advance=1)
        self.assertEqual(self.targets(), [["10.0.0.5:443"], ["10.0.0.6:443"]])

    async def test_manual_release_joins_the_digest(self):
        await self.disable("10.0.0.5")
        self.clock.now += 10
        await self.runner.release(RELAY_ID, TARGET)
        self.assertEqual(self.digests, [])
        await self.cycle(advance=NOTIFY_DELAY_SEC - 10)
        self.assertEqual([[(e.kind, e.cause) for e in digest] for digest in self.digests],
                         [[(EventKind.DISABLED, None), (EventKind.RESTORED, RestoreCause.MANUAL)]])

    async def test_stopping_the_runner_sends_what_is_waiting(self):
        # Перезапуск панели: о накопленном после него никто не вспомнит
        await self.disable("10.0.0.5")
        await self.runner.stop()
        self.assertEqual(self.targets(), [["10.0.0.5:443"]])
        await self.runner.stop()
        self.assertEqual(len(self.digests), 1)

    def runner_with(self, notifier) -> QuarantineRunner:
        runner = QuarantineRunner(
            storage=self.storage, client_factory=lambda server: self.client, clock=self.clock,
            registry=self.registry, notifier=notifier, notify_delay=0.0,
        )
        runner.set_settings(settings())
        return runner

    async def test_digest_cut_short_by_a_stop_is_sent_by_the_stop(self):
        telegram_hangs = True

        async def notifier(events: list[Event], current: QuarantineSettings) -> None:
            if telegram_hangs:
                await asyncio.Event().wait()
            self.digests.append(list(events))

        self.runner = self.runner_with(notifier)
        self.observe(("10.0.0.5", 0.0, 3), *self.CALM)
        cycle = asyncio.create_task(self.cycle())
        await asyncio.sleep(0.05)
        cycle.cancel()
        with suppress(asyncio.CancelledError):
            await cycle
        telegram_hangs = False
        await self.runner.stop()
        self.assertEqual(self.targets(), [["10.0.0.5:443"]])

    async def test_stop_does_not_wait_for_telegram_forever(self):
        async def notifier(events: list[Event], current: QuarantineSettings) -> None:
            await asyncio.Event().wait()

        self.runner = self.runner_with(notifier)
        self.runner._notify_delay = NOTIFY_DELAY_SEC
        self.observe(("10.0.0.5", 0.0, 3), *self.CALM)
        await self.cycle()
        with mock.patch.object(runner_module, "STOP_NOTIFY_TIMEOUT_SEC", 0.05):
            await asyncio.wait_for(self.runner.stop(), 2)

    async def test_decided_events_survive_a_failed_push(self):
        # Адрес вернулся, а база в этот момент не ответила: сообщение о возврате не теряется
        async def notifier(events: list[Event], current: QuarantineSettings) -> None:
            self.digests.append(list(events))

        async def database_is_away(server_ids: set[int]) -> dict:
            raise OSError("database is away")

        self.runner = self.runner_with(notifier)
        self.observe(("10.0.0.5", 0.0, 3), *self.CALM)
        await self.cycle()
        self.observe(("10.0.0.5", 0.0, 0), *self.CALM)
        self.storage.load_servers = database_is_away
        with self.assertLogs(runner_module.logger, level="ERROR"):
            for _ in range(70):
                await self.cycle(advance=10)
        kinds = [event.kind for digest in self.digests for event in digest]
        self.assertEqual(kinds, [EventKind.DISABLED, EventKind.RESTORED])


class ModeChangeTests(RunnerTestCase):
    async def test_switching_off_returns_everything(self):
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        self.runner.set_settings(settings("off"))
        await self.cycle(advance=10)
        self.assertEqual(self.bodies()[-1]["targets"], [])
        self.assertEqual(self.runner.tracker.desired(), {})
        self.assertEqual([(e.kind, e.cause) for e in self.notified],
                         [(EventKind.DISABLED, None), (EventKind.RESTORED, RestoreCause.MODE)])

    async def test_switching_to_observe_returns_everything(self):
        self.observe(("10.0.0.5", 0.0, 0))
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        self.runner.set_settings(settings("observe"))
        await self.cycle(advance=10)
        self.assertEqual(self.bodies()[-1]["targets"], [])

    async def test_release_that_cannot_be_delivered_is_dropped_after_the_node_lease(self):
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        self.runner.set_settings(settings("off"))
        self.network_down.add("relay10.example")
        for _ in range(17):
            await self.cycle(advance=10)
        loads = self.storage.server_loads
        # Срок на ноде (180 с) вышел: она вернула сервер сама, слать больше нечего
        for _ in range(6):
            await self.cycle(advance=10)
        self.assertEqual(self.storage.server_loads, loads + 1)


class ManualReleaseTests(RunnerTestCase):
    async def test_release_returns_the_address_now(self):
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        self.assertTrue(await self.runner.release(RELAY_ID, TARGET))
        await self.cycle(advance=1)
        self.assertEqual(self.bodies()[-1]["targets"], [])
        self.assertEqual(self.notified[-1].cause, RestoreCause.MANUAL)

    async def test_release_of_an_unknown_pair_is_refused(self):
        self.assertFalse(await self.runner.release(RELAY_ID, TARGET))
        self.assertEqual(self.notified, [])


class StorageTests(RunnerTestCase):
    async def test_state_is_saved_when_it_changes_only(self):
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        saved = self.storage.saves
        self.assertGreaterEqual(saved, 1)
        self.assertEqual(self.storage.rows[(RELAY_ID, TARGET)]["state"], "disabled")
        for _ in range(5):
            await self.cycle(advance=10)
        self.assertEqual(self.storage.saves, saved)

    async def test_restarted_panel_keeps_the_disable_quietly(self):
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        self.storage.stored_settings = settings()
        self.runner = self.make_runner()
        await self.runner.restore()
        self.notified.clear()
        self.requests.clear()
        self.observe(("10.0.0.5", 20.0, 0))
        await self.cycle(advance=30)
        self.assertEqual(self.state(), PairState.DISABLED)
        # Нода могла вернуть сервер за время простоя панели — набор уходит ей сразу
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.notified, [])

    async def test_restarted_panel_does_not_return_everything(self):
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        self.storage.stored_settings = settings()
        self.runner = self.make_runner()
        await self.runner.restore()
        self.requests.clear()
        await self.cycle(advance=30)
        self.assertEqual(self.bodies()[0]["targets"], [{"ip": "10.0.0.5", "port": 443}])

    async def test_restart_with_auto_disable_switched_off_returns_what_was_disabled(self):
        # Режим выключили, а состояние сохранить не успели: в базе пара всё ещё «отключена»
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        self.storage.stored_settings = settings("off")
        self.runner = self.make_runner()
        await self.runner.restore()
        self.requests.clear()
        self.notified.clear()
        await self.cycle(advance=30)
        self.assertEqual([body["targets"] for body in self.bodies()], [[]])
        self.assertEqual(self.runner.tracker.desired(), {})
        self.assertEqual([(e.kind, e.cause) for e in self.notified], [(EventKind.RESTORED, RestoreCause.MODE)])
        self.assertNotEqual(self.storage.rows.get((RELAY_ID, TARGET), {}).get("state"), "disabled")
        await self.cycle(advance=60)
        self.assertEqual(len(self.requests), 1)

    async def test_restart_in_observe_mode_does_not_keep_servers_disabled(self):
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        self.storage.stored_settings = settings("observe")
        self.runner = self.make_runner()
        await self.runner.restore()
        self.requests.clear()
        self.observe(("10.0.0.5", 0.0, 0))
        await self.cycle(advance=30)
        await self.cycle(advance=60)
        self.assertEqual([body["targets"] for body in self.bodies()], [[]])

    async def test_failed_restore_is_retried(self):
        self.storage.stored_settings = settings()
        original = self.storage.load_settings
        failures = [OSError("database is not ready")]

        async def flaky():
            if failures:
                raise failures.pop()
            return await original()

        self.storage.load_settings = flaky
        runner = self.make_runner()
        with self.assertLogs(runner_module.logger, level="ERROR"):
            self.assertFalse(await runner.ensure_restored())
        self.assertTrue(await runner.ensure_restored())
        self.assertEqual(runner.view({})["mode"], "enforce")

    async def test_damaged_row_is_skipped(self):
        self.storage.rows[(RELAY_ID, TARGET)] = {"state": "disabled"}
        with self.assertLogs(runner_module.logger, level="WARNING"):
            await self.runner.restore()
        self.assertEqual(self.runner.tracker.pairs, {})


class ClientProbeRequestTests(RunnerTestCase):
    async def test_nodes_are_asked_to_probe_only_when_the_owner_switched_it_on(self):
        self.assertIs(self.runner.client_probe_wanted(), False)
        self.runner.set_settings(settings(client_probe=True))
        self.assertIs(self.runner.client_probe_wanted(), True)

    async def test_silent_server_is_disabled_and_shown_with_its_reason(self):
        self.registry.update(RELAY_ID, "relay10", {"loss_probe": [
            {"ip": "10.0.0.5", "port": 443, "loss_pct": 2.0, "rtt_ms": 40.0, "samples": 60, "downs": 0,
             "stalled": None, "answers": False},
        ]})
        await self.cycle()
        for _ in range(3):
            await self.cycle(advance=60)
        [item] = self.runner.view({})["items"]
        self.assertEqual((item["state"], item["reason"]["kind"]), ("disabled", "no_reply"))


class ViewTests(RunnerTestCase):
    async def test_view_lists_pairs_with_reason_and_term(self):
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        view = self.runner.view({"10.0.0.5": "nl-3"})
        self.assertEqual(view["mode"], "enforce")
        self.assertEqual(view["items"], [{
            "target": TARGET, "ip": "10.0.0.5", "port": 443, "owner": "nl-3",
            "server_id": RELAY_ID, "server_name": "relay10", "state": "disabled",
            "reason": {"kind": "downs", "loss_pct": 0.0, "downs": 3},
            "disabled_at": self.clock.now, "until": self.clock.now + 600,
            "strikes": 1, "sessions": 7, "blocked_reason": None, "partial_reason": None,
        }])
        self.assertEqual((view["chronic"], view["paused_relays"]), ([], []))

    async def test_chronic_address_is_listed_with_its_owner_even_when_back_in_service(self):
        # Адрес уже вернули, но сегодня его отключали четыре раза: строке на странице нужны ip, порт и владелец
        now = self.clock.now
        self.runner.tracker.pairs[(RELAY_ID, TARGET)] = Pair(
            relay_id=RELAY_ID, relay_name="relay10", target=TARGET, state=PairState.IDLE,
            reason_kind=ReasonKind.LOSS, loss_pct=30.0, downs=0, decided_at=now, last_data_at=now,
            strikes=[now - 3000, now - 2000, now - 1000, now - 10],
        )
        view = self.runner.view({"10.0.0.5": "nl-3"})
        self.assertEqual(view["items"], [])
        self.assertEqual(view["chronic"], [
            {"target": TARGET, "ip": "10.0.0.5", "port": 443, "owner": "nl-3", "strikes": 4},
        ])

    async def test_blocked_pair_shows_why(self):
        self.skip_reason = "last_server"
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        [item] = self.runner.view({})["items"]
        self.assertEqual((item["state"], item["blocked_reason"], item["until"], item["owner"]),
                         ("blocked", "last_server", None, None))

    async def test_returned_pairs_are_not_listed(self):
        self.observe(("10.0.0.5", 0.0, 3))
        await self.cycle()
        await self.runner.release(RELAY_ID, TARGET)
        self.assertEqual(self.runner.view({})["items"], [])

    async def test_paused_relay_is_listed(self):
        self.observe(*((f"10.0.0.{i}", 60.0, 0) for i in range(1, 5)))
        await self.cycle()
        self.assertEqual(self.runner.view({})["paused_relays"],
                         [{"server_id": RELAY_ID, "name": "relay10", "since": self.clock.now}])

    async def test_silent_server_is_disabled_and_shown_with_its_reason(self):
        # Потерь и падений нет: релей сообщает только, что сервер не отвечает клиентам
        self.registry.update(RELAY_ID, "relay10", {"loss_probe": [
            {"ip": "10.0.0.5", "port": 443, "loss_pct": 0.0, "rtt_ms": 40.0, "samples": 60, "downs": 0,
             "stalled": True},
        ]})
        await self.cycle()
        for _ in range(3):
            await self.cycle(advance=60)
        [item] = self.runner.view({})["items"]
        self.assertEqual((item["state"], item["reason"]["kind"]), ("disabled", "stalled"))

    async def test_disabled_relays_by_target(self):
        self.storage.servers[11] = relay(11)
        self.observe(("10.0.0.5", 0.0, 3))
        self.observe(("10.0.0.5", 0.0, 3), server_id=11)
        await self.cycle()
        self.assertEqual(self.runner.disabled_relays(), {TARGET: [RELAY_ID, 11]})


class StatementTests(unittest.TestCase):
    def test_statements_compile_for_postgres(self):
        dialect = postgresql.dialect()
        self.assertIn("backend_quarantines", str(pairs_select().compile(dialect=dialect)))
        self.assertIn("DELETE FROM backend_quarantines", str(pairs_delete().compile(dialect=dialect)))


if __name__ == "__main__":
    unittest.main()
