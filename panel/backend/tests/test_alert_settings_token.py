"""Токен бота уведомлений не покидает панель.

Настройки алертов отдавали токен как есть; теперь наружу уходит маска, маска
в ответном PUT токен не трогает, а тестовое сообщение шлётся сохранённым токеном.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import AlertSettings  # noqa: E402
from app.routers import alerts  # noqa: E402

TOKEN = "123456:ABC-DEF"
CHAT_ID = "-1001234567890"


class FakeDb:
    """alert_settings из одной строки."""

    def __init__(self, token: str | None = TOKEN, events: list | None = None):
        self.settings = AlertSettings(telegram_bot_token=token, telegram_chat_id=CHAT_ID)
        self.events = events if events is not None else []
        self.refresh = mock.AsyncMock()

    async def execute(self, statement) -> SimpleNamespace:
        return SimpleNamespace(scalar_one_or_none=lambda: self.settings)

    async def scalar(self, statement) -> str | None:
        return self.settings.telegram_bot_token

    async def commit(self) -> None:
        self.events.append("commit")


class AlertTokenMaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_token_is_returned_masked(self):
        response = await alerts.get_alert_settings(FakeDb())

        self.assertEqual(response["telegram_bot_token"], alerts.SECRET_MASK)
        self.assertNotIn(TOKEN, str(response))
        self.assertEqual(response["telegram_chat_id"], CHAT_ID)

    async def test_missing_token_is_returned_empty(self):
        for missing in (None, ""):
            response = await alerts.get_alert_settings(FakeDb(token=missing))

            self.assertEqual(response["telegram_bot_token"], "")

    async def test_update_response_is_masked_too(self):
        response = await alerts.update_alert_settings(alerts.AlertSettingsUpdate(enabled=True), FakeDb())

        self.assertEqual(response["telegram_bot_token"], alerts.SECRET_MASK)

    async def test_mask_sent_back_keeps_the_token(self):
        db = FakeDb()

        await alerts.update_alert_settings(
            alerts.AlertSettingsUpdate(telegram_bot_token=alerts.SECRET_MASK, telegram_chat_id="-42"), db,
        )

        self.assertEqual(db.settings.telegram_bot_token, TOKEN)
        self.assertEqual(db.settings.telegram_chat_id, "-42")

    async def test_new_token_replaces_the_saved_one(self):
        db = FakeDb()

        await alerts.update_alert_settings(alerts.AlertSettingsUpdate(telegram_bot_token="999:NEW"), db)

        self.assertEqual(db.settings.telegram_bot_token, "999:NEW")

    async def test_empty_token_clears_the_saved_one(self):
        db = FakeDb()

        response = await alerts.update_alert_settings(alerts.AlertSettingsUpdate(telegram_bot_token=""), db)

        self.assertFalse(db.settings.telegram_bot_token)
        self.assertEqual(response["telegram_bot_token"], "")


class AlertTelegramTestTests(unittest.IsolatedAsyncioTestCase):
    def use_alerter(self, events: list) -> mock.AsyncMock:
        async def send(bot_token: str, chat_id: str) -> dict:
            events.append("send")
            return {"success": True, "message": "Test message sent"}

        alerter = SimpleNamespace(test_telegram=mock.AsyncMock(side_effect=send))
        patcher = mock.patch.object(alerts, "get_server_alerter", return_value=alerter)
        patcher.start()
        self.addCleanup(patcher.stop)
        return alerter.test_telegram

    async def test_test_message_uses_the_saved_token(self):
        events: list = []
        send = self.use_alerter(events)

        response = await alerts.test_telegram(alerts.TelegramTestRequest(chat_id=CHAT_ID), FakeDb(events=events))

        send.assert_awaited_once_with(TOKEN, CHAT_ID)
        self.assertTrue(response["success"])

    async def test_database_connection_is_released_before_telegram(self):
        events: list = []
        self.use_alerter(events)

        await alerts.test_telegram(alerts.TelegramTestRequest(chat_id=CHAT_ID), FakeDb(events=events))

        self.assertEqual(events, ["commit", "send"])

    async def test_mask_from_an_old_page_is_not_used_as_a_token(self):
        """Страница, открытая до обновления панели, шлёт bot_token — теперь это маска."""
        send = self.use_alerter([])
        request = alerts.TelegramTestRequest.model_validate({"bot_token": alerts.SECRET_MASK, "chat_id": CHAT_ID})

        await alerts.test_telegram(request, FakeDb())

        send.assert_awaited_once_with(TOKEN, CHAT_ID)

    async def test_without_a_saved_token_nothing_is_sent(self):
        send = self.use_alerter([])

        response = await alerts.test_telegram(alerts.TelegramTestRequest(chat_id=CHAT_ID), FakeDb(token=None))

        send.assert_not_awaited()
        self.assertEqual(response, {"success": False, "error": "Bot token is not saved"})


if __name__ == "__main__":
    unittest.main()
