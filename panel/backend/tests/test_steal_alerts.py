"""Алерт о steal: хостер забирает процессор у ВМ.

Сообщение уходит, когда steal держится выше порога дольше удержания, и
повторяется на каждой проверке, пока держится (реже их делает cooldown
алертера). «В норме» — только после сообщённого эпизода и только когда steal
держится ниже порога столько же: мигающий steal не шлёт пар «есть / нет».
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.steal_alerts import StealEvent, StealPolicy, StealWatch  # noqa: E402

POLICY = StealPolicy(threshold=20.0, sustained_sec=300)


def run(watch: StealWatch, readings: list[tuple[float, float]]) -> list:
    return [watch.evaluate(steal, now, POLICY) for now, steal in readings]


class StealWatchTests(unittest.TestCase):

    def test_short_spike_is_not_reported(self):
        watch = StealWatch()

        events = run(watch, [(0, 46.0), (120, 46.0), (240, 5.0)])

        self.assertEqual(events, [None, None, None])
        self.assertFalse(watch.alerted)

    def test_sustained_steal_is_reported_on_every_check(self):
        watch = StealWatch()

        events = run(watch, [(0, 46.0), (299, 46.0), (300, 46.0), (360, 30.0)])

        self.assertEqual(events, [None, None, StealEvent.HIGH, StealEvent.HIGH])
        self.assertEqual(watch.high_for(360), 360)

    def test_dip_below_threshold_restarts_the_hold(self):
        watch = StealWatch()

        events = run(watch, [(0, 46.0), (200, 10.0), (260, 46.0), (500, 46.0), (560, 46.0)])

        self.assertEqual(events, [None, None, None, None, StealEvent.HIGH])

    def test_recovery_needs_a_reported_episode(self):
        watch = StealWatch()

        events = run(watch, [(0, 46.0), (100, 1.0), (1000, 1.0)])

        self.assertEqual(events, [None, None, None])

    def test_recovery_after_calm_as_long_as_the_hold(self):
        watch = StealWatch(alerted=True)

        events = run(watch, [(0, 3.0), (299, 3.0), (300, 3.0), (400, 3.0)])

        self.assertEqual(events, [None, None, StealEvent.RECOVERED, None])
        self.assertFalse(watch.alerted)

    def test_flapping_steal_does_not_recover(self):
        watch = StealWatch(alerted=True)

        events = run(watch, [(0, 3.0), (200, 25.0), (260, 3.0), (500, 3.0), (560, 3.0)])

        self.assertEqual(events, [None, None, None, None, StealEvent.RECOVERED])

    def test_threshold_itself_counts_as_high(self):
        watch = StealWatch()

        self.assertEqual(run(watch, [(0, 20.0), (300, 20.0)]), [None, StealEvent.HIGH])

    def test_reset_forgets_the_episode(self):
        watch = StealWatch(alerted=True, high_since=0.0)

        watch.reset()

        self.assertEqual(watch, StealWatch())


if __name__ == "__main__":
    unittest.main()
