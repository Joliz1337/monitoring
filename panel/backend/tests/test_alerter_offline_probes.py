"""Тесты активных проб пропавших нод в алертере (app/services/server_alerter.py).

Голый unittest, без PostgreSQL и без сети: сама проба подменена.

Проба одной пропавшей ноды занимает 20–40 секунд. Пока пробы шли по очереди внутри
обхода парка, падение сотни нод одного хостера растягивало обход на час: алерты
приходили с опозданием, а проверки CPU и памяти у остальных нод пропускались.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.server_alerter import ServerAlertState, ServerAlerter  # noqa: E402

PROBE_SECONDS = 0.05
SETTINGS = SimpleNamespace(offline_enabled=True, check_interval=60, offline_fail_threshold=3, alert_cooldown=1800)


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
    return alerter


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


if __name__ == "__main__":
    unittest.main()
