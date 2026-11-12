import asyncio
import math
import os
import sys
import unittest
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError  # noqa: E402
from aiogram.methods import SendMessage  # noqa: E402

from app.services import telegram_bot  # noqa: E402
from app.services.telegram_bot import TelegramBotService  # noqa: E402

MALFORMED_TOKEN = "not-a-token"
WELL_FORMED_TOKEN = "123456:ABCDEFghijklmnopqrstuvwxyz0123456789"
CHAT_ID = "-1001"
OTHER_CHAT_ID = "-1002"
# Столько секунд Telegram велит подождать в отказе флуд-контроля
FLOOD_WAIT = 30
# Лимит Telegram на сообщения бота в одну группу
GROUP_MESSAGES_PER_MINUTE = 20
MINUTE = 60


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


def _flood() -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=SendMessage(chat_id=CHAT_ID, text="x"), message="Too Many Requests", retry_after=FLOOD_WAIT,
    )


class FakeClock:
    """Часы и сон без ожидания: сон только переводит стрелки."""

    def __init__(self) -> None:
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        # Настоящий сон отдаёт управление — соседние отправки успевают вклиниться
        await asyncio.sleep(0)


class HeldSleep:
    """Флуд-ожидание, которое длится, пока тест не отпустит: видно, кто кого ждёт."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.released = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        if seconds < FLOOD_WAIT:
            return
        self.started.set()
        await self.released.wait()


class RecordingBot:
    """Бот, который отказывает по сценарию и помнит, что и когда ему отдали."""

    def __init__(self, clock: FakeClock, refusals: list[Exception]) -> None:
        self._clock = clock
        self._refusals = refusals
        self.attempts: list[float] = []
        self.delivered: list[tuple[str, str, float]] = []

    async def send_message(self, *, chat_id: str, text: str, **_options) -> SimpleNamespace:
        self.attempts.append(self._clock.now)
        if self._refusals:
            raise self._refusals.pop(0)
        self.delivered.append((chat_id, text, self._clock.now))
        return SimpleNamespace(message_id=len(self.delivered))


class GroupLimitedBot:
    """Бот, который, как Telegram, не принимает в группу больше 20 сообщений за минуту."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.accepted_at: list[float] = []
        self.refusals = 0

    async def send_message(self, *, chat_id: str, text: str, **_options) -> SimpleNamespace:
        now = self._clock.now
        recent = [at for at in self.accepted_at if now - at < MINUTE]
        if len(recent) >= GROUP_MESSAGES_PER_MINUTE:
            self.refusals += 1
            raise TelegramRetryAfter(
                method=SendMessage(chat_id=chat_id, text=text),
                message="Too Many Requests",
                retry_after=math.ceil(MINUTE - (now - recent[0])),
            )
        self.accepted_at.append(now)
        return SimpleNamespace(message_id=len(self.accepted_at))


