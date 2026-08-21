import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import BackupSettings  # noqa: E402
from app.services.backup_scheduler import should_run  # noqa: E402


def _settings(**kw) -> BackupSettings:
    s = BackupSettings()
    s.enabled = kw.get("enabled", True)
    s.schedule_kind = kw.get("schedule_kind", "daily")
    s.at_time = kw.get("at_time", "04:00")
    s.every_hours = kw.get("every_hours", 24)
    s.last_run_at = kw.get("last_run_at")
    return s


class ShouldRunTests(unittest.TestCase):
    def test_disabled_never_runs(self):
        s = _settings(enabled=False)
        self.assertFalse(should_run(s, datetime(2026, 8, 21, 5, 0, tzinfo=timezone.utc)))

    def test_every_hours_first_run(self):
        s = _settings(schedule_kind="every_hours", every_hours=6, last_run_at=None)
        self.assertTrue(should_run(s, datetime(2026, 8, 21, 5, 0, tzinfo=timezone.utc)))

    def test_every_hours_too_soon(self):
        now = datetime(2026, 8, 21, 5, 0, tzinfo=timezone.utc)
        s = _settings(schedule_kind="every_hours", every_hours=6, last_run_at=now - timedelta(hours=2))
        self.assertFalse(should_run(s, now))

    def test_every_hours_due(self):
        now = datetime(2026, 8, 21, 5, 0, tzinfo=timezone.utc)
        s = _settings(schedule_kind="every_hours", every_hours=6, last_run_at=now - timedelta(hours=7))
        self.assertTrue(should_run(s, now))

    def test_daily_before_time(self):
        s = _settings(at_time="04:00", last_run_at=None)
        self.assertFalse(should_run(s, datetime(2026, 8, 21, 3, 30, tzinfo=timezone.utc)))

    def test_daily_after_time_not_run_today(self):
        s = _settings(at_time="04:00", last_run_at=None)
        self.assertTrue(should_run(s, datetime(2026, 8, 21, 4, 30, tzinfo=timezone.utc)))

    def test_daily_already_ran_today(self):
        now = datetime(2026, 8, 21, 5, 0, tzinfo=timezone.utc)
        s = _settings(at_time="04:00", last_run_at=datetime(2026, 8, 21, 4, 1, tzinfo=timezone.utc))
        self.assertFalse(should_run(s, now))

    def test_daily_ran_yesterday(self):
        now = datetime(2026, 8, 21, 5, 0, tzinfo=timezone.utc)
        s = _settings(at_time="04:00", last_run_at=datetime(2026, 8, 20, 4, 1, tzinfo=timezone.utc))
        self.assertTrue(should_run(s, now))


if __name__ == "__main__":
    unittest.main()
