"""Опрос задачи автоустановки: статус и лог по смещению.

Браузер не держит долгий стрим, а периодически спрашивает у бэка «что нового
с позиции N» — обрыв сети у клиента ничего не теряет, следующий запрос
продолжает с того же места. Сквозная нумерация строк должна переживать
усечение буфера, а ошибка задачи — попадать и в лог, и в поле error.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import deploy_job_manager as module  # noqa: E402
from app.services.deploy_job_manager import DeployJob, DeployJobManager  # noqa: E402


def make_manager_with_job() -> tuple[DeployJobManager, DeployJob]:
    manager = DeployJobManager()
    job = DeployJob(
        id="job", name="node-1", host="203.0.113.10",
        server_url="https://203.0.113.10:9100",
    )
    manager._jobs[job.id] = job
    return manager, job


class SnapshotTests(unittest.TestCase):
    def test_unknown_job_is_none(self):
        manager, _ = make_manager_with_job()
        self.assertIsNone(manager.snapshot("missing", 0))

    def test_incremental_fetch_returns_only_new_lines(self):
        manager, job = make_manager_with_job()
        manager._log(job, "a")
        manager._log(job, "b")

        first = manager.snapshot(job.id, 0)
        self.assertEqual(first["lines"], ["a", "b"])
        self.assertEqual(first["next_offset"], 2)
        self.assertEqual(first["status"], "running")

        manager._log(job, "c")
        second = manager.snapshot(job.id, first["next_offset"])
        self.assertEqual(second["lines"], ["c"])
        self.assertEqual(second["next_offset"], 3)

        self.assertEqual(manager.snapshot(job.id, 3)["lines"], [])

    def test_offsets_survive_buffer_truncation(self):
        manager, job = make_manager_with_job()
        original = module.LOG_BUFFER_LIMIT
        module.LOG_BUFFER_LIMIT = 3
        try:
            for line in ("1", "2", "3", "4", "5"):
                manager._log(job, line)
        finally:
            module.LOG_BUFFER_LIMIT = original

        self.assertEqual(job.log, ["3", "4", "5"])
        self.assertEqual(job.log_offset, 2)
        # Клиент, читавший до строки №3 включительно, получает ровно хвост
        self.assertEqual(manager.snapshot(job.id, 3)["lines"], ["4", "5"])
        # Отставший сильнее, чем помещается в буфер, — всё, что ещё хранится
        self.assertEqual(manager.snapshot(job.id, 0)["lines"], ["3", "4", "5"])
        self.assertEqual(manager.snapshot(job.id, 5)["next_offset"], 5)

    def test_fail_puts_reason_into_log_and_error(self):
        manager, job = make_manager_with_job()
        manager._fail(job, "SSH: неверный логин, пароль или ключ")

        snapshot = manager.snapshot(job.id, 0)
        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["error"], "SSH: неверный логин, пароль или ключ")
        self.assertEqual(snapshot["lines"], ["[ERROR] SSH: неверный логин, пароль или ключ"])
        self.assertIsNotNone(job.finished_at)

    def test_first_error_is_kept(self):
        manager, job = make_manager_with_job()
        manager._fail(job, "первая причина")
        manager._fail(job, "вторая причина")
        self.assertEqual(job.error, "первая причина")


if __name__ == "__main__":
    unittest.main()
