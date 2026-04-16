import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, Message, Update,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

logger = logging.getLogger(__name__)

SETTINGS_CHECK_INTERVAL = 60


class TelegramBotService:
    """Централизованный сервис управления Telegram-ботами.

    - Дедупликация экземпляров Bot по токену
    - Единый Dispatcher с роутерами для обработки команд/callbacks
    - Long polling для каждого активного бота
    - Периодическая очистка устаревших ботов
    """

    def __init__(self):
        self._bots: dict[str, Bot] = {}
        self._poll_tasks: dict[str, asyncio.Task] = {}
        self._dp = Dispatcher()
        self._main_router = Router(name="main")
        self._dp.include_router(self._main_router)
        self._settings_check_task: asyncio.Task | None = None
        self._running = False

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

    async def send_message(
        self,
        bot_token: str,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict | None = None,
    ) -> bool:
        if not bot_token or not chat_id:
            return False
        bot = await self._get_or_create_bot(bot_token)

        markup = self._convert_markup(reply_markup)

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=markup,
            )
            return True
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")
            return False

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
        if not bot_token or not chat_id:
            return {"success": False, "error": "No bot token or chat ID"}
        ok = await self.send_message(bot_token, chat_id, text)
        if ok:
            return {"success": True, "message": "Test message sent"}
        return {"success": False, "error": "Failed to send message"}

    # --- Управление экземплярами Bot ---

    async def _get_or_create_bot(self, token: str) -> Bot:
        if token in self._bots:
            return self._bots[token]

        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
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
        from app.models import AlertSettings, RemnawaveSettings, XrayMonitorSettings
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

            result = await db.execute(select(XrayMonitorSettings).limit(1))
            xm = result.scalar_one_or_none()
            if xm and xm.use_custom_bot and xm.telegram_bot_token:
                active_tokens.add(xm.telegram_bot_token)

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
