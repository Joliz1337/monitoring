"""Уведомление о несостоявшемся обновлении ноды (node_update_watcher._notify).

Голый unittest, без PostgreSQL и без сети: сессия и Telegram подменены.

Уведомление уходит один раз — попытка к этому моменту уже закрыта. Если Telegram
в эту минуту недоступен, сообщение должно встать в очередь повторной отправки,
иначе о ноде, оставшейся на старой версии, оператор не узнает.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.models import AlertHistory  # noqa: E402
    from app.services import node_update_watcher  # noqa: E402
    from app.services.node_update_watcher import FailureReason, FallbackProblem  # noqa: E402
except ImportError as e:  # рантайм панели (sqlalchemy, asyncpg, aiogram) не установлен
    raise unittest.SkipTest(f"node_update_watcher requires the panel runtime: {e}")

SERVER = SimpleNamespace(id=7, name="nl-1")


class FakeDatabase:
    """Подмена async_session: отдаёт настройки алертов, строки истории копит."""

    def __init__(self, alert_settings: SimpleNamespace | None) -> None:
        self.alert_settings = alert_settings
        self.history: list[AlertHistory] = []

    def __call__(self) -> "FakeDatabase":
        return self

    async def __aenter__(self) -> "FakeDatabase":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def execute(self, _statement) -> SimpleNamespace:
        return SimpleNamespace(scalar_one_or_none=lambda: self.alert_settings)

    def add(self, row: AlertHistory) -> None:
        self.history.append(row)

    async def commit(self) -> None:
        pass


class NotifyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.telegram = SimpleNamespace(send_message=mock.AsyncMock(return_value=True))
        patcher = mock.patch.object(node_update_watcher, "get_telegram_bot_service", return_value=self.telegram)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def notify(self, alert_settings: SimpleNamespace | None) -> FakeDatabase:
        db = FakeDatabase(alert_settings)
        with mock.patch.object(node_update_watcher, "async_session", db):
            await node_update_watcher._notify(
                SERVER, FailureReason.IMAGE_UNAVAILABLE, None, FallbackProblem.NO_CREDS,
            )
        return db

    async def test_message_is_queued_for_retry_when_telegram_is_unreachable(self):
        settings = SimpleNamespace(language="ru", telegram_bot_token="123:token", telegram_chat_id="-100")
        db = await self.notify(settings)

        self.telegram.send_message.assert_awaited_once()
        call = self.telegram.send_message.await_args
        self.assertEqual(call.args[:2], ("123:token", "-100"))
        self.assertIn("nl-1", call.args[2])
        self.assertEqual(call.kwargs, {"retry_later": True})
        self.assertEqual(len(db.history), 1)
        self.assertTrue(db.history[0].notified)

    async def test_alert_history_is_written_without_telegram(self):
        db = await self.notify(None)

        self.telegram.send_message.assert_not_awaited()
        self.assertEqual(len(db.history), 1)
        self.assertFalse(db.history[0].notified)
        self.assertEqual(db.history[0].server_id, SERVER.id)


if __name__ == "__main__":
    unittest.main()
