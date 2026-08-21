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


async def run_backup_to_telegram(s: BackupSettings) -> tuple[bool, str | None]:
    """Снять дамп, зашифровать паролем, разбить на тома и залить в Telegram."""
    token, chat_id, password = s.bot_token, s.chat_id, s.archive_password
    if not token or not chat_id:
        return False, "Не заданы Telegram бот-токен и чат"
    if not password:
        return False, "Не задан пароль архива"

    loop = asyncio.get_event_loop()
    data, err = await loop.run_in_executor(None, backup_router._run_pg_dump)
    if err or not data:
        msg = err or "Пустой дамп"
        await backup_telegram.send_message(token, chat_id, f"🔴 Бэкап панели не выполнен: {msg}")
        return False, msg

    framed = backup_router._frame_backup(data, backup_router.read_panel_env())

    volume_bytes = max(1, s.volume_size_mb or 45) * 1024 * 1024
    try:
        parts = backup_telegram.encrypt_and_split(framed, password, volume_bytes)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        caption = (
            f"🗄 Бэкап панели\n📅 {ts} UTC\n"
            f"📦 {len(framed) // (1024 * 1024)} МБ · томов: {len(parts)}\n🔐 AES-256"
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
        s.last_error = err
        await db.commit()


async def _loop() -> None:
    while True:
        try:
            async with async_session_maker() as db:
                settings = await get_or_create_settings(db)
            now = datetime.now(timezone.utc)
            if should_run(settings, now):
                logger.info("Auto-backup: scheduled run starting")
                ok, err = await run_backup_to_telegram(settings)
                await _record_run(now, ok, err)
        except Exception as exc:  # noqa: BLE001 — верхняя граница фонового цикла
            logger.error("Auto-backup scheduler error: %s", exc)
        await asyncio.sleep(CHECK_INTERVAL)


def start_scheduler() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
        logger.info("Auto-backup scheduler started")
