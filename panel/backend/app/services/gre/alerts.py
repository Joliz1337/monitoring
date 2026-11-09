"""Telegram-уведомления GRE-туннелей: туннель замолчал и снова ожил.

Уходят через бота раздела «Уведомления» на языке его настроек и пишутся в
историю алертов. Повторов нет: каждое сообщение — один переход состояния.
"""

import html
import json
import logging

from sqlalchemy import select

from app.database import async_session
from app.models import AlertHistory, AlertSettings
from app.services.gre.activity import SILENT_AFTER_SECONDS, ActivityChange, ActivityEvent

logger = logging.getLogger(__name__)


def alert_text(lang: str, server_name: str, change: ActivityChange) -> str:
    name, iface, ce = html.escape(server_name), html.escape(change.name), html.escape(change.ce)
    minutes = SILENT_AFTER_SECONDS // 60
    if change.event == ActivityEvent.SILENT:
        if lang == "ru":
            return (
                f"\U0001f534 <b>GRE-туннель молчит</b>\n\n"
                f"Сервер <b>{name}</b>: через {iface} (CE {ce}) больше {minutes} мин не приходит ни одного пакета — "
                f"ни трафика клиентов, ни проверок DDoS-Guard. Проверьте статус туннеля в кабинете DDoS-Guard "
                f"и не режет ли хостер GRE."
            )
        return (
            f"\U0001f534 <b>GRE tunnel is silent</b>\n\n"
            f"Server <b>{name}</b>: not a single packet has arrived through {iface} (CE {ce}) for over {minutes} min — "
            f"neither client traffic nor DDoS-Guard health checks. Check the tunnel status in the DDoS-Guard "
            f"dashboard and whether the hoster blocks GRE."
        )
    if lang == "ru":
        return f"✅ <b>GRE-туннель снова работает</b>\n\nСервер <b>{name}</b>: через {iface} (CE {ce}) снова идут пакеты."
    return f"✅ <b>GRE tunnel is back</b>\n\nServer <b>{name}</b>: packets are coming through {iface} (CE {ce}) again."


async def notify(server_id: int, server_name: str, change: ActivityChange, enabled: bool) -> bool:
    """Отправить в Telegram (если включено у сервера и бот настроен) и записать в историю."""
    notified = False
    try:
        async with async_session() as db:
            settings = (await db.execute(select(AlertSettings).limit(1))).scalar_one_or_none()
        lang = (settings.language or "en").lower() if settings else "en"
        text = alert_text(lang, server_name, change)
        if enabled and settings and settings.telegram_bot_token and settings.telegram_chat_id:
            from app.services.telegram_bot import get_telegram_bot_service
            notified = await get_telegram_bot_service().send_message(
                settings.telegram_bot_token, settings.telegram_chat_id, text,
            )
        async with async_session() as db:
            db.add(AlertHistory(
                server_id=server_id,
                server_name=server_name,
                alert_type=f"gre_{change.event.value}",
                severity="warning" if change.event == ActivityEvent.SILENT else "info",
                message=text,
                details=json.dumps({"interface": change.name, "ce": change.ce}, ensure_ascii=False),
                notified=notified,
            ))
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — уведомление не должно ронять цикл
        logger.error("GRE alert failed for server %s: %s", server_id, exc)
    return notified
