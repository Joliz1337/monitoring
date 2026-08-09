"""Тесты очереди отложенной синхронизации нод (app/services/node_sync_queue.py).

Голый unittest, без PostgreSQL и без сети: проверяются чистые части механизма —
расчёт отсрочки повторной попытки и разбор результата синка на «долг закрыт» /
«долг остаётся». Всё остальное в очереди — это UPSERT и DELETE, то есть сама база.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services.node_sync_queue import (  # noqa: E402
        RETRY_BASE_SECONDS,
        RETRY_MAX_SECONDS,
        retry_delay,
    )
    from app.services.blocklist_manager import BlocklistManager  # noqa: E402
except ImportError as e:  # рантайм панели (sqlalchemy, asyncpg) не установлен
    raise unittest.SkipTest(f"node_sync_queue requires the panel runtime: {e}")


class RetryDelayTest(unittest.TestCase):
    def test_first_attempt_waits_base_interval(self):
        self.assertEqual(retry_delay(1), RETRY_BASE_SECONDS)

    def test_delay_doubles_with_attempts(self):
        self.assertEqual(retry_delay(2), RETRY_BASE_SECONDS * 2)
        self.assertEqual(retry_delay(3), RETRY_BASE_SECONDS * 4)

    def test_delay_is_capped(self):
        # Без потолка нода, лежащая сутки, ушла бы в отсрочку на месяцы вперёд.
        self.assertEqual(retry_delay(50), RETRY_MAX_SECONDS)

    def test_zero_attempts_does_not_shorten_delay(self):
        self.assertEqual(retry_delay(0), RETRY_BASE_SECONDS)


class BlocklistErrorExtractionTest(unittest.TestCase):
    """`_first_error` превращает результат синка в причину, попадающую в очередь."""

    def test_reports_failing_direction(self):
        result = {
            "in": {"success": True, "message": "Synced"},
            "out": {"success": False, "message": "Timeout"},
        }
        self.assertEqual(BlocklistManager._first_error(result), "Timeout")

    def test_prefers_first_failing_direction(self):
        result = {
            "in": {"success": False, "message": "HTTP 500"},
            "out": {"success": False, "message": "Timeout"},
        }
        self.assertEqual(BlocklistManager._first_error(result), "HTTP 500")

    def test_falls_back_when_no_message(self):
        self.assertEqual(BlocklistManager._first_error({}), "Sync failed")


if __name__ == "__main__":
    unittest.main()
