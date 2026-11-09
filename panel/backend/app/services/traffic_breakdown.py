"""Разбивка скорости сервера для окна на дашборде: по IP, интерфейсам и портам.

У каждой строки — скорость сейчас (из памяти `TrafficIngest`) и мини-график за
сутки (часовые бакеты из `server_traffic`). Строка адреса — весь его трафик:
клиенты плюс свои соединения сервера, как у интерфейса. Клиенты идут под ней
отдельно, и у них ищется резкое падение: оператор держит на сервере
заблокированный у части клиентов IP и решает, менять ли его, по тому, ушли ли с
него клиенты. Весь трафик для этого не годится — с основного адреса сервер сам
ходит в интернет за клиентов, пришедших на новый.

Арифметика — чистые функции, загрузка из БД — отдельно.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Server, ServerTraffic
from app.services.traffic_import import MIN_NODE_VERSION_FOR_IP_TRAFFIC, node_supports_ip_traffic
from app.services.traffic_ingest import ObservedSpeeds, Period, Scope, TrafficIngest, floor_hour

SPARKLINE_HOURS = 24

# Падение ищется сравнением последних часов с теми же часами сутками раньше, а не
# со средним за сутки: у VPN-трафика суточный цикл, и ночной спад против вечернего
# пика сам по себе даёт кратную разницу.
DROP_WINDOW_HOURS = 3
DROP_MIN_RATIO = 4
# Падение с почти пустого адреса не сигнал — там нечему было падать
DROP_MIN_BASELINE_BYTES_PER_SEC = 125_000  # 1 Мбит/с
# Окно, покрытое наблюдениями меньше чем на полчаса, ничего не говорит о скорости
DROP_MIN_COVERED_SECONDS = 1800

HISTORY_HOURS = 24 + DROP_WINDOW_HOURS
BREAKDOWN_SCOPES = (Scope.IFACE.value, Scope.IP.value, Scope.IP_OWN.value, Scope.PORT.value)


class ServerNotFoundError(Exception):
    def __init__(self, server_id: int):
        super().__init__(f"Server {server_id} not found")
        self.server_id = server_id


@dataclass(frozen=True)
class HourBucket:
    bytes_total: int
    covered_seconds: int


HourlyHistory = dict[datetime, HourBucket]


def average_rate(bucket: Optional[HourBucket]) -> Optional[float]:
    """Средняя скорость за наблюдённую часть часа; None — наблюдений не было."""
    if bucket is None or bucket.covered_seconds <= 0:
        return None
    return bucket.bytes_total / bucket.covered_seconds


def sparkline(history: HourlyHistory, last_hour: datetime) -> list[Optional[float]]:
    """Байт/с (приём + отдача) по часам за сутки, последняя точка — текущий час."""
    return [
        average_rate(history.get(last_hour - timedelta(hours=offset)))
        for offset in range(SPARKLINE_HOURS - 1, -1, -1)
    ]


def window_rate(history: HourlyHistory, last_hour: datetime, hours: int) -> Optional[float]:
    covered = 0
    total = 0
    for offset in range(hours):
        bucket = history.get(last_hour - timedelta(hours=offset))
        if bucket is not None:
            covered += bucket.covered_seconds
            total += bucket.bytes_total
    if covered < DROP_MIN_COVERED_SECONDS:
        return None
    return total / covered


def detect_drop(history: HourlyHistory, last_hour: datetime) -> Optional[dict]:
    """Резкое падение трафика против того же времени сутками раньше.

    None — падения нет или судить не по чему. `ratio` = во сколько раз упал;
    None внутри — трафик пропал совсем.
    """
    recent = window_rate(history, last_hour, DROP_WINDOW_HOURS)
    day_before = window_rate(history, last_hour - timedelta(days=1), DROP_WINDOW_HOURS)
    if recent is None or day_before is None:
        return None
    if day_before < DROP_MIN_BASELINE_BYTES_PER_SEC:
        return None
    if recent * DROP_MIN_RATIO > day_before:
        return None
    return {"ratio": round(day_before / recent, 1) if recent > 0 else None}


def combined_speeds(*parts: Optional[ObservedSpeeds]) -> Optional[ObservedSpeeds]:
    present = [part for part in parts if part is not None]
    if not present:
        return None
    return ObservedSpeeds(
        sum(part.rx_bytes_per_sec for part in present),
        sum(part.tx_bytes_per_sec for part in present),
    )


def combined_history(*parts: HourlyHistory) -> HourlyHistory:
    """Сумма рядов по часам. Покрытие — максимум, а не сумма: ряды адреса сняты
    одними и теми же замерами, и сложенное время вдвое занизило бы скорость."""
    merged: HourlyHistory = {}
    for part in parts:
        for hour, bucket in part.items():
            seen = merged.get(hour)
            merged[hour] = bucket if seen is None else HourBucket(
                bytes_total=seen.bytes_total + bucket.bytes_total,
                covered_seconds=max(seen.covered_seconds, bucket.covered_seconds),
            )
    return merged


def speed_view(speeds: Optional[ObservedSpeeds], history: HourlyHistory, last_hour: datetime) -> dict:
    return {
        "rx_bytes_per_sec": speeds.rx_bytes_per_sec if speeds else None,
        "tx_bytes_per_sec": speeds.tx_bytes_per_sec if speeds else None,
        "sparkline": sparkline(history, last_hour),
    }


def build_rows(
    rates: dict[str, ObservedSpeeds],
    history: dict[str, HourlyHistory],
    last_hour: datetime,
) -> list[dict]:
    """Строки одного раздела: всё, что видно сейчас или было видно за сутки.

    Порядок — по скорости сейчас, затем по объёму за сутки: замолчавший адрес
    уходит вниз, но не пропадает, пока виден на графике.
    """
    first_hour = last_hour - timedelta(hours=SPARKLINE_HOURS - 1)
    seen_today = {key for key, hours in history.items() if any(hour >= first_hour for hour in hours)}
    ranked = []
    for key in set(rates) | seen_today:
        speeds = rates.get(key)
        key_history = history.get(key, {})
        row = {"key": key, **speed_view(speeds, key_history, last_hour)}
        speed_now = speeds.rx_bytes_per_sec + speeds.tx_bytes_per_sec if speeds else -1.0
        day_volume = sum(bucket.bytes_total for bucket in key_history.values())
        ranked.append(((-speed_now, -day_volume, key), row))
    ranked.sort(key=lambda item: item[0])
    return [row for _, row in ranked]


def build_ip_rows(
    client_rates: dict[str, ObservedSpeeds],
    own_rates: dict[str, ObservedSpeeds],
    client_history: dict[str, HourlyHistory],
    own_history: dict[str, HourlyHistory],
    last_hour: datetime,
) -> list[dict]:
    """Строки адресов: весь трафик, под ним — клиенты с пометкой падения."""
    total_rates = {
        key: combined_speeds(client_rates.get(key), own_rates.get(key))
        for key in set(client_rates) | set(own_rates)
    }
    total_history = {
        key: combined_history(client_history.get(key, {}), own_history.get(key, {}))
        for key in set(client_history) | set(own_history)
    }
    rows = build_rows(total_rates, total_history, last_hour)
    for row in rows:
        clients = client_history.get(row["key"], {})
        row["clients"] = {
            **speed_view(client_rates.get(row["key"]), clients, last_hour),
            "drop": detect_drop(clients, last_hour),
        }
    return rows


async def load_hourly_history(
    db: AsyncSession, server_id: int, since: datetime
) -> dict[str, dict[str, HourlyHistory]]:
    """Часовые бакеты интерфейсов, адресов и портов: scope → ключ → час → бакет."""
    result = await db.execute(
        select(
            ServerTraffic.scope,
            ServerTraffic.scope_key,
            ServerTraffic.bucket,
            ServerTraffic.rx_bytes,
            ServerTraffic.tx_bytes,
            ServerTraffic.covered_seconds,
        ).where(
            ServerTraffic.server_id == server_id,
            ServerTraffic.period_type == Period.HOUR.value,
            ServerTraffic.scope.in_(BREAKDOWN_SCOPES),
            ServerTraffic.bucket >= since,
        )
    )
    history: dict[str, dict[str, HourlyHistory]] = {}
    for row in result.all():
        history.setdefault(row.scope, {}).setdefault(row.scope_key, {})[row.bucket] = HourBucket(
            bytes_total=int(row.rx_bytes) + int(row.tx_bytes),
            covered_seconds=int(row.covered_seconds),
        )
    return history


async def build_breakdown(
    db: AsyncSession, server_id: int, ingest: TrafficIngest, now: datetime
) -> dict:
    # Проекция одной колонки: окно открывается наведением курсора, а строка servers
    # тащит за собой тяжёлые TEXT-поля с последними метриками
    row = (await db.execute(select(Server.node_version).where(Server.id == server_id))).first()
    if row is None:
        raise ServerNotFoundError(server_id)

    last_hour = floor_hour(now)
    history = await load_hourly_history(db, server_id, last_hour - timedelta(hours=HISTORY_HOURS - 1))
    rates = ingest.live_rates(server_id, now)

    def rows_for(scope: Scope) -> list[dict]:
        return build_rows(rates.get(scope.value, {}), history.get(scope.value, {}), last_hour)

    return {
        "ips_supported": node_supports_ip_traffic(row.node_version),
        "node_version": row.node_version,
        "min_ip_version": MIN_NODE_VERSION_FOR_IP_TRAFFIC,
        "ips": build_ip_rows(
            rates.get(Scope.IP.value, {}), rates.get(Scope.IP_OWN.value, {}),
            history.get(Scope.IP.value, {}), history.get(Scope.IP_OWN.value, {}),
            last_hour,
        ),
        "interfaces": rows_for(Scope.IFACE),
        "ports": rows_for(Scope.PORT),
    }
