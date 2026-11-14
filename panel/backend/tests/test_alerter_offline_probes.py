"""Тесты активных проб пропавших нод в алертере (app/services/server_alerter.py).

Голый unittest, без PostgreSQL и без сети: сама проба подменена.

Проба одной пропавшей ноды занимает 20–40 секунд. Пока пробы шли по очереди внутри
обхода парка, падение сотни нод одного хостера растягивало обход на час: алерты
приходили с опозданием, а проверки CPU и памяти у остальных нод пропускались.

Здесь же — алерт о ноде, упавшей повторно внутри кулдауна: он придерживается,
но обязан уйти, если авария пережила кулдаун.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import server_alerter  # noqa: E402
from app.services.server_alerter import ServerAlertState, ServerAlerter  # noqa: E402

PROBE_SECONDS = 0.05
COOLDOWN = 1800
SETTINGS = SimpleNamespace(offline_enabled=True, check_interval=60, offline_fail_threshold=3, alert_cooldown=COOLDOWN)
ALERTING = SimpleNamespace(**vars(SETTINGS), language="en", offline_recovery_notify=True)
DEAD = (True, False)
T0 = 1_000_000.0


def node(server_id: int, *, silent_for_seconds: float) -> SimpleNamespace:
    last_seen = datetime.now(timezone.utc) - timedelta(seconds=silent_for_seconds)
    return SimpleNamespace(id=server_id, name=f"node-{server_id}", last_seen=last_seen, proxy_url=None)


def silent(server_id: int) -> SimpleNamespace:
    return node(server_id, silent_for_seconds=3600)


def alive(server_id: int) -> SimpleNamespace:
    return node(server_id, silent_for_seconds=5)


class ProbeRecorder:
    def __init__(self) -> None:
        self.probed: list[int] = []
        self.running = 0
        self.peak = 0

    async def __call__(self, srv) -> tuple[bool, bool | None]:
        self.probed.append(srv.id)
        self.running += 1
        self.peak = max(self.peak, self.running)
        await asyncio.sleep(PROBE_SECONDS)
        self.running -= 1
        return True, False


def alerter_with(recorder: ProbeRecorder) -> ServerAlerter:
    alerter = ServerAlerter()
    alerter._trigger_excluded = {"offline": set()}
    alerter._active_probe_sequence = recorder
    # У самой панели связь есть; настоящих подключений наружу тест не делает
    alerter._panel_reaches_outside = mock.AsyncMock(return_value=True)
    return alerter


class SentAlerts:
    """Подмена отправки: помнит, какие алерты поднял алертер."""

    def __init__(self, alerter: ServerAlerter) -> None:
        self.alerts: list[tuple[str, str]] = []
        alerter._send_and_save = self

    async def __call__(self, srv, settings, alert_type, severity, message, details) -> None:
        self.alerts.append((alert_type, message))

    def take(self) -> list[tuple[str, str]]:
        taken, self.alerts = self.alerts, []
        return taken


class HistoryRows:
    """Подмена async_session: на любой запрос отдаёт готовые строки истории."""

    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows

    def __call__(self) -> "HistoryRows":
        return self

    async def __aenter__(self) -> "HistoryRows":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def execute(self, statement) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: list(self.rows))


def ago(seconds: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


class OfflineProbesTest(unittest.IsolatedAsyncioTestCase):
    async def test_many_nodes_going_silent_at_once_are_probed_together(self):
        recorder = ProbeRecorder()
        alerter = alerter_with(recorder)
        servers = [silent(i) for i in range(1, 31)]

        started = time.monotonic()
        probes = await alerter._probe_offline_candidates(servers, set(), SETTINGS)
        elapsed = time.monotonic() - started

        self.assertEqual(sorted(probes), list(range(1, 31)))
        self.assertGreater(recorder.peak, 1)
        self.assertLess(elapsed, PROBE_SECONDS * 10)

    async def test_only_newly_silent_nodes_are_probed(self):
        recorder = ProbeRecorder()
        alerter = alerter_with(recorder)
        known_dead = ServerAlertState()
        known_dead.was_offline = True
        alerter._states[2] = known_dead

        await alerter._probe_offline_candidates(
            [silent(1), silent(2), alive(3), silent(4), silent(5)], {5}, SETTINGS,
        )

        self.assertEqual(sorted(recorder.probed), [1, 4])

    async def test_nothing_is_probed_when_offline_alerts_are_disabled(self):
        recorder = ProbeRecorder()
        alerter = alerter_with(recorder)
        disabled = SimpleNamespace(offline_enabled=False, check_interval=60, offline_fail_threshold=3)

        probes = await alerter._probe_offline_candidates([silent(1)], set(), disabled)

        self.assertEqual(probes, {})
        self.assertEqual(recorder.probed, [])

    async def test_node_that_answered_the_probe_is_not_reported_offline(self):
        recorder = ProbeRecorder()
        alerter = alerter_with(recorder)
        state = ServerAlertState()

        await alerter._check_offline(
            silent(1), state, SETTINGS, is_online=False, now=time.time(), probe=(False, None),
        )

        self.assertFalse(state.was_offline)
        self.assertEqual(recorder.probed, [])

    async def peak_of_probes_in_flight(self, outage: int) -> int:
        """Пределы уменьшены в десять раз против боевых (50 и 200): тысячи задач тесту ни к чему."""
        recorder = ProbeRecorder()
        alerter = alerter_with(recorder)
        with (
            mock.patch.object(server_alerter, "OFFLINE_PROBE_CONCURRENCY", 5),
            mock.patch.object(server_alerter, "OFFLINE_PROBE_MAX_CONCURRENCY", 20),
        ):
            probes = await alerter._probe_offline_candidates([silent(i) for i in range(1, outage + 1)], set(), SETTINGS)
        self.assertEqual(len(probes), outage)
        return recorder.peak

    async def test_small_outage_keeps_the_usual_number_of_probes_in_flight(self):
        self.assertEqual(await self.peak_of_probes_in_flight(outage=30), 5)

    async def test_probes_in_flight_grow_to_a_tenth_of_the_outage(self):
        self.assertEqual(await self.peak_of_probes_in_flight(outage=80), 8)

    async def test_probes_in_flight_never_exceed_the_ceiling(self):
        self.assertEqual(await self.peak_of_probes_in_flight(outage=300), 20)


class RepeatedOutageTest(unittest.IsolatedAsyncioTestCase):
    """Нода упала, поднялась и упала снова внутри кулдауна. Второй алерт глушится —
    это защита от мигающей ноды. Но раньше вместе с ним терялась вся авария:
    нода лежала сутки, а об этом не приходило ничего."""

    def setUp(self) -> None:
        self.recorder = ProbeRecorder()
        self.alerter = alerter_with(self.recorder)
        self.sent = SentAlerts(self.alerter)
        self.state = ServerAlertState()
        self.srv = silent(1)

    async def at(self, offset: float, *, online: bool, probe=None, silent_minutes: float = 0) -> list[tuple[str, str]]:
        self.srv.last_seen = ago(silent_minutes * 60)
        await self.alerter._check_offline(self.srv, self.state, ALERTING, online, T0 + offset, probe)
        return self.sent.take()

    async def flap(self) -> None:
        """Упала (алерт), поднялась (алерт), через 20 минут от первого падения упала снова."""
        await self.at(0, online=False, probe=DEAD)
        await self.at(600, online=True)
        self.assertEqual(await self.at(1200, online=False, probe=DEAD), [])

    async def test_first_outage_and_recovery_are_announced_as_before(self):
        self.assertEqual(
            await self.at(0, online=False, probe=DEAD),
            [("offline", "Server node-1 is offline (API and ICMP unreachable)")],
        )
        self.assertEqual(await self.at(600, online=True), [("recovery", "Server node-1 is back online")])

    async def test_outage_that_outlasts_the_cooldown_is_announced_when_the_cooldown_ends(self):
        await self.flap()

        self.assertEqual(await self.at(COOLDOWN - 60, online=False, silent_minutes=12), [])
        self.assertEqual(
            await self.at(COOLDOWN, online=False, silent_minutes=13),
            [("offline", "Server node-1 has been offline for 13 min (API and ICMP unreachable)")],
        )

    async def test_held_back_outage_is_announced_only_once(self):
        await self.flap()
        await self.at(COOLDOWN, online=False, silent_minutes=13)

        self.assertEqual(await self.at(3600, online=False, silent_minutes=43), [])
        self.assertEqual(await self.at(86400, online=False, silent_minutes=1423), [])

    async def test_held_back_alert_is_not_sent_once_the_node_is_back(self):
        await self.flap()

        self.assertEqual(await self.at(1500, online=True), [])
        self.assertFalse(self.state.offline_unannounced)
        self.assertEqual(
            await self.at(COOLDOWN + 60, online=False, probe=DEAD),
            [("offline", "Server node-1 is offline (API and ICMP unreachable)")],
        )

    async def test_flapping_node_gets_one_offline_alert_per_cooldown(self):
        announced_at_minute = []
        for minute in range(0, 120, 2):
            online = minute % 4 == 2
            alerts = await self.at(minute * 60, online=online, probe=None if online else DEAD, silent_minutes=1)
            if any(kind == "offline" for kind, _text in alerts):
                announced_at_minute.append(minute)

        # Падает каждые 4 минуты; кулдаун — 30 минут
        self.assertEqual(announced_at_minute, [0, 32, 64, 96])

    async def test_held_back_alert_needs_no_second_probe(self):
        await self.flap()

        await self.at(COOLDOWN, online=False, silent_minutes=13)

        self.assertEqual(self.recorder.probed, [])
        self.alerter._states[self.srv.id] = self.state
        await self.alerter._probe_offline_candidates([self.srv], set(), SETTINGS)
        self.assertEqual(self.recorder.probed, [])

    async def test_held_back_alert_keeps_what_the_probe_found(self):
        await self.at(0, online=False, probe=DEAD)
        await self.at(600, online=True)
        await self.at(1200, online=False, probe=(True, True))

        self.assertEqual(
            await self.at(COOLDOWN, online=False, silent_minutes=13),
            [("offline", "Server node-1 has been offline for 13 min (API unreachable, ICMP reachable)")],
        )

    async def test_long_outage_is_told_in_hours(self):
        self.state.last_alert["offline"] = T0 - 60
        await self.at(0, online=False, probe=DEAD)

        self.assertEqual(
            await self.at(COOLDOWN, online=False, silent_minutes=135),
            [("offline", "Server node-1 has been offline for 2 h 15 min (API and ICMP unreachable)")],
        )

    async def test_held_back_alert_speaks_the_language_of_notifications(self):
        russian = SimpleNamespace(**{**vars(ALERTING), "language": "ru"})
        self.state.last_alert["offline"] = T0 - 60
        self.srv.last_seen = ago(13 * 60)
        await self.alerter._check_offline(self.srv, self.state, russian, False, T0, DEAD)

        await self.alerter._check_offline(self.srv, self.state, russian, False, T0 + COOLDOWN)

        self.assertEqual(
            self.sent.take(),
            [("offline", "Сервер node-1 недоступен уже 13 мин (API и ICMP не отвечают)")],
        )

    async def test_isolated_panel_neither_probes_nor_reports_a_silent_node(self):
        self.alerter._panel_isolated = True

        self.assertEqual(await self.at(0, online=False), [])

        self.assertFalse(self.state.was_offline)
        self.assertEqual(self.recorder.probed, [])


class RestartTest(unittest.IsolatedAsyncioTestCase):
    """Придержанный алерт живёт в памяти. После рестарта панели нода, упавшая
    внутри кулдауна, находится заново — и алерт по ней всё равно уходит."""

    async def restored(self, rows: list[tuple]) -> tuple[ServerAlerter, SentAlerts]:
        alerter = alerter_with(ProbeRecorder())
        sent = SentAlerts(alerter)
        with mock.patch.object(server_alerter, "async_session", HistoryRows(rows)):
            await alerter._restore_state_from_history()
        return alerter, sent

    async def test_node_that_fell_again_before_the_restart_is_still_announced(self):
        alerter, sent = await self.restored([(1, "offline", ago(1500)), (1, "recovery", ago(900))])
        srv = node(1, silent_for_seconds=300)
        state = alerter._states[1]
        now = time.time()

        await alerter._check_offline(srv, state, ALERTING, False, now, DEAD)
        self.assertEqual(sent.take(), [])
        await alerter._check_offline(srv, state, ALERTING, False, now + 400)

        self.assertEqual(sent.take(), [("offline", "Server node-1 has been offline for 5 min (API and ICMP unreachable)")])

    async def test_outage_announced_before_the_restart_is_not_announced_again(self):
        alerter, sent = await self.restored([(1, "offline", ago(7200))])
        state = alerter._states[1]

        await alerter._check_offline(silent(1), state, ALERTING, False, time.time())

        self.assertEqual(sent.take(), [])
        self.assertTrue(state.was_offline)

    async def test_panel_isolation_announced_before_the_restart_stays_open(self):
        alerter, _sent = await self.restored([(None, "panel_isolated", ago(600))])

        self.assertAlmostEqual(alerter._isolation_alerted_at, time.time() - 600, delta=5)

    async def test_panel_isolation_that_ended_before_the_restart_is_closed(self):
        alerter, _sent = await self.restored(
            [(None, "panel_isolated", ago(600)), (None, "panel_reconnected", ago(300))],
        )

        self.assertIsNone(alerter._isolation_alerted_at)


class PanelConnectivityCheckTest(unittest.IsolatedAsyncioTestCase):
    """Проверка связи самой панели: три независимых адреса, хватает ответа одного."""

    async def reaches_outside(self, open_connection) -> bool:
        with mock.patch.object(asyncio, "open_connection", open_connection):
            return await ServerAlerter()._panel_reaches_outside()

    async def test_one_reachable_address_is_enough(self):
        async def only_quad9(host: str, port: int):
            if host != "9.9.9.9":
                raise OSError("network is unreachable")
            return mock.Mock(), mock.Mock()

        self.assertTrue(await self.reaches_outside(only_quad9))

    async def test_panel_with_no_reachable_address_has_no_connectivity(self):
        tried: list[tuple[str, int]] = []

        async def unreachable(host: str, port: int):
            tried.append((host, port))
            raise OSError("network is unreachable")

        self.assertFalse(await self.reaches_outside(unreachable))
        self.assertEqual(sorted(tried), sorted(server_alerter.CONNECTIVITY_ENDPOINTS))

    async def test_address_that_does_not_answer_in_time_is_unreachable(self):
        async def hangs(host: str, port: int):
            await asyncio.sleep(3600)

        with mock.patch.object(server_alerter, "CONNECTIVITY_TIMEOUT", 0.01):
            self.assertFalse(await asyncio.wait_for(self.reaches_outside(hangs), 2))


if __name__ == "__main__":
    unittest.main()
