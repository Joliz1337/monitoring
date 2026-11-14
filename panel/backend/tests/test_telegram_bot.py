import asyncio
import math
import os
import sys
import unittest
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiogram.exceptions import (  # noqa: E402
    ClientDecodeError,
    RestartingTelegram,
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import SendMessage  # noqa: E402
from aiogram.types import Chat  # noqa: E402
from pydantic import ValidationError  # noqa: E402

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


class UnreliableBot:
    """Бот за сетью, которая то есть, то нет; отдельные сообщения Telegram отвергает сам."""

    def __init__(self) -> None:
        self.reachable = True
        self.refusals: dict[str, Exception] = {}
        self.attempts: list[str] = []
        self.delivered: list[tuple[str, str]] = []

    async def send_message(self, *, chat_id: str, text: str, **_options) -> SimpleNamespace:
        self.attempts.append(text)
        if not self.reachable:
            raise _api_error(TelegramNetworkError, "Request timeout error")
        if text in self.refusals:
            raise self.refusals[text]
        self.delivered.append((str(chat_id), text))
        return SimpleNamespace(message_id=len(self.delivered))

    def texts(self) -> list[str]:
        return [text for _chat, text in self.delivered]


def _dropped(count: int) -> str:
    return telegram_bot.OUTBOX_DROPPED_NOTICE.format(count=count)


class TelegramRetryLaterTests(unittest.IsolatedAsyncioTestCase):
    """Telegram недоступен в момент отправки — сообщение раньше терялось. С retry_later
    оно ждёт в очереди повтора и уходит, когда Telegram снова отвечает."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.bot = UnreliableBot()
        self.service = TelegramBotService(clock=self.clock.time, sleep=self.clock.sleep)
        self.service._get_or_create_bot = AsyncMock(return_value=self.bot)

    async def send(self, text: str, chat_id: str = CHAT_ID, **options) -> bool:
        return await self.service.send_message(WELL_FORMED_TOKEN, chat_id, text, retry_later=True, **options)

    async def next_pass(self) -> None:
        await self.service._flush_outbox()

    async def test_message_that_met_a_network_failure_arrives_with_the_next_pass(self):
        self.bot.reachable = False
        delivered_now = await self.send("alert")
        self.bot.reachable = True

        await self.next_pass()

        self.assertFalse(delivered_now)
        self.assertEqual(self.bot.delivered, [(CHAT_ID, "alert")])

    async def test_delivered_message_is_not_sent_again(self):
        self.assertTrue(await self.send("alert"))

        await self.next_pass()

        self.assertEqual(self.bot.texts(), ["alert"])

    async def test_without_retry_later_the_message_is_lost_as_before(self):
        self.bot.reachable = False
        await self.service.send_message(WELL_FORMED_TOKEN, CHAT_ID, "alert")
        self.bot.reachable = True

        await self.next_pass()

        self.assertEqual(self.bot.delivered, [])

    async def test_caller_learns_about_the_late_delivery(self):
        on_delivered = AsyncMock()
        self.bot.reachable = False
        await self.send("alert", on_delivered=on_delivered)
        on_delivered.assert_not_awaited()
        self.bot.reachable = True

        await self.next_pass()
        await self.next_pass()

        on_delivered.assert_awaited_once_with()

    async def test_message_delivered_at_once_does_not_call_on_delivered(self):
        on_delivered = AsyncMock()

        await self.send("alert", on_delivered=on_delivered)
        await self.next_pass()

        on_delivered.assert_not_awaited()

    async def test_telegram_outage_is_retried(self):
        outages = {
            "5xx": _api_error(TelegramServerError, "Internal Server Error"),
            "restart": _api_error(RestartingTelegram, "Bad Gateway: restart"),
            "gateway error page instead of json": ClientDecodeError(
                "Failed to decode object", ValueError("not json"), "<html>502 Bad Gateway</html>",
            ),
        }
        for name, outage in outages.items():
            with self.subTest(name):
                self.bot.refusals[name] = outage
                self.assertFalse(await self.send(name))
                del self.bot.refusals[name]

                await self.next_pass()

                self.assertIn(name, self.bot.texts())

    async def test_exhausted_flood_limit_is_retried(self):
        bot = RecordingBot(self.clock, [_flood() for _ in range(1 + telegram_bot.MAX_FLOOD_RETRIES)])
        self.service._get_or_create_bot = AsyncMock(return_value=bot)

        delivered_now = await self.send("alert")
        await self.next_pass()

        self.assertFalse(delivered_now)
        self.assertEqual([text for _chat, text, _at in bot.delivered], ["alert"])

    async def test_final_refusal_is_not_retried(self):
        refusals = {
            "bad token": _api_error(TelegramUnauthorizedError, "Unauthorized"),
            "chat not found": _api_error(TelegramBadRequest, "Bad Request: chat not found"),
            "broken markup": _api_error(TelegramBadRequest, "Bad Request: can't parse entities"),
            "bot was kicked": _api_error(TelegramForbiddenError, "Forbidden: bot was kicked from the group chat"),
            "too large": _api_error(TelegramEntityTooLarge, "Request Entity Too Large"),
        }
        self.bot.refusals.update(refusals)
        for text in refusals:
            self.assertFalse(await self.send(text))
        self.bot.refusals.clear()

        await self.next_pass()

        self.assertEqual(self.bot.delivered, [])
        self.assertEqual(self.bot.attempts, list(refusals))

    async def test_answer_that_aiogram_could_not_parse_is_not_retried(self):
        # Telegram ответил JSON-ом, то есть сообщение, скорее всего, принял: повтор дал бы дубль
        with self.assertRaises(ValidationError) as unparsed:
            Chat.model_validate({"id": "not a number"})
        self.bot.refusals["alert"] = ClientDecodeError(
            "Failed to deserialize object", unparsed.exception, {"ok": True},
        )

        self.assertFalse(await self.send("alert"))

        self.assertEqual(self.service._outbox, {})

    async def test_malformed_token_is_not_retried(self):
        service = TelegramBotService(clock=self.clock.time, sleep=self.clock.sleep)

        self.assertFalse(await service.send_message(MALFORMED_TOKEN, CHAT_ID, "alert", retry_later=True))

        self.assertEqual(service._outbox, {})

    async def test_new_message_waits_behind_the_undelivered_one(self):
        self.bot.reachable = False
        await self.send("offline")
        self.bot.reachable = True

        delivered_now = await self.send("back online")
        self.assertFalse(delivered_now)
        self.assertEqual(self.bot.delivered, [])
        await self.next_pass()

        self.assertEqual(self.bot.texts(), ["offline", "back online"])

    async def test_undelivered_message_does_not_hold_back_another_chat(self):
        self.bot.reachable = False
        await self.send("to the first chat")
        self.bot.reachable = True

        delivered_now = await self.send("to another chat", chat_id=OTHER_CHAT_ID)

        self.assertTrue(delivered_now)
        self.assertEqual(self.bot.delivered, [(OTHER_CHAT_ID, "to another chat")])

    async def test_unreachable_telegram_costs_one_attempt_per_pass(self):
        self.bot.reachable = False
        for number in range(5):
            await self.send(f"alert {number}")
        self.assertEqual(self.bot.attempts, ["alert 0"])

        await self.next_pass()
        await self.next_pass()
        self.assertEqual(self.bot.attempts, ["alert 0"] * 3)
        self.bot.reachable = True
        await self.next_pass()

        self.assertEqual(self.bot.texts(), [f"alert {number}" for number in range(5)])

    async def test_message_older_than_six_hours_is_dropped_and_the_chat_is_told_how_many(self):
        self.bot.reachable = False
        await self.send("stale 1")
        await self.send("stale 2")
        self.clock.now += telegram_bot.OUTBOX_MAX_AGE + 1
        await self.send("fresh")
        self.bot.reachable = True

        await self.next_pass()
        await self.next_pass()

        self.assertEqual(self.bot.texts(), [_dropped(2), "fresh"])

    async def test_dropped_message_never_calls_on_delivered(self):
        on_delivered = AsyncMock()
        self.bot.reachable = False
        await self.send("stale", on_delivered=on_delivered)
        self.clock.now += telegram_bot.OUTBOX_MAX_AGE + 1
        self.bot.reachable = True

        await self.next_pass()

        on_delivered.assert_not_awaited()
        self.assertEqual(self.bot.texts(), [_dropped(1)])

    async def test_drop_notice_survives_until_telegram_is_reachable(self):
        self.bot.reachable = False
        await self.send("stale")
        self.clock.now += telegram_bot.OUTBOX_MAX_AGE + 1

        await self.next_pass()
        self.bot.reachable = True
        await self.next_pass()

        self.assertEqual(self.bot.texts(), [_dropped(1)])

    async def test_overflow_drops_the_oldest_and_the_chat_is_told_how_many(self):
        extra = 5
        self.bot.reachable = False
        for number in range(telegram_bot.OUTBOX_LIMIT + extra):
            await self.send(f"alert {number}")
        self.bot.reachable = True

        await self.next_pass()

        kept = [f"alert {number}" for number in range(extra, telegram_bot.OUTBOX_LIMIT + extra)]
        self.assertEqual(self.bot.texts(), [_dropped(extra), *kept])

    async def test_message_put_straight_into_the_queue_is_delivered_by_the_next_pass(self):
        on_delivered = AsyncMock()

        self.service.queue_message(WELL_FORMED_TOKEN, CHAT_ID, "from history", on_delivered=on_delivered)
        self.assertEqual(self.bot.attempts, [])
        await self.next_pass()

        self.assertEqual(self.bot.texts(), ["from history"])
        on_delivered.assert_awaited_once_with()

    async def test_age_of_a_queued_message_counts_towards_its_six_hours(self):
        almost_expired = telegram_bot.OUTBOX_MAX_AGE - 60
        self.service.queue_message(WELL_FORMED_TOKEN, CHAT_ID, "raised long ago", age=almost_expired)
        self.service.queue_message(WELL_FORMED_TOKEN, CHAT_ID, "raised just now")
        self.clock.now += 120

        await self.next_pass()

        self.assertEqual(self.bot.texts(), [_dropped(1), "raised just now"])

    async def test_final_refusal_of_a_queued_message_does_not_block_the_ones_behind_it(self):
        refused, accepted = AsyncMock(), AsyncMock()
        self.bot.reachable = False
        await self.send("<b>broken", on_delivered=refused)
        await self.send("fine", on_delivered=accepted)
        self.bot.reachable = True
        self.bot.refusals["<b>broken"] = _api_error(TelegramBadRequest, "Bad Request: can't parse entities")

        await self.next_pass()
        await self.next_pass()

        self.assertEqual(self.bot.texts(), ["fine"])
        refused.assert_not_awaited()
        accepted.assert_awaited_once_with()

    async def test_failing_on_delivered_does_not_stop_the_queue(self):
        self.bot.reachable = False
        await self.send("first", on_delivered=AsyncMock(side_effect=RuntimeError("database is down")))
        await self.send("second")
        self.bot.reachable = True

        with self.assertLogs(telegram_bot.logger, level="ERROR"):
            await self.next_pass()

        self.assertEqual(self.bot.texts(), ["first", "second"])


class TelegramRetryTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_started_service_retries_in_the_background_until_stopped(self):
        bot = UnreliableBot()
        service = TelegramBotService()
        service._get_or_create_bot = AsyncMock(return_value=bot)
        original_interval = telegram_bot.OUTBOX_RETRY_INTERVAL
        telegram_bot.OUTBOX_RETRY_INTERVAL = 0.01
        self.addCleanup(setattr, telegram_bot, "OUTBOX_RETRY_INTERVAL", original_interval)

        await service.start()
        self.addAsyncCleanup(service.stop)
        service.queue_message(WELL_FORMED_TOKEN, CHAT_ID, "alert")
        for _ in range(200):
            if bot.delivered:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(bot.texts(), ["alert"])

        await service.stop()
        service.queue_message(WELL_FORMED_TOKEN, CHAT_ID, "after stop")
        await asyncio.sleep(0.05)

        self.assertEqual(bot.texts(), ["alert"])
        self.assertIsNone(service._outbox_task)


if __name__ == "__main__":
    unittest.main()
