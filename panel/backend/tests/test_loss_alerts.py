"""Tests for the relay packet-loss alert logic (services/loss_alerts).

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Инварианты: алерт только после удержания порога, один раз на эпизод; «снизились»
только ниже половины порога; пропавший адрес забывается без сообщения.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import server_alerter  # noqa: E402
from app.services.loss_alerts import (  # noqa: E402
    MIN_SAMPLES,
    LossAlertState,
    LossReading,
    alert_message,
    parse_readings,
    recovery_message,
)

THRESHOLD = 20.0
SUSTAINED = 300
COOLDOWN = 1800


def reading(loss: float, ip: str = "62.50.146.225", rtt: float | None = 45.0) -> LossReading:
    return LossReading(ip=ip, port=8443, loss_pct=loss, rtt_ms=rtt)


def run(state: LossAlertState, readings: list[LossReading], now: float):
    return state.evaluate(readings, THRESHOLD, SUSTAINED, COOLDOWN, now)


class ParseReadingsTests(unittest.TestCase):
    def test_skips_short_windows_and_broken_entries(self):
        metrics = {"loss_probe": [
            {"ip": "10.0.0.5", "port": 443, "loss_pct": 40.0, "rtt_ms": 45.0, "samples": MIN_SAMPLES},
            {"ip": "10.0.0.6", "port": 443, "loss_pct": 90.0, "rtt_ms": None, "samples": MIN_SAMPLES - 1},
            {"ip": "10.0.0.7", "loss_pct": 10.0, "samples": 60},
            "garbage",
        ]}
        self.assertEqual(parse_readings(metrics), [LossReading("10.0.0.5", 443, 40.0, 45.0)])

    def test_old_node_without_block(self):
        self.assertEqual(parse_readings({"cpu": {}}), [])


class EvaluateTests(unittest.TestCase):
    def test_fires_only_after_sustained(self):
        state = LossAlertState()
        self.assertEqual(run(state, [reading(40)], now=0).fired, [])
        self.assertEqual(run(state, [reading(40)], now=SUSTAINED - 1).fired, [])
        self.assertEqual(run(state, [reading(40)], now=SUSTAINED).fired, [reading(40)])

    def test_one_alert_per_episode(self):
        state = LossAlertState()
        run(state, [reading(40)], now=0)
        run(state, [reading(40)], now=SUSTAINED)
        self.assertEqual(run(state, [reading(50)], now=SUSTAINED + COOLDOWN + 1).fired, [])

    def test_short_dip_restarts_the_timer(self):
        state = LossAlertState()
        run(state, [reading(40)], now=0)
        run(state, [reading(5)], now=100)
        run(state, [reading(40)], now=200)
        self.assertEqual(run(state, [reading(40)], now=SUSTAINED + 100).fired, [])
        self.assertEqual(len(run(state, [reading(40)], now=SUSTAINED + 200).fired), 1)

    def test_recovery_needs_half_threshold(self):
        state = LossAlertState()
        run(state, [reading(40)], now=0)
        run(state, [reading(40)], now=SUSTAINED)
        between = run(state, [reading(THRESHOLD * 0.75)], now=SUSTAINED + 60)
        self.assertEqual((between.fired, between.recovered), ([], []))
        back = run(state, [reading(THRESHOLD * 0.25)], now=SUSTAINED + 120)
        self.assertEqual(back.recovered, [reading(THRESHOLD * 0.25)])

    def test_back_above_threshold_before_recovery_does_not_realert(self):
        state = LossAlertState()
        run(state, [reading(40)], now=0)
        run(state, [reading(40)], now=SUSTAINED)
        run(state, [reading(15)], now=SUSTAINED + 60)
        # Кулдаун и удержание уже пройдены — молчит именно незакрытый эпизод
        run(state, [reading(40)], now=SUSTAINED + COOLDOWN)
        self.assertEqual(run(state, [reading(40)], now=2 * SUSTAINED + COOLDOWN).fired, [])

    def test_cooldown_after_recovery(self):
        state = LossAlertState()
        run(state, [reading(40)], now=0)
        run(state, [reading(40)], now=SUSTAINED)
        run(state, [reading(1)], now=SUSTAINED + 60)
        run(state, [reading(40)], now=SUSTAINED + 120)
        self.assertEqual(run(state, [reading(40)], now=2 * SUSTAINED + 120).fired, [])
        self.assertEqual(len(run(state, [reading(40)], now=SUSTAINED + COOLDOWN).fired), 1)

    def test_batch_of_addresses_fires_together(self):
        state = LossAlertState()
        both = [reading(40, ip="62.50.146.225"), reading(35, ip="62.50.146.227")]
        run(state, both + [reading(0, ip="62.50.146.231")], now=0)
        fired = run(state, both + [reading(0, ip="62.50.146.231")], now=SUSTAINED).fired
        self.assertEqual({r.ip for r in fired}, {"62.50.146.225", "62.50.146.227"})

    def test_vanished_address_is_forgotten_silently(self):
        state = LossAlertState()
        run(state, [reading(40)], now=0)
        run(state, [reading(40)], now=SUSTAINED)
        gone = run(state, [], now=SUSTAINED + 60)
        self.assertEqual((gone.fired, gone.recovered), ([], []))
        self.assertEqual((state.started, state.alerted, state.last_fired), ({}, set(), {}))

    def test_reset_clears_episode(self):
        state = LossAlertState()
        run(state, [reading(40)], now=0)
        state.reset()
        self.assertEqual(run(state, [reading(40)], now=SUSTAINED).fired, [])


class MessageTests(unittest.TestCase):
    def test_ru_alert_lists_addresses(self):
        text = alert_message("Релей <VK>", [reading(40), reading(100, ip="10.0.0.9", rtt=None)], 20, 300, "ru")
        self.assertIn("Релей &lt;VK&gt;", text)
        self.assertIn("выше 20% дольше 5 мин", text)
        self.assertIn("• 62.50.146.225:8443 — 40% (45 мс)", text)
        self.assertIn("• 10.0.0.9:8443 — 100% (нет ответа)", text)

    def test_en_recovery(self):
        text = recovery_message("relay-1", [reading(2.5)], "en")
        self.assertIn("Packet loss from relay-1 is back to normal", text)
        self.assertIn("• 62.50.146.225:8443 — 2.5% (45 ms)", text)


class AlerterWiringTests(unittest.IsolatedAsyncioTestCase):
    """Триггер в цикле алертера: включён — одно сообщение на релей, исключён — тишина."""

    def setUp(self):
        self.settings = SimpleNamespace(
            language="ru", check_interval=60, offline_fail_threshold=3, alert_cooldown=COOLDOWN,
            offline_enabled=False, load_avg_enabled=False,
            packet_loss_enabled=True, packet_loss_threshold=THRESHOLD, packet_loss_sustained_seconds=SUSTAINED,
        )
        loss_probe = [{"ip": "62.50.146.225", "port": 8443, "loss_pct": 40.0, "rtt_ms": 45.0, "samples": 60}]
        self.relay = SimpleNamespace(
            id=7, name="VK relay", last_metrics=json.dumps({"loss_probe": loss_probe}),
            last_seen=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        self.alerter = server_alerter.ServerAlerter()
        self.alerter._check_antiddos = mock.AsyncMock()
        self.alerter._send_and_save = mock.AsyncMock()
        self.state = server_alerter.ServerAlertState()
        ingest = mock.patch.object(server_alerter, "get_traffic_ingest")
        ingest.start()
        self.addCleanup(ingest.stop)

    async def check(self, now: float, excluded: set[int] = frozenset()):
        self.alerter._trigger_excluded = {"packet_loss": set(excluded)}
        await self.alerter._check_server(self.relay, self.state, self.settings, now)

    async def test_sends_one_warning_after_sustained(self):
        await self.check(now=0)
        await self.check(now=SUSTAINED)
        self.alerter._send_and_save.assert_awaited_once()
        args = self.alerter._send_and_save.await_args.args
        self.assertEqual(args[2:4], ("packet_loss", "warning"))
        self.assertIn("62.50.146.225:8443 — 40%", args[4])

    async def test_excluded_relay_is_silent(self):
        await self.check(now=0, excluded={7})
        await self.check(now=SUSTAINED, excluded={7})
        self.alerter._send_and_save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
