"""Определение online/offline статуса сервера по данным метрик-коллектора."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PanelSettings, Server

DEFAULT_OFFLINE_THRESHOLD = 60


def resolve_status(server: Server, threshold: int = DEFAULT_OFFLINE_THRESHOLD) -> str:
    """Determine server status tolerant to transient failures.

    Server is 'offline' only if last_seen is older than threshold seconds.
    A single timeout (last_error set but last_seen still fresh) is 'online'.
    """
    if not server.last_seen:
        return "offline" if server.last_error else "loading"

    now = datetime.now(timezone.utc)
    age = (now - server.last_seen).total_seconds()

    if age > threshold:
        return "offline"
    return "online"


async def get_offline_threshold(db: AsyncSession) -> int:
    """Порог офлайна с учётом настроенного интервала сбора метрик."""
    interval_row = await db.execute(
        select(PanelSettings.value).where(PanelSettings.key == "metrics_collect_interval")
    )
    interval_val = interval_row.scalar_one_or_none()
    collect_interval = int(interval_val) if interval_val else 10
    return max(DEFAULT_OFFLINE_THRESHOLD, collect_interval * 3 + 30)