class TelegramFloodControlTests(unittest.IsolatedAsyncioTestCase):
    """Telegram отвечает 429 «retry after N», когда группе шлют больше ~20 сообщений
    в минуту. В аварию отказы идут сериями: сообщение, потерянное на втором из них, —
    это пропущенный алерт."""

    def setUp(self) -> None:
        self.clock = FakeClock()

    def _service(self, *refusals: Exception, sleep=None) -> tuple[TelegramBotService, RecordingBot]:
        bot = RecordingBot(self.clock, list(refusals))
        service = TelegramBotService(clock=self.clock.time, sleep=sleep or self.clock.sleep)
        service._get_or_create_bot = AsyncMock(return_value=bot)
        return service, bot

    async def _gap_between_two_messages(
        self, service: TelegramBotService, bot: RecordingBot, chat_id: str = CHAT_ID,
    ) -> float:
        for text in ("one", "two"):
            await service.send_message(WELL_FORMED_TOKEN, chat_id, text)
        (_, _, earlier), (_, _, later) = bot.delivered[-2:]
        return later - earlier

    async def test_message_survives_flood_waits_in_a_row(self):
        service, bot = self._service(_flood(), _flood(), _flood())

        delivered = await service.send_message(WELL_FORMED_TOKEN, CHAT_ID, "alert")

        self.assertTrue(delivered)
        waits = [later - earlier for earlier, later in pairwise(bot.attempts)]
        self.assertEqual(len(waits), 3)
        self.assertTrue(all(wait >= FLOOD_WAIT for wait in waits), waits)

    async def test_message_is_dropped_only_after_the_retry_limit(self):
        service, bot = self._service(*[_flood() for _ in range(20)])

        delivered = await service.send_message(WELL_FORMED_TOKEN, CHAT_ID, "alert")

        self.assertFalse(delivered)
        self.assertEqual(len(bot.attempts), 1 + telegram_bot.MAX_FLOOD_RETRIES)

    async def test_next_message_waits_out_the_flood_that_dropped_the_previous_one(self):
        service, bot = self._service(*[_flood() for _ in range(1 + telegram_bot.MAX_FLOOD_RETRIES)])
        await service.send_message(WELL_FORMED_TOKEN, CHAT_ID, "dropped")
        dropped_at = bot.attempts[-1]

        delivered = await service.send_message(WELL_FORMED_TOKEN, CHAT_ID, "next")

        self.assertTrue(delivered)
        self.assertGreaterEqual(bot.attempts[-1] - dropped_at, FLOOD_WAIT)

    async def test_chat_gets_at_most_twenty_messages_a_minute_after_a_flood_wait(self):
        service, bot = self._service(_flood())

        for number in range(5):
            await service.send_message(WELL_FORMED_TOKEN, CHAT_ID, f"alert {number}")

        gaps = [later - earlier for (_, _, earlier), (_, _, later) in pairwise(bot.delivered)]
        self.assertEqual(len(gaps), 4)
        self.assertGreaterEqual(min(gaps), MINUTE / GROUP_MESSAGES_PER_MINUTE)
        self.assertAlmostEqual(max(gaps), telegram_bot.FLOOD_PACING_SECONDS)

    async def test_slow_pace_lasts_ten_minutes_after_the_flood_wait(self):
        service, bot = self._service(_flood())
        await service.send_message(WELL_FORMED_TOKEN, CHAT_ID, "the one that hit the limit")

        self.clock.now += 9 * MINUTE
        still_slow = await self._gap_between_two_messages(service, bot)
        self.clock.now += 2 * MINUTE
        back_to_normal = await self._gap_between_two_messages(service, bot)

        self.assertGreaterEqual(still_slow, MINUTE / GROUP_MESSAGES_PER_MINUTE)
        self.assertAlmostEqual(back_to_normal, telegram_bot.MIN_SEND_INTERVAL)

    async def test_flood_in_one_chat_does_not_slow_down_another_chat(self):
        service, bot = self._service(_flood())
        await service.send_message(WELL_FORMED_TOKEN, CHAT_ID, "the one that hit the limit")

        gap = await self._gap_between_two_messages(service, bot, OTHER_CHAT_ID)

        self.assertAlmostEqual(gap, telegram_bot.MIN_SEND_INTERVAL)

    async def test_flood_wait_in_one_chat_does_not_hold_back_another_chat(self):
        flood_wait = HeldSleep()
        service, _bot = self._service(_flood(), sleep=flood_wait)
        flooded = asyncio.create_task(service.send_message(WELL_FORMED_TOKEN, CHAT_ID, "to the flooded chat"))

        async def let_it_finish() -> None:
            flood_wait.released.set()
            await flooded

        self.addAsyncCleanup(let_it_finish)
        await asyncio.wait_for(flood_wait.started.wait(), timeout=1)

        try:
            delivered = await asyncio.wait_for(
                service.send_message(WELL_FORMED_TOKEN, OTHER_CHAT_ID, "to another chat"), timeout=1,
            )
        except asyncio.TimeoutError:
            self.fail("сообщение в другой чат ждёт конца чужого флуд-ожидания")

        self.assertTrue(delivered)
        self.assertFalse(flooded.done())

    async def test_messages_to_one_chat_keep_their_order_through_a_flood_wait(self):
        service, bot = self._service(_flood())

        await asyncio.gather(*[
            service.send_message(WELL_FORMED_TOKEN, CHAT_ID, text) for text in ("first", "second", "third")
        ])

        self.assertEqual([text for _chat, text, _at in bot.delivered], ["first", "second", "third"])

    async def test_without_flood_control_the_pace_stays_as_it_was(self):
        service, bot = self._service()

        gap = await self._gap_between_two_messages(service, bot)

        self.assertAlmostEqual(gap, telegram_bot.MIN_SEND_INTERVAL)

    async def test_burst_over_the_group_limit_arrives_in_full_and_stops_hitting_the_limit(self):
        burst = 5 * GROUP_MESSAGES_PER_MINUTE
        bot = GroupLimitedBot(self.clock)
        service = TelegramBotService(clock=self.clock.time, sleep=self.clock.sleep)
        service._get_or_create_bot = AsyncMock(return_value=bot)

        delivered = [await service.send_message(WELL_FORMED_TOKEN, CHAT_ID, f"alert {n}") for n in range(burst)]

        self.assertTrue(all(delivered))
        self.assertEqual(len(bot.accepted_at), burst)
        self.assertEqual(bot.refusals, 1)


if __name__ == "__main__":
    unittest.main()
