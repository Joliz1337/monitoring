"""Автобэкап в Telegram: исход запуска записывается при любом сбое.

Без БД, сети и Docker. Расписание смотрит на last_run_at: пока запуск не
записан, он считается несостоявшимся, и цикл стартует новый дамп каждую минуту.
Исключение из конвейера (таймаут pg_dump, сбой при отправке алерта) раньше
обходило запись — десятиминутные дампы шли один за другим без конца.
"""
import asyncio
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import BackupSettings  # noqa: E402
from app.routers import backup as bk  # noqa: E402
from app.services import backup_scheduler as sched  # noqa: E402
from app.services import backup_telegram as bt  # noqa: E402

DUMP = b"PGDMP\x01\x0e\x00custom-format-dump"
NOW = datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc)
LAST_ERROR_COLUMN_SIZE = BackupSettings.__table__.c.last_error.type.length


def due_settings() -> BackupSettings:
    """Автобэкап включён, ещё не запускался, отправлять есть куда."""
    s = BackupSettings()
    s.enabled = True
    s.schedule_kind = "every_hours"
    s.every_hours = 6
    s.last_run_at = None
    s.bot_token = "123456:token"
    s.chat_id = "-100500"
    s.archive_password = "archive-pw"
    s.volume_size_mb = 1
    return s


class FakeSession:
    def __init__(self):
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def commit(self):
        self.commits += 1


class PatchingTestCase(unittest.IsolatedAsyncioTestCase):
    def patch(self, target, name: str, replacement):
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)
        return replacement


class ScheduledRunTests(PatchingTestCase):
    def setUp(self):
        self.record_run = self.patch(sched, "_record_run", mock.AsyncMock())
        self.send_message = self.patch(bt, "send_message", mock.AsyncMock(return_value=True))
        self.patch(bt, "send_volumes", mock.AsyncMock())
        # Стоимость вывода ключа здесь ни при чём, а шаг цикла событий она затягивает
        self.patch(bt, "_PBKDF2_ITERS", 1)
        self.patch(bk, "read_panel_env", mock.Mock(return_value=None))
        self.patch(sched, "async_session_maker", FakeSession)
        self.patch(sched, "get_or_create_settings", mock.AsyncMock(return_value=due_settings()))

    async def run_one_tick(self) -> None:
        """Один оборот настоящего цикла: сон между оборотами его отменяет."""
        cancelled_sleep = mock.AsyncMock(side_effect=asyncio.CancelledError)
        with mock.patch.object(sched.asyncio, "sleep", cancelled_sleep), self.assertRaises(asyncio.CancelledError):
            await sched._loop()

    def recorded_run(self) -> tuple[bool, str | None]:
        self.record_run.assert_awaited_once()
        started_at, ok, error = self.record_run.await_args.args
        self.assertIsInstance(started_at, datetime)
        return ok, error

    async def test_exception_from_the_dump_is_recorded_as_a_failed_run(self):
        timeout = subprocess.TimeoutExpired(cmd="docker exec panel-postgres pg_dump", timeout=600)
        self.patch(bk, "_run_pg_dump", mock.Mock(side_effect=timeout))

        with self.assertLogs(sched.logger, level="ERROR"):
            await self.run_one_tick()

        ok, error = self.recorded_run()
        self.assertFalse(ok)
        self.assertIn("timed out after 600 seconds", error)

    async def test_failure_while_alerting_is_recorded_too(self):
        self.patch(bk, "_run_pg_dump", mock.Mock(return_value=(b"", "pg_dump: error: connection refused")))
        self.send_message.side_effect = ValueError("Telegram answered with a non-JSON page")

        with self.assertLogs(sched.logger, level="ERROR"):
            await self.run_one_tick()

        ok, error = self.recorded_run()
        self.assertFalse(ok)
        self.assertIn("non-JSON page", error)

    async def test_pg_dump_timeout_alerts_and_is_recorded(self):
        timeout = subprocess.TimeoutExpired(cmd="docker exec panel-postgres pg_dump", timeout=600)
        self.patch(bk, "_pg_env", mock.Mock(return_value=("panel", "paneldb")))
        self.patch(bk.subprocess, "run", mock.Mock(side_effect=timeout))

        await self.run_one_tick()

        ok, error = self.recorded_run()
        self.assertFalse(ok)
        self.assertIn("timed out", error)
        self.assertIn("timed out", self.send_message.await_args.args[2])

    async def test_empty_dump_alerts_and_is_recorded(self):
        self.patch(bk, "_run_pg_dump", mock.Mock(return_value=(b"", None)))

        await self.run_one_tick()

        self.assertEqual(self.recorded_run(), (False, "Пустой дамп"))
        self.assertIn("Пустой дамп", self.send_message.await_args.args[2])

    async def test_delivered_backup_is_recorded_ok(self):
        self.patch(bk, "_run_pg_dump", mock.Mock(return_value=(DUMP, None)))

        await self.run_one_tick()

        self.assertEqual(self.recorded_run(), (True, None))


class RecordRunTests(PatchingTestCase):
    def setUp(self):
        self.settings = BackupSettings()
        self.session = FakeSession()
        self.patch(sched, "async_session_maker", mock.Mock(return_value=self.session))
        self.patch(sched, "get_or_create_settings", mock.AsyncMock(return_value=self.settings))

    async def test_error_longer_than_the_column_still_gets_recorded(self):
        # pg_dump в ошибке перечисляет все колонки таблицы — это заведомо длиннее колонки
        long_error = "pg_dump: detail: Command was: COPY public.aggregated_metrics (" + "column, " * 200 + ") TO stdout;"

        await sched._record_run(NOW, False, long_error)

        self.assertEqual(self.settings.last_error, long_error[:LAST_ERROR_COLUMN_SIZE])
        self.assertEqual((self.settings.last_run_at, self.settings.last_status), (NOW, "error"))
        self.assertEqual(self.session.commits, 1)

    async def test_successful_run_clears_the_error(self):
        self.settings.last_error = "pg_dump: error: connection refused"

        await sched._record_run(NOW, True, None)

        self.assertEqual((self.settings.last_status, self.settings.last_error), ("ok", None))
        self.assertEqual(self.session.commits, 1)


class ManualRunTests(PatchingTestCase):
    """Кнопка «Бэкап сейчас»: сбой виден в настройках, а не тонет в фоновой задаче."""

    async def finish_background_tasks(self) -> None:
        background = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        await asyncio.gather(*background, return_exceptions=True)

    async def test_unexpected_error_shows_up_as_a_failed_run(self):
        settings = due_settings()
        self.patch(bk, "async_session_maker", FakeSession)
        self.patch(sched, "async_session_maker", FakeSession)
        self.patch(sched, "get_or_create_settings", mock.AsyncMock(return_value=settings))
        self.patch(sched, "run_backup_to_telegram", mock.AsyncMock(side_effect=MemoryError("archive does not fit")))

        with self.assertLogs(sched.logger, level="ERROR"):
            await bk.backup_to_telegram_now(_={})
            await self.finish_background_tasks()
            # Проверки внутри блока: иначе отсутствие лога заслонило бы незаписанный запуск
            self.assertEqual(settings.last_status, "error")
            self.assertIn("archive does not fit", settings.last_error)
            self.assertIsInstance(settings.last_run_at, datetime)


if __name__ == "__main__":
    unittest.main()
