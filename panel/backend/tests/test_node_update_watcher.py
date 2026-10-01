"""Тесты наблюдателя за обновлениями нод (app/services/node_update_watcher.py).

Голый unittest, без PostgreSQL и без сети: проверяется чистая часть — как ответ
ноды на /api/system/update/status превращается в «ждём / обновилась / провал»,
в том числе у старых агентов, которые попытку не помнят, и тексты уведомлений.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.models import NodeUpdateAttempt  # noqa: E402
    from app.services.node_update_watcher import (  # noqa: E402
        ATTEMPT_DEADLINE,
        SILENCE_LIMIT,
        FailureReason,
        FallbackProblem,
        Verdict,
        decide,
        failure_message,
        summarize_error,
    )
except ImportError as e:  # рантайм панели (sqlalchemy, asyncpg, aiogram) не установлен
    raise unittest.SkipTest(f"node_update_watcher requires the panel runtime: {e}")

STARTED = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SOON = STARTED + timedelta(minutes=1)


def make_attempt(attempt_id="a1", target_ref="main", from_version="10.30.0", last_contact_at=None):
    return NodeUpdateAttempt(
        server_id=1,
        attempt_id=attempt_id,
        target_ref=target_ref,
        from_version=from_version,
        started_at=STARTED,
        last_contact_at=last_contact_at or STARTED,
        stage="agent",
    )


class ReportingAgentTest(unittest.TestCase):
    """Агент, который пишет попытку на диск и отдаёт её идентификатор."""

    def test_running_attempt_waits(self):
        status = {"attempt_id": "a1", "in_progress": True, "last_result": None}
        self.assertIs(decide(make_attempt(), status, "10.30.0", SOON).verdict, Verdict.WAIT)

    def test_success(self):
        status = {"attempt_id": "a1", "in_progress": False, "last_result": "success"}
        self.assertIs(decide(make_attempt(), status, "10.31.0", SOON).verdict, Verdict.SUCCESS)

    def test_success_counts_even_if_version_unchanged(self):
        # На dev-канале версия между пушами может не меняться — верим итогу агента
        status = {"attempt_id": "a1", "last_result": "success"}
        self.assertIs(decide(make_attempt(), status, "10.30.0", SOON).verdict, Verdict.SUCCESS)

    def test_reported_failure_keeps_meaningful_line(self):
        status = {
            "attempt_id": "a1",
            "last_result": "failed",
            "last_error": "Exit code: 1\n[INFO] Trying GitHub\n[ERROR] Failed to clone repository\n"
                          "[ERROR] Update failed (exit code: 1)",
        }
        decision = decide(make_attempt(), status, "10.30.0", SOON)
        self.assertIs(decision.verdict, Verdict.FAILED)
        self.assertIs(decision.reason, FailureReason.REPORTED)
        self.assertEqual(decision.detail, "[ERROR] Failed to clone repository")

    def test_image_unavailable(self):
        status = {"attempt_id": "a1", "last_result": "failed", "reason": "image_unavailable"}
        self.assertIs(decide(make_attempt(), status, None, SOON).reason, FailureReason.IMAGE_UNAVAILABLE)

    def test_other_attempt_supersedes(self):
        status = {"attempt_id": "b2", "last_result": "failed"}
        self.assertIs(decide(make_attempt(), status, None, SOON).verdict, Verdict.SUPERSEDED)

    def test_deadline_turns_wait_into_failure(self):
        status = {"attempt_id": "a1", "in_progress": True, "last_result": None}
        decision = decide(make_attempt(), status, None, STARTED + ATTEMPT_DEADLINE)
        self.assertIs(decision.reason, FailureReason.TIMEOUT)


class LegacyAgentTest(unittest.TestCase):
    """Старый агент: идентификатора попытки нет, итог успеха теряется с рестартом."""

    def test_in_progress_waits(self):
        status = {"in_progress": True, "last_result": "failed"}
        self.assertIs(decide(make_attempt(attempt_id=None), status, "10.30.0", SOON).verdict, Verdict.WAIT)

    def test_failure_report_survives(self):
        status = {"in_progress": False, "last_result": "failed", "last_error": "[ERROR] Failed to get new images"}
        decision = decide(make_attempt(attempt_id=None), status, "10.30.0", SOON)
        self.assertIs(decision.verdict, Verdict.FAILED)

    def test_version_changed_means_success(self):
        status = {"in_progress": False, "last_result": None}
        self.assertIs(decide(make_attempt(attempt_id=None), status, "10.31.0", SOON).verdict, Verdict.SUCCESS)

    def test_same_version_on_stable_is_failure(self):
        status = {"in_progress": False, "last_result": None}
        decision = decide(make_attempt(attempt_id=None), status, "10.30.0", SOON)
        self.assertIs(decision.reason, FailureReason.VERSION_UNCHANGED)

    def test_same_version_on_dev_is_trusted(self):
        status = {"in_progress": False, "last_result": None}
        attempt = make_attempt(attempt_id=None, target_ref="dev")
        self.assertIs(decide(attempt, status, "10.30.0", SOON).verdict, Verdict.SUCCESS)

    def test_unknown_version_waits(self):
        status = {"in_progress": False, "last_result": None}
        self.assertIs(decide(make_attempt(attempt_id=None), status, None, SOON).verdict, Verdict.WAIT)

    def test_reporting_attempt_without_node_state_falls_back_to_version(self):
        # Файл состояния на ноде пропал — судим по версии, как у старого агента
        status = {"attempt_id": None, "in_progress": False, "last_result": None}
        self.assertIs(decide(make_attempt(), status, "10.31.0", SOON).verdict, Verdict.SUCCESS)


class SilentNodeTest(unittest.TestCase):
    def test_short_silence_waits(self):
        self.assertIs(decide(make_attempt(), None, None, SOON).verdict, Verdict.WAIT)

    def test_long_silence_is_failure(self):
        decision = decide(make_attempt(), None, None, STARTED + SILENCE_LIMIT)
        self.assertIs(decision.verdict, Verdict.FAILED)
        self.assertIs(decision.reason, FailureReason.SILENT)

    def test_silence_counts_from_last_contact(self):
        contacted = STARTED + timedelta(minutes=30)
        attempt = make_attempt(last_contact_at=contacted)
        self.assertIs(decide(attempt, None, None, contacted + timedelta(minutes=1)).verdict, Verdict.WAIT)


class SummarizeErrorTest(unittest.TestCase):
    def test_strips_ansi_colors(self):
        text = "\x1b[0;31m[ERROR]\x1b[0m Failed to get new images — update cancelled"
        self.assertEqual(summarize_error(text), "[ERROR] Failed to get new images — update cancelled")

    def test_falls_back_to_tail_without_errors(self):
        self.assertEqual(summarize_error("one\ntwo\nthree"), "two / three")

    def test_empty(self):
        self.assertEqual(summarize_error(None), "")


class FailureMessageTest(unittest.TestCase):
    def test_no_creds_ru(self):
        text = failure_message("ru", "nl-1", FailureReason.IMAGE_UNAVAILABLE, None, FallbackProblem.NO_CREDS)
        self.assertIn("nl-1", text)
        self.assertIn("не сохранён SSH-доступ", text)

    def test_ssh_failed_en(self):
        text = failure_message(
            "en", "de-2", FailureReason.REPORTED, "[ERROR] Failed to clone repository",
            FallbackProblem.SSH_FAILED, "SSH: wrong login",
        )
        self.assertIn("Failed to clone repository", text)
        self.assertIn("SSH: wrong login", text)

    def test_every_combination_renders(self):
        for lang in ("ru", "en"):
            for reason in FailureReason:
                for problem in FallbackProblem:
                    self.assertTrue(failure_message(lang, "x", reason, "d", problem, "p"))


if __name__ == "__main__":
    unittest.main()
