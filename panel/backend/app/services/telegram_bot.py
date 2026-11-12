import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyParameters,
)
from aiogram.utils.token import TokenValidationError

logger = logging.getLogger(__name__)

SETTINGS_CHECK_INTERVAL = 60

# Пейсинг отправки под лимит Telegram (~30 msg/s на бота): на 500 нодах массовый
# всплеск алертов иначе ловит 429 и теряется молча.
MIN_SEND_INTERVAL = 0.05

# Сколько отказов флуд-контроля подряд пережидаем, прежде чем признать сообщение
# потерянным: в аварию отказы идут сериями, а потерянное сообщение — пропущенный алерт
MAX_FLOOD_RETRIES = 5
# Запас к названному Telegram ожиданию — на расхождение часов
FLOOD_WAIT_MARGIN = 0.5
# В группу Telegram принимает ~20 сообщений в минуту. Чат, упёршийся в лимит,
# FLOOD_PACING_WINDOW секунд идёт в этом темпе: на обычном он получил бы отказ снова
FLOOD_PACING_SECONDS = 3.1
FLOOD_PACING_WINDOW = 600


class TelegramSendError(Exception):
    """Сообщение не доставлено; `reason` — текст, пригодный для показа оператору."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class _ChatPace:
    """Очередь одного чата и его темп после отказа флуд-контроля."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sent_at: float = 0.0
    # Раньше этого момента Telegram сообщение в чат не примет
    blocked_until: float = 0.0
    slow_until: float = 0.0

    def delay(self, now: float) -> float:
        ready_at = self.blocked_until
        if now < self.slow_until:
            ready_at = max(ready_at, self.sent_at + FLOOD_PACING_SECONDS)
        return max(0.0, ready_at - now)

    def flood(self, now: float, retry_after: float) -> None:
        self.blocked_until = now + retry_after + FLOOD_WAIT_MARGIN
        self.slow_until = self.blocked_until + FLOOD_PACING_WINDOW


