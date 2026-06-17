"""Репозиторий состояния IP-аномалии Remnawave.

Хранит по каждому пользователю: счётчик срабатываний, известные IP (анти-спам)
и message_id предыдущего уведомления для reply-threading. Используется как детектором
(`xray_stats_collector`), так и callback-хендлерами кнопок — поэтому SQL/JSON собраны здесь,
чтобы не дублировать логику и держать слои чистыми (функции принимают `db: AsyncSession`).
"""

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RemnawaveIpAnomalyState


def parse_ips(json_str: str | None) -> set[str]:
    if not json_str:
        return set()
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return set()
    if isinstance(data, list):
        return {str(ip) for ip in data}
    return set()


def dump_ips(ips: set[str]) -> str:
    return json.dumps(sorted(ips))


def seconds_since_last(state: RemnawaveIpAnomalyState, now: datetime) -> float | None:
    """Сколько секунд прошло с последнего уведомления. None — если уведомлений не было.

    `now` — naive UTC. Хранимое значение нормализуем к naive UTC: timestamptz может
    вернуться tz-aware, и вычитание из naive упало бы с TypeError.
    """
    last = state.last_notified_at
    if last is None:
        return None
    if last.tzinfo is not None:
        last = last.astimezone(timezone.utc).replace(tzinfo=None)
    return (now - last).total_seconds()


async def get_or_create(db: AsyncSession, email: int) -> RemnawaveIpAnomalyState:
    state = await db.get(RemnawaveIpAnomalyState, email)
    if state is None:
        state = RemnawaveIpAnomalyState(email=email, trigger_count=0)
        db.add(state)
    return state


async def record_notification(
    db: AsyncSession,
    state: RemnawaveIpAnomalyState,
    *,
    current_ips: set[str],
    message_id: int | None,
    chat_id: str,
    now: datetime,
) -> None:
    """Зафиксировать отправленное уведомление: +1 к счётчику, запомнить текущие IP,
    сохранить message_id/chat для будущего reply."""
    state.trigger_count = (state.trigger_count or 0) + 1
    state.known_ips = dump_ips(current_ips)
    state.last_notified_at = now
    if message_id is not None:
        state.last_message_id = message_id
        state.last_chat_id = chat_id
    await db.commit()


async def reset(db: AsyncSession, email: int) -> None:
    """Сбросить счётчик и забыть известные IP — следующее срабатывание с чистого листа."""
    state = await db.get(RemnawaveIpAnomalyState, email)
    if state is None:
        return
    state.trigger_count = 0
    state.known_ips = None
    state.last_message_id = None
    state.last_chat_id = None
    state.last_notified_at = None
    await db.commit()
