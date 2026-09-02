"""Telegram-уведомления exit-прокси с cooldown по (нода, вид события)."""

import html
import json
import logging
import time
from typing import Optional

from sqlalchemy import select

from app.database import async_session
from app.models import AlertHistory, AlertSettings

logger = logging.getLogger(__name__)

KIND_SWITCHED = "switched"
KIND_MANUAL_SWITCH = "manual_switch"
KIND_NO_HEALTHY = "no_healthy"
KIND_RECOVERED = "recovered"
KIND_SELF_TEST_FAILED = "self_test_failed"
KIND_SELF_TEST_RECOVERED = "self_test_recovered"
KIND_CHECK_FAILED = "check_failed"
KIND_ENABLED = "enabled"
KIND_DISABLED = "disabled"

# Что уходит в Telegram; остальное — только в журнал панели
ALERT_KINDS = frozenset({
    KIND_SWITCHED, KIND_NO_HEALTHY, KIND_RECOVERED, KIND_SELF_TEST_FAILED, KIND_SELF_TEST_RECOVERED,
})

REASON_LABELS = {
    "switched": "текущий выход не прошёл проверки",
    "unknown": "текущий выход не удалось проверить",
    "no_healthy": "ни один выход не прошёл проверки",
    "keep": "выход остался прежним",
    "pinned": "закреплён вручную",
}


def candidate_label(candidate: Optional[str]) -> str:
    if not candidate:
        return "—"
    if candidate == "warp":
        return "WARP"
    return candidate.removeprefix("ip:")


def alert_text(kind: str, server_name: str, from_value: Optional[str], to_value: Optional[str], reason: str) -> str:
    name = html.escape(server_name)
    src, dst = html.escape(candidate_label(from_value)), html.escape(candidate_label(to_value))
    why = html.escape(REASON_LABELS.get(reason, reason or ""))
    if kind == KIND_SWITCHED:
        return (
            f"\U0001f500 <b>Exit-прокси: смена выхода</b>\n\n"
            f"Нода <b>{name}</b>: {src} → <b>{dst}</b>\nПричина: {why}"
        )
    if kind == KIND_NO_HEALTHY:
        return (
            f"⚠️ <b>Exit-прокси: нет здоровых выходов</b>\n\n"
            f"Нода <b>{name}</b>: все кандидаты не прошли проверки Google. "
            f"Трафик идёт через <b>{dst}</b> — первый по приоритету."
        )
    if kind == KIND_RECOVERED:
        return f"✅ <b>Exit-прокси: выход восстановился</b>\n\nНода <b>{name}</b>: <b>{dst}</b> снова проходит проверки."
    if kind == KIND_SELF_TEST_FAILED:
        return (
            f"❌ <b>Exit-прокси: сквозная проверка провалена</b>\n\n"
            f"Нода <b>{name}</b>: трафик через локальный socks не выходит через ожидаемый IP ({dst}). {why}"
        )
    if kind == KIND_SELF_TEST_RECOVERED:
        return f"✅ <b>Exit-прокси: сквозная проверка снова в норме</b>\n\nНода <b>{name}</b>: выход {dst}."
    return f"<b>Exit-прокси</b>: нода <b>{name}</b>, событие {html.escape(kind)}"


class ExitProxyAlerter:
    def __init__(self):
        self._last_sent: dict[tuple[int, str], float] = {}

    def _cooldown_ok(self, server_id: int, kind: str, cooldown: int) -> bool:
        last = self._last_sent.get((server_id, kind))
        return last is None or time.monotonic() - last >= cooldown

    async def notify(
        self,
        kind: str,
        server_id: int,
        server_name: str,
        *,
        from_value: Optional[str],
        to_value: Optional[str],
        reason: str,
        enabled: bool,
        cooldown_seconds: int,
    ) -> bool:
        """Отправить в Telegram (если включено и cooldown прошёл) и записать в историю алертов."""
        if kind not in ALERT_KINDS:
            return False
        if not self._cooldown_ok(server_id, kind, cooldown_seconds):
            return False
        text = alert_text(kind, server_name, from_value, to_value, reason)
        notified = False
        try:
            async with async_session() as db:
                alert_settings = (await db.execute(select(AlertSettings).limit(1))).scalar_one_or_none()
            token = getattr(alert_settings, "telegram_bot_token", None)
            chat_id = getattr(alert_settings, "telegram_chat_id", None)
            if enabled and token and chat_id:
                from app.services.telegram_bot import get_telegram_bot_service
                notified = await get_telegram_bot_service().send_message(token, chat_id, text)

            async with async_session() as db:
                db.add(AlertHistory(
                    server_id=server_id,
                    server_name=server_name,
                    alert_type=f"exit_proxy_{kind}",
                    severity="warning" if kind in (KIND_NO_HEALTHY, KIND_SELF_TEST_FAILED) else "info",
                    message=text,
                    details=json.dumps({"from": from_value, "to": to_value, "reason": reason}, ensure_ascii=False),
                    notified=notified,
                ))
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — алерт не должен ронять цикл
            logger.error("Exit proxy alert failed: %s", exc)
        self._last_sent[(server_id, kind)] = time.monotonic()
        return notified
