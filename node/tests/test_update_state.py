"""Тесты итога обновления агента (app/services/update_state.py).

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Главное, что здесь проверяется: итог попытки не теряется, когда процесс агента,
запустивший апдейтер, до итога не дожил (при успехе apply-update.sh пересоздаёт
контейнер), и опоздавший итог старой попытки не затирает новую.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.update_state import (  # noqa: E402
    UpdateOutcome,
    UpdateResult,
    UpdateStateStore,
    UpdaterContainer,
    current_stage,
    new_attempt,
    outcome_from_exit,
    resolve_orphaned,
    status_payload,
)


class OutcomeFromExitTest(unittest.TestCase):
    def test_zero_is_success(self):
        self.assertIs(outcome_from_exit(0, "logs").result, UpdateResult.SUCCESS)

    def test_exit_20_needs_image_delivery(self):
        outcome = outcome_from_exit(20, "")
        self.assertIs(outcome.result, UpdateResult.FAILED)
        self.assertEqual(outcome.reason, "image_unavailable")

    def test_other_codes_keep_log_tail(self):
        outcome = outcome_from_exit(1, "x" * 5000 + "[ERROR] Failed to clone repository")
        self.assertIs(outcome.result, UpdateResult.FAILED)
        self.assertIn("Exit code: 1", outcome.error)
        self.assertIn("Failed to clone repository", outcome.error)
        self.assertLess(len(outcome.error), 1100)


class ResolveOrphanedTest(unittest.TestCase):
    def setUp(self):
        self.attempt = new_attempt("main", "10.30.0")

    def _container(self, running=False, exit_code=0, attempt_id=None):
        return UpdaterContainer(
            running=running,
            exit_code=exit_code,
            attempt_id=attempt_id or self.attempt.attempt_id,
            logs="[ERROR] boom",
        )

    def test_exited_updater_of_this_attempt_decides(self):
        resolved = resolve_orphaned(self.attempt, self._container(exit_code=0))
        self.assertIs(resolved.result, UpdateResult.SUCCESS)
        self.assertIsNotNone(resolved.finished_at)

    def test_failed_updater_reports_failure(self):
        resolved = resolve_orphaned(self.attempt, self._container(exit_code=1))
        self.assertIs(resolved.result, UpdateResult.FAILED)
        self.assertIn("boom", resolved.error)

    def test_running_updater_keeps_attempt_open(self):
        resolved = resolve_orphaned(self.attempt, self._container(running=True))
        self.assertTrue(resolved.running)

    def test_missing_updater_is_interrupted(self):
        resolved = resolve_orphaned(self.attempt, None)
        self.assertIs(resolved.result, UpdateResult.FAILED)
        self.assertEqual(resolved.reason, "interrupted")

    def test_foreign_updater_does_not_decide(self):
        # Код выхода чужого апдейтера к этой попытке отношения не имеет
        resolved = resolve_orphaned(self.attempt, self._container(exit_code=0, attempt_id="other"))
        self.assertEqual(resolved.reason, "interrupted")

    def test_finished_attempt_is_left_alone(self):
        finished = resolve_orphaned(self.attempt, self._container(exit_code=0))
        self.assertIs(resolve_orphaned(finished, None), finished)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UpdateStateStore(Path(self.tmp.name) / "sub" / "update-state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_means_no_attempt(self):
        self.assertIsNone(self.store.load())

    def test_roundtrip(self):
        attempt = new_attempt("dev", "10.31.0")
        self.store.save(attempt)
        self.assertEqual(self.store.load(), attempt)

    def test_record_finishes_current_attempt(self):
        attempt = new_attempt("main", "10.30.0")
        self.store.save(attempt)
        self.store.record(attempt.attempt_id, UpdateOutcome(UpdateResult.FAILED, error="clone failed"))
        loaded = self.store.load()
        self.assertIs(loaded.result, UpdateResult.FAILED)
        self.assertEqual(loaded.error, "clone failed")

    def test_late_result_of_old_attempt_is_ignored(self):
        old = new_attempt("main", "10.30.0")
        fresh = new_attempt("main", "10.30.0")
        self.store.save(fresh)
        self.store.record(old.attempt_id, UpdateOutcome(UpdateResult.FAILED, error="stale"))
        self.assertTrue(self.store.load().running)

    def test_corrupt_file_reads_as_empty(self):
        self.store.save(new_attempt("main", "1"))
        self.store._path.write_text("{not json")
        self.assertIsNone(self.store.load())


class CurrentStageTest(unittest.TestCase):
    def test_last_marker_wins(self):
        logs = "\n".join([
            "[STAGE] download",
            "[INFO] Cloning...",
            "[STAGE] files",
            "sending incremental file list",
            "[STAGE] images",
            "",
        ])
        self.assertEqual(current_stage(logs), "images")

    def test_no_markers(self):
        # Апдейтер старой ноды меток не печатает — этап неизвестен
        self.assertIsNone(current_stage("[INFO] Cloning repository...\n"))

    def test_unknown_stage_is_ignored(self):
        self.assertEqual(current_stage("[STAGE] files\n[STAGE] mystery\n"), "files")

    def test_marker_must_be_whole_line(self):
        self.assertIsNone(current_stage("echo [STAGE] files later\n"))


class StatusPayloadTest(unittest.TestCase):
    def test_running_attempt_has_no_result_yet(self):
        attempt = new_attempt("main", "10.30.0")
        payload = status_payload(attempt, True, "images", "10.30.0")
        self.assertTrue(payload["in_progress"])
        self.assertEqual(payload["stage"], "images")
        self.assertIsNone(payload["last_result"])
        self.assertEqual(payload["attempt_id"], attempt.attempt_id)

    def test_no_attempt(self):
        payload = status_payload(None, False, None, "10.31.0")
        self.assertIsNone(payload["attempt_id"])
        self.assertEqual(payload["version"], "10.31.0")


if __name__ == "__main__":
    unittest.main()
