"""Жив ли туннель со стороны DDoS-Guard — по входящим пакетам интерфейса.

DDoS-Guard регулярно пингует CE из своей сети проверок через туннель, поэтому
счётчик входящих пакетов туннеля растёт даже без клиентов. Стоит дольше
`SILENT_AFTER_SECONDS` — до сервера не доходит ни трафик, ни проверки: туннель
у DDoS-Guard неактивен, хостер режет GRE или упал сервер на их стороне.
Отдельного пинга и правил firewall для этого не нужно.

Счётчик снимает цикл панели раз в минуту, поэтому время последнего пакета
известно с точностью до тика. Чистые функции, без БД и сети.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

SILENT_AFTER_SECONDS = 300


class Activity(str, Enum):
    ALIVE = "alive"       # пакеты идут
    WAITING = "waiting"   # туннель только что появился, ещё не с чем сравнить
    SILENT = "silent"     # пакетов нет дольше SILENT_AFTER_SECONDS


class ActivityEvent(str, Enum):
    SILENT = "silent"
    RECOVERED = "recovered"


@dataclass(frozen=True)
class ActivityChange:
    name: str
    ce: str
    event: ActivityEvent


def _parse(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def update_activity(
    previous: dict[str, dict], tunnels: list[dict], now: datetime,
) -> tuple[dict[str, dict], list[ActivityChange]]:
    """Новые отметки по туннелям из состояния ноды и переходы для уведомлений.

    «Замолчал» сообщается только про туннель, по которому пакеты уже шли: пока
    DDoS-Guard не включил защиту, новый туннель молчит законно.
    """
    silence = timedelta(seconds=SILENT_AFTER_SECONDS)
    stamp = now.isoformat()
    activity: dict[str, dict] = {}
    changes: list[ActivityChange] = []
    for tunnel in tunnels:
        name, ce = tunnel.get("name"), tunnel.get("ce") or ""
        if not name:
            continue
        rx_packets = int(tunnel.get("rx_packets") or 0)
        before = previous.get(name)
        if before is None:
            activity[name] = {"rx_packets": rx_packets, "changed_at": None, "first_seen_at": stamp, "silent": False}
            continue
        entry = dict(before)
        changed_at = _parse(before.get("changed_at"))
        # Счётчик сбрасывается с пересозданием интерфейса — любое отличие значит, что пакеты были
        if rx_packets != before.get("rx_packets"):
            entry.update(rx_packets=rx_packets, changed_at=stamp)
            if before.get("silent"):
                entry["silent"] = False
                changes.append(ActivityChange(name, ce, ActivityEvent.RECOVERED))
        elif changed_at is not None and not before.get("silent") and now - changed_at >= silence:
            entry["silent"] = True
            changes.append(ActivityChange(name, ce, ActivityEvent.SILENT))
        activity[name] = entry
    return activity, changes


def activity_status(entry: Optional[dict], now: datetime) -> tuple[Activity, Optional[str]]:
    """(состояние, время последнего замеченного пакета ISO) для карточки туннеля."""
    if entry is None:
        return Activity.WAITING, None
    changed_at = _parse(entry.get("changed_at"))
    silence = timedelta(seconds=SILENT_AFTER_SECONDS)
    if changed_at is not None:
        status = Activity.ALIVE if now - changed_at < silence else Activity.SILENT
        return status, entry.get("changed_at")
    first_seen = _parse(entry.get("first_seen_at"))
    if first_seen is not None and now - first_seen >= silence:
        return Activity.SILENT, None
    return Activity.WAITING, None