class TelegramBotService:
    """Централизованный сервис управления Telegram-ботами.

    - Дедупликация экземпляров Bot по токену
    - Единый Dispatcher с роутерами для обработки команд/callbacks
    - Long polling для каждого активного бота
    - Периодическая очистка устаревших ботов
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._clock = clock
        self._sleep = sleep
        self._bots: dict[str, Bot] = {}
        self._poll_tasks: dict[str, asyncio.Task] = {}
        self._dp = Dispatcher()
        self._main_router = Router(name="main")
        self._dp.include_router(self._main_router)
        self._settings_check_task: asyncio.Task | None = None
        self._running = False
        # Сериализация и пейсинг отправок по токену (защита от 429-burst)
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._last_send_at: dict[str, float] = {}
        self._chat_paces: dict[tuple[str, str], _ChatPace] = {}

    def _send_lock(self, token: str) -> asyncio.Lock:
        lock = self._send_locks.get(token)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[token] = lock
        return lock

    def _chat_pace(self, token: str, chat_id: str) -> _ChatPace:
        key = (token, str(chat_id))
        pace = self._chat_paces.get(key)
        if pace is None:
            pace = self._chat_paces[key] = _ChatPace()
        return pace

    @property
    def dispatcher(self) -> Dispatcher:
        return self._dp

    @property
    def router(self) -> Router:
        """Основной роутер для регистрации хендлеров."""
        return self._main_router

    def include_router(self, child: Router):
        """Подключить внешний роутер (например, от xray_stats_collector)."""
        self._dp.include_router(child)

    async def start(self):
        if self._running:
            return
        self._running = True

        self._register_builtin_handlers()
        self._settings_check_task = asyncio.create_task(self._settings_check_loop())
        logger.info("TelegramBotService started")

    async def stop(self):
        self._running = False

        if self._settings_check_task:
            self._settings_check_task.cancel()
            try:
                await self._settings_check_task
            except asyncio.CancelledError:
                pass
            self._settings_check_task = None

        for token in list(self._poll_tasks):
            await self._stop_bot(token)

        logger.info("TelegramBotService stopped")

    # --- Публичный API для сервисов ---

    async def _send(
        self,
        bot_token: str,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
    ) -> Message:
        """Отправляет сообщение; при любой неудаче поднимает TelegramSendError с причиной."""
        if not bot_token or not chat_id:
            raise TelegramSendError("Bot token or chat ID is empty")
        bot = await self._get_or_create_bot(bot_token)

        markup = self._convert_markup(reply_markup)
        reply_params = (
            ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)
            if reply_to_message_id else None
        )

        pace = self._chat_pace(bot_token, chat_id)
        # Очередь чата держится и на время флуд-ожидания: следующим сообщениям в него
        # всё равно ждать. Замок бота берётся только на сам запрос, поэтому ожидание
        # одного чата не задерживает отправки того же бота в другие
        async with pace.lock:
            flood_retries = 0
            while True:
                delay = pace.delay(self._clock())
                if delay > 0:
                    await self._sleep(delay)
                try:
                    msg = await self._request(
                        bot,
                        bot_token,
                        chat_id=chat_id,
                        text=text,
                        parse_mode=parse_mode,
                        reply_markup=markup,
                        reply_parameters=reply_params,
                    )
                except TelegramRetryAfter as e:
                    pace.flood(self._clock(), e.retry_after)
                    if flood_retries == MAX_FLOOD_RETRIES:
                        raise TelegramSendError(f"Telegram rate limit, retry after {e.retry_after}s") from e
                    flood_retries += 1
                else:
                    pace.sent_at = self._clock()
                    return msg

    async def _request(self, bot: Bot, bot_token: str, **message) -> Message:
        """Один запрос в общем темпе бота. Отказ флуд-контроля отдаёт наверх как есть:
        пережидают его уже без замка бота."""
        async with self._send_lock(bot_token):
            gap = self._clock() - self._last_send_at.get(bot_token, 0.0)
            if gap < MIN_SEND_INTERVAL:
                await self._sleep(MIN_SEND_INTERVAL - gap)

            try:
                msg = await bot.send_message(**message)
            except TelegramRetryAfter:
                self._last_send_at[bot_token] = self._clock()
                raise
            except TelegramNetworkError as e:
                raise TelegramSendError(f"Network error: {e.message}") from e
            except TelegramAPIError as e:
                raise TelegramSendError(f"Telegram: {e.message}") from e
            except Exception as e:
                raise TelegramSendError(str(e) or type(e).__name__) from e
            self._last_send_at[bot_token] = self._clock()
            return msg

    async def send_message(
        self,
        bot_token: str,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict | None = None,
    ) -> bool:
        try:
            await self._send(bot_token, chat_id, text, parse_mode, reply_markup)
        except TelegramSendError as e:
            logger.warning(f"Telegram send failed: {e.reason}")
            return False
        return True

    async def send_message_returning_id(
        self,
        bot_token: str,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        try:
            msg = await self._send(bot_token, chat_id, text, parse_mode, reply_markup, reply_to_message_id)
        except TelegramSendError as e:
            logger.warning(f"Telegram send failed: {e.reason}")
            return None
        return msg.message_id

    @staticmethod
    def _convert_markup(reply_markup) -> InlineKeyboardMarkup | None:
        """Конвертирует dict → InlineKeyboardMarkup, если нужно."""
        if reply_markup is None or isinstance(reply_markup, InlineKeyboardMarkup):
            return reply_markup
        if isinstance(reply_markup, dict) and "inline_keyboard" in reply_markup:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(**btn) for btn in row]
                    for row in reply_markup["inline_keyboard"]
                ]
            )
        return reply_markup

    async def send_test(self, bot_token: str, chat_id: str, text: str) -> dict:
        try:
            await self._send(bot_token, chat_id, text)
        except TelegramSendError as e:
            return {"success": False, "error": e.reason}
        return {"success": True, "message": "Test message sent"}

    # --- Управление экземплярами Bot ---

    async def _get_or_create_bot(self, token: str) -> Bot:
        if token in self._bots:
            return self._bots[token]

        try:
            bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        except TokenValidationError as e:
            raise TelegramSendError("Invalid bot token format, expected <bot_id>:<secret>") from e
        self._bots[token] = bot

        task = asyncio.create_task(self._poll_loop(token))
        self._poll_tasks[token] = task
        logger.info(f"Bot started (token ...{token[-6:]})")
        return bot

    async def _stop_bot(self, token: str):
        task = self._poll_tasks.pop(token, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        bot = self._bots.pop(token, None)
        if bot:
            await bot.session.close()
            logger.info(f"Bot stopped (token ...{token[-6:]})")

    # --- Polling ---

    async def _poll_loop(self, token: str):
        await asyncio.sleep(5)
        offset = 0

        while self._running and token in self._bots:
            bot = self._bots.get(token)
            if not bot:
                break

            try:
                updates = await bot.get_updates(offset=offset, timeout=30)
                for update in updates:
                    offset = update.update_id + 1
                    try:
                        await self._dp.feed_update(bot, update)
                    except Exception as e:
                        logger.error(f"Error handling update: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"Poll error (token ...{token[-6:]}): {e}")
                await asyncio.sleep(10)

    # --- Встроенные команды ---

    def _register_builtin_handlers(self):
        @self._main_router.message(Command("start"))
        async def cmd_start(message: Message):
            chat_id = message.chat.id
            await message.answer(
                f"✅ <b>Мониторинг бот активен</b>\n\nChat ID: <code>{chat_id}</code>",
            )

        @self._main_router.message(Command("status"))
        async def cmd_status(message: Message):
            from app.database import async_session
            from app.models import Server
            from sqlalchemy import select, func

            try:
                async with async_session() as db:
                    total = await db.scalar(select(func.count(Server.id)))
                    active = await db.scalar(
                        select(func.count(Server.id)).where(Server.is_active.is_(True))
                    )
                text = (
                    f"📊 <b>Статус мониторинга</b>\n\n"
                    f"Серверов: {active}/{total} активных\n"
                    f"Ботов запущено: {len(self._bots)}"
                )
            except Exception as e:
                text = f"⚠️ Ошибка получения статуса: {e}"

            await message.answer(text)

    # --- Очистка устаревших ботов ---

    async def _settings_check_loop(self):
        await asyncio.sleep(SETTINGS_CHECK_INTERVAL)
        while self._running:
            try:
                await self._cleanup_stale_bots()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"Settings check error: {e}")
            await asyncio.sleep(SETTINGS_CHECK_INTERVAL)

    async def _cleanup_stale_bots(self):
        from app.database import async_session
        from app.models import AlertSettings, RemnawaveSettings
        from sqlalchemy import select

        active_tokens: set[str] = set()

        async with async_session() as db:
            result = await db.execute(select(AlertSettings).limit(1))
            alert = result.scalar_one_or_none()
            if alert and alert.telegram_bot_token:
                active_tokens.add(alert.telegram_bot_token)

            result = await db.execute(select(RemnawaveSettings).limit(1))
            rw = result.scalar_one_or_none()
            if rw and rw.anomaly_use_custom_bot and rw.anomaly_tg_bot_token:
                active_tokens.add(rw.anomaly_tg_bot_token)

        for token in list(self._bots):
            if token not in active_tokens:
                await self._stop_bot(token)
                logger.info(f"Stopped stale bot (token ...{token[-6:]})")


# --- Singleton ---

_service: Optional[TelegramBotService] = None


def get_telegram_bot_service() -> TelegramBotService:
    global _service
    if _service is None:
        _service = TelegramBotService()
    return _service


async def start_telegram_bot_service():
    service = get_telegram_bot_service()
    await service.start()


async def stop_telegram_bot_service():
    service = get_telegram_bot_service()
    await service.stop()
