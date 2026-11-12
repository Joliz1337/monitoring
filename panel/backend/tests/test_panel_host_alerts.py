"""Алерты о нагрузке хоста самой панели.

Тревога по ресурсу — после удержания выше порога, повтор не чаще cooldown;
«в норме» — после такого же затишья и только после сообщённой тревоги,
cooldown на неё не действует. Ресурсы друг от друга не зависят.

Запуск: python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.panel_host_alerts import (  # noqa: E402
    PAIRED_ALERT_OPENER,
    PanelHostWatch,
    PanelReading,
    PanelResource,
    alert_type,
    format_bytes,
    format_message,
)
from app.services.threshold_watch import ThresholdEvent, ThresholdPolicy  # noqa: E402

POLICY = ThresholdPolicy(threshold=90.0, sustained_sec=300)
COOLDOWN = 1800
MB = 1024 * 1024
GB = 1024 * MB


def ram(percent: float) -> PanelReading:
    return PanelReading(PanelResource.RAM, percent, free_bytes=208 * MB)


def run(watch: PanelHostWatch, readings: list[tuple[float, PanelReading]]) -> list:
    return [watch.evaluate(reading, POLICY, now, COOLDOWN) for now, reading in readings]


class PanelHostWatchTests(unittest.TestCase):

    def test_sustained_load_alerts_once_per_cooldown(self):
        watch = PanelHostWatch()

        events = run(watch, [(0, ram(95)), (300, ram(95)), (360, ram(95)), (2100, ram(95))])

        self.assertEqual(events, [None, ThresholdEvent.HIGH, None, ThresholdEvent.HIGH])

    def test_recovery_after_calm_ignores_cooldown(self):
        watch = PanelHostWatch()

        events = run(watch, [(0, ram(95)), (300, ram(95)), (360, ram(60)), (660, ram(60))])

        self.assertEqual(events, [None, ThresholdEvent.HIGH, None, ThresholdEvent.RECOVERED])

    def test_no_recovery_without_reported_alert(self):
        watch = PanelHostWatch()

        events = run(watch, [(0, ram(95)), (120, ram(60)), (1000, ram(60))])

        self.assertEqual(events, [None, None, None])

    def test_restored_alert_recovers_after_restart(self):
        watch = PanelHostWatch()
        watch.restore_alerted(alert_type(PanelResource.RAM, ThresholdEvent.HIGH))

        events = run(watch, [(0, ram(60)), (300, ram(60))])

        self.assertEqual(events, [None, ThresholdEvent.RECOVERED])

    def test_resources_are_independent(self):
        watch = PanelHostWatch()
        cpu = PanelReading(PanelResource.CPU, 99.0)

        events = run(watch, [(0, ram(95)), (0, cpu), (300, ram(95)), (300, cpu)])

        self.assertEqual(events, [None, None, ThresholdEvent.HIGH, ThresholdEvent.HIGH])

    def test_reset_forgets_open_episodes(self):
        watch = PanelHostWatch()
        run(watch, [(0, ram(95)), (300, ram(95))])

        watch.reset()

        self.assertEqual(run(watch, [(400, ram(60)), (800, ram(60))]), [None, None])


class PanelAlertTypesTests(unittest.TestCase):

    def test_recovery_closes_its_own_resource(self):
        self.assertEqual(PAIRED_ALERT_OPENER["panel_ram_recovered"], "panel_ram_high")
        self.assertEqual(PAIRED_ALERT_OPENER["panel_disk_high"], "panel_disk_high")
        self.assertEqual(len(PAIRED_ALERT_OPENER), 2 * len(PanelResource))


class FormatTests(unittest.TestCase):

    def test_format_bytes(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(int(208.5 * MB)), "208.5 MB")
        self.assertEqual(format_bytes(4 * GB), "4 GB")

    def test_ram_alert_says_how_much_is_left(self):
        text = format_message(ram(94.5), ThresholdEvent.HIGH, 90.0, 5, "ru")

        self.assertEqual(text, "Память сервера панели занята на 94% уже 5 мин (порог 90%), свободно 208 MB")

    def test_disk_alert_shows_free_of_total(self):
        reading = PanelReading(PanelResource.DISK, 91.0, free_bytes=8 * GB, total_bytes=96 * GB)

        text = format_message(reading, ThresholdEvent.HIGH, 90.0, 1, "en")

        self.assertEqual(text, "Panel server disk is 91% full (threshold 90%), 8 GB free of 96 GB")

    def test_unknown_language_falls_back_to_english(self):
        reading = PanelReading(PanelResource.CPU, 35.0)

        text = format_message(reading, ThresholdEvent.RECOVERED, 90.0, 1, "de")

        self.assertEqual(text, "Panel server CPU is back to normal: 35% (threshold 90%)")


if __name__ == "__main__":
    unittest.main()
