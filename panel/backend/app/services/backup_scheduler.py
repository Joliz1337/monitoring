"""Автоматические бэкапы панели в Telegram по расписанию.

Переиспользует конвейер существующего бэкапа (pg_dump + вшивание ключа шифрования),
добавляя шифрование архива паролем, разбивку на тома и заливку в Telegram.
Фоновый цикл раз в минуту проверяет расписание и запускает бэкап.
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session_maker
from app.models import BackupSettings
from app.routers import backup as backup_router
from app.services import backup_telegram

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60
_BYTES_PER_MB = 1024 * 1024
# Строку длиннее колонки Postgres отклоняет целиком, и запуск остался бы
# незаписанным; а pg_dump в тексте ошибки перечисляет все колонки таблицы
_LAST_ERROR_MAX_LEN = BackupSettings.__table__.c.last_error.type.length
_task: asyncio.Task | None = None


async def get_or_create_settings(db) -> BackupSettings:
    settings = (await db.execute(select(BackupSettings).limit(1))).scalar_one_or_none()
    if settings is None:
        settings = BackupSettings()
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


def should_run(s: BackupSettings, now: datetime) -> bool:
    """Пора ли запускать автобэкап. now — tz-aware UTC."""
    if not s.enabled:
        return False
    last = s.last_run_at

    if s.schedule_kind == "every_hours":
        hours = s.every_hours or 24
        if last is None:
            return True
        return (now - last).total_seconds() >= hours * 3600

    # daily в at_time (UTC)
    try:
        hh, mm = (s.at_time or "04:00").split(":")
        target_minute = int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        target_minute = 4 * 60
    if now.hour * 60 + now.minute < target_minute:
        return False
    if last is not None and last.astimezone(timezone.utc).date() == now.date():
        return False
    return True


def _dump_and_frame() -> tuple[bytes, str | None]:
    """Дамп с вшитым .env или причина провала — целиком в потоке executor'а.

    Сырой дамп из потока не выходит: результат executor'а остаётся в памяти,
    пока корутина не уступит циклу событий, то есть до самой заливки, и дамп
    пролежал бы всё шифрование рядом со своей копией с .env и шифртекстом.
    """
    data, err = backup_router._run_pg_dump()
    if err or not data:
        return b"", err or "Пустой дамп"
    return backup_router._frame_backup(data, backup_router.read_panel_env()), None


async def run_backup_to_telegram(s: BackupSettings) -> tuple[bool, str | None]:
    """Снять дамп, зашифровать паролем, разбить на тома и залить в Telegram."""
    token, chat_id, password = s.bot_token, s.chat_id, s.archive_password
    if not token or not chat_id:
        return False, "Не заданы Telegram бот-токен и чат"
    if not password:
        return False, "Не задан пароль архива"

    loop = asyncio.get_event_loop()
    framed, err = await loop.run_in_executor(None, _dump_and_frame)
    if err:
        await backup_telegram.send_message(token, chat_id, f"🔴 Бэкап панели не выполнен: {err}")
        return False, err
    size_mb = len(framed) // _BYTES_PER_MB

    volume_bytes = max(1, s.volume_size_mb or 45) * _BYTES_PER_MB
    try:
        parts = backup_telegram.encrypt_and_split(framed, password, volume_bytes)
        # У шифртекста своя копия архива — открытый текст на время заливки не нужен
        del framed
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        caption = (
            f"🗄 Бэкап панели\n📅 {ts} UTC\n"
            f"📦 {size_mb} МБ · томов: {len(parts)}\n🔐 AES-256"
        )
        await backup_telegram.send_volumes(token, chat_id, parts, f"panel-backup_{ts}.enc", caption)
    except Exception as exc:  # noqa: BLE001 — граница: крипто/сеть Telegram
        await backup_telegram.send_message(token, chat_id, f"🔴 Бэкап панели: {exc}")
        return False, str(exc)

    logger.info("Auto-backup delivered to Telegram (%d volumes)", len(parts))
    return True, None


async def _record_run(now: datetime, ok: bool, err: str | None) -> None:
    async with async_session_maker() as db:
        s = await get_or_create_settings(db)
        s.last_run_at = now
        s.last_status = "ok" if ok else "error"
        s.last_error = err[:_LAST_ERROR_MAX_LEN] if err else err
        await db.commit()


async def run_and_record(settings: BackupSettings, now: datetime) -> None:
    """Выполнить бэкап и записать исход запуска — при любом сбое.

    Незаписанный запуск расписание считает несостоявшимся и стартует новый дамп
    каждую минуту, поэтому неожиданное исключение — тоже записанный провал.
    """
    try:
        ok, err = await run_backup_to_telegram(settings)
    except Exception as exc:  # noqa: BLE001 — записать нужно любой исход
        logger.exception("Auto-backup failed unexpectedly")
        ok, err = False, f"{type(exc).__name__}: {exc}"
    await _record_run(now, ok, err)


async def _loop() -> None:
    while True:
        try:
            async with async_session_maker() as db:
                settings = await get_or_create_settings(db)
            now = datetime.now(timezone.utc)
            if should_run(settings, now):
                logger.info("Auto-backup: scheduled run starting")
                await run_and_record(settings, now)
        except Exception as exc:  # noqa: BLE001 — верхняя граница фонового цикла
            logger.error("Auto-backup scheduler error: %s", exc)
        await asyncio.sleep(CHECK_INTERVAL)


def start_scheduler() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
        logger.info("Auto-backup scheduler started")
