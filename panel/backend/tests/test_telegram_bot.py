import os
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError  # noqa: E402
from aiogram.methods import SendMessage  # noqa: E402

from app.services.telegram_bot import TelegramBotService  # noqa: E402

MALFORMED_TOKEN = "not-a-token"
WELL_FORMED_TOKEN = "123456:ABCDEFghijklmnopqrstuvwxyz0123456789"
CHAT_ID = "-1001"


def _fake_bot(exc: Exception):
    bot = AsyncMock()
    bot.send_message = AsyncMock(side_effect=exc)
    return bot


def _api_error(cls, message: str):
    return cls(method=SendMessage(chat_id=CHAT_ID, text="x"), message=message)


class TelegramSendTestTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_token_reports_reason(self):
        result = await TelegramBotService().send_test(MALFORMED_TOKEN, CHAT_ID, "hi")
        self.assertFalse(result["success"])
        self.assertIn("token", result["error"].lower())

    async def test_unauthorized_reports_telegram_message(self):
        service = TelegramBotService()
        service._get_or_create_bot = AsyncMock(
            return_value=_fake_bot(_api_error(TelegramUnauthorizedError, "Unauthorized"))
        )
        result = await service.send_test(WELL_FORMED_TOKEN, CHAT_ID, "hi")
        self.assertFalse(result["success"])
        self.assertIn("Unauthorized", result["error"])

    async def test_network_error_reports_reason(self):
        service = TelegramBotService()
        service._get_or_create_bot = AsyncMock(
            return_value=_fake_bot(_api_error(TelegramNetworkError, "Request timeout error"))
        )
        result = await service.send_test(WELL_FORMED_TOKEN, CHAT_ID, "hi")
        self.assertFalse(result["success"])
        self.assertIn("timeout", result["error"].lower())

    async def test_success(self):
        service = TelegramBotService()
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=object())
        service._get_or_create_bot = AsyncMock(return_value=bot)
        result = await service.send_test(WELL_FORMED_TOKEN, CHAT_ID, "hi")
        self.assertTrue(result["success"])


class TelegramSendMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_token_does_not_raise(self):
        ok = await TelegramBotService().send_message(MALFORMED_TOKEN, CHAT_ID, "hi")
        self.assertFalse(ok)

    async def test_api_error_returns_none_id(self):
        service = TelegramBotService()
        service._get_or_create_bot = AsyncMock(
            return_value=_fake_bot(_api_error(TelegramUnauthorizedError, "Unauthorized"))
        )
        mid = await service.send_message_returning_id(WELL_FORMED_TOKEN, CHAT_ID, "hi")
        self.assertIsNone(mid)


if __name__ == "__main__":
    unittest.main()
