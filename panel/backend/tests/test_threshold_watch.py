"""Эпизод «выше порога» — общий для steal на нодах и нагрузки хоста панели.

Тревога уходит, когда значение держится выше порога дольше удержания, и
повторяется на каждой проверке, пока держится (реже их делает cooldown
алертера). «В норме» — только после сообщённого эпизода и только когда значение
держится ниже порога столько же: мигающее значение не шлёт пар «есть / нет».
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.threshold_watch import ThresholdEvent, ThresholdPolicy, ThresholdWatch  # noqa: E402

POLICY = ThresholdPolicy(threshold=20.0, sustained_sec=300)


def run(watch: ThresholdWatch, readings: list[tuple[float, float]]) -> list:
    return [watch.evaluate(value, now, POLICY) for now, value in readings]


class ThresholdWatchTests(unittest.TestCase):

    def test_short_spike_is_not_reported(self):
        watch = ThresholdWatch()

        events = run(watch, [(0, 46.0), (120, 46.0), (240, 5.0)])

        self.assertEqual(events, [None, None, None])
        self.assertFalse(watch.alerted)

    def test_sustained_value_is_reported_on_every_check(self):
        watch = ThresholdWatch()

        events = run(watch, [(0, 46.0), (299, 46.0), (300, 46.0), (360, 30.0)])

        self.assertEqual(events, [None, None, ThresholdEvent.HIGH, ThresholdEvent.HIGH])
        self.assertEqual(watch.high_for(360), 360)

    def test_dip_below_threshold_restarts_the_hold(self):
        watch = ThresholdWatch()

        events = run(watch, [(0, 46.0), (200, 10.0), (260, 46.0), (500, 46.0), (560, 46.0)])

        self.assertEqual(events, [None, None, None, None, ThresholdEvent.HIGH])

    def test_recovery_needs_a_reported_episode(self):
        watch = ThresholdWatch()

        events = run(watch, [(0, 46.0), (100, 1.0), (1000, 1.0)])

        self.assertEqual(events, [None, None, None])

    def test_recovery_after_calm_as_long_as_the_hold(self):
        watch = ThresholdWatch(alerted=True)

        events = run(watch, [(0, 3.0), (299, 3.0), (300, 3.0), (400, 3.0)])

        self.assertEqual(events, [None, None, ThresholdEvent.RECOVERED, None])
        self.assertFalse(watch.alerted)

    def test_flapping_value_does_not_recover(self):
        watch = ThresholdWatch(alerted=True)

        events = run(watch, [(0, 3.0), (200, 25.0), (260, 3.0), (500, 3.0), (560, 3.0)])

        self.assertEqual(events, [None, None, None, None, ThresholdEvent.RECOVERED])

    def test_threshold_itself_counts_as_high(self):
        watch = ThresholdWatch()

        self.assertEqual(run(watch, [(0, 20.0), (300, 20.0)]), [None, ThresholdEvent.HIGH])

    def test_reset_forgets_the_episode(self):
        watch = ThresholdWatch(alerted=True, high_since=0.0)

        watch.reset()

        self.assertEqual(watch, ThresholdWatch())


if __name__ == "__main__":
    unittest.main()
