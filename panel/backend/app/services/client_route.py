"""Маршрут клиента на карте: трасса с ноды → остановки в городах → участки линии.

Трассу (mtr) гоняет нода, к которой подключён клиент, — от ноды к клиенту.
Каждому ответившему узлу ищется город по базе DB-IP, но город роутера база знает
хуже, чем город абонента: магистральные адреса часто записаны на головной офис
оператора. Поэтому место проверяется физикой: за 1 мс туда-обратно сигнал в
оптике проходит не дальше ~100 км, и узел, ответивший быстрее, чем свет дошёл бы
до указанного города, остаётся без места.

Задержка участка — разница сглаженных задержек на его концах. Роутер, который сам
медленно отвечает на ICMP, даёт всплеск, которого нет у узлов дальше, поэтому у
каждого узла берётся минимум по нему и всем следующим. Участок краснеет, когда
добавленная задержка заметно больше, чем объясняет его длина.

Показ — от клиента к ноде: так идёт трафик пользователя.
"""

import asyncio
import ipaddress
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RemnawaveNodeCache, Server
from app.services.geoip import GeoRecord
from app.services.net_utils import host_to_ip
from app.services.node_ips import public_interface_ipv4, url_host

Point = tuple[float, float]  # (lon, lat)

EARTH_RADIUS_KM = 6371.0
# Свет в оптике — ~200 км/мс в одну сторону, значит за 1 мс туда-обратно не дальше 100 км
KM_PER_RTT_MS = 100.0
# Кабель не идёт по прямой: путь по сети обычно на треть длиннее расстояния между городами
FIBER_DETOUR = 1.3
CURVE_BEND = 0.12
CURVE_STEP_KM = 15.0
CURVE_MIN_STEPS = 16
CURVE_MAX_STEPS = 96
# База ошибается в пределах области: город провайдера вместо соседнего города роутера
GEO_SLACK_KM = 150.0
SAME_STOP_KM = 40.0
LEG_WARN_EXCESS_MS = 7.0
LEG_BAD_EXCESS_MS = 15.0
COORD_DECIMALS = 4


class StopKind(str, Enum):
    CLIENT = "client"
    ROUTER = "router"
    NODE = "node"


class LegStatus(str, Enum):
    OK = "ok"
    WARN = "warn"
    BAD = "bad"


class HiddenReason(str, Enum):
    SILENT = "silent"
    PRIVATE = "private"
    NO_GEO = "no_geo"
    IMPOSSIBLE = "impossible"


@dataclass(frozen=True)
class RoutePoint:
    lat: float
    lon: float
    place: Optional[str]
    country_code: Optional[str]

    @classmethod
    def from_geo(cls, geo: Optional[GeoRecord]) -> Optional["RoutePoint"]:
        if geo is None or geo.lat is None or geo.lon is None:
            return None
        return cls(geo.lat, geo.lon, geo.place or None, geo.country_code)

    @property
    def lon_lat(self) -> Point:
        return round(self.lon, COORD_DECIMALS), round(self.lat, COORD_DECIMALS)


@dataclass
class _Stop:
    kind: StopKind
    point: RoutePoint
    rtt_ms: Optional[float]
    hops: list[dict] = field(default_factory=list)
    hidden_before: list[dict] = field(default_factory=list)


def haversine_km(a: Point, b: Point) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def _km(a: RoutePoint, b: RoutePoint) -> float:
    return haversine_km(a.lon_lat, b.lon_lat)


def leg_curve(start: Point, end: Point) -> list[list[float]]:
    """Плавная дуга между городами (квадратичная кривая Безье): соседние участки не
    сливаются в одну прямую. Через антимеридиан идёт коротким путём."""
    end_lon = end[0]
    if end_lon - start[0] > 180:
        end_lon -= 360
    elif end_lon - start[0] < -180:
        end_lon += 360
    dx, dy = end_lon - start[0], end[1] - start[1]
    control = ((start[0] + end_lon) / 2 - dy * CURVE_BEND, (start[1] + end[1]) / 2 + dx * CURVE_BEND)
    steps = max(CURVE_MIN_STEPS, min(CURVE_MAX_STEPS, int(haversine_km(start, end) / CURVE_STEP_KM)))
    points = []
    for i in range(steps + 1):
        t = i / steps
        lon = (1 - t) ** 2 * start[0] + 2 * (1 - t) * t * control[0] + t * t * end_lon
        lat = (1 - t) ** 2 * start[1] + 2 * (1 - t) * t * control[1] + t * t * end[1]
        points.append([round(lon, COORD_DECIMALS), round(lat, COORD_DECIMALS)])
    return points


def _answered(hop: dict) -> bool:
    return bool(hop.get("host")) and (hop.get("received") or 0) > 0 and hop.get("avg_ms") is not None


def _hop_view(hop: dict, reason: Optional[HiddenReason] = None) -> dict:
    return {
        "hop": hop["hop"], "host": hop.get("host"), "asn": hop.get("asn"), "as_name": hop.get("as_name"),
        "avg_ms": hop.get("avg_ms"), "loss_pct": hop.get("loss_pct"), "reason": reason,
    }


def _smoothed_rtt(hops: list[dict]) -> dict[int, float]:
    smoothed: dict[int, float] = {}
    floor = math.inf
    for hop in reversed(hops):
        if _answered(hop):
            floor = min(floor, hop["avg_ms"])
            smoothed[hop["hop"]] = floor
    return smoothed


def _locate(hop: dict, node: RoutePoint,
            lookup: Callable[[str], Optional[GeoRecord]]) -> tuple[Optional[RoutePoint], Optional[HiddenReason]]:
    if not _answered(hop):
        return None, HiddenReason.SILENT
    try:
        address = ipaddress.ip_address(hop["host"])
    except ValueError:
        return None, HiddenReason.NO_GEO
    if not address.is_global:
        return None, HiddenReason.PRIVATE
    place = RoutePoint.from_geo(lookup(hop["host"]))
    if place is None:
        return None, HiddenReason.NO_GEO
    fastest = hop["best_ms"] if hop.get("best_ms") is not None else hop["avg_ms"]
    if _km(node, place) > max(GEO_SLACK_KM, fastest * KM_PER_RTT_MS):
        return None, HiddenReason.IMPOSSIBLE
    return place, None


def _leg_status(added_ms: Optional[float], expected_ms: float) -> Optional[LegStatus]:
    if added_ms is None:
        return None
    excess = added_ms - expected_ms
    if excess > LEG_BAD_EXCESS_MS:
        return LegStatus.BAD
    if excess > LEG_WARN_EXCESS_MS:
        return LegStatus.WARN
    return LegStatus.OK


def _leg_view(outer: _Stop, inner: _Stop) -> dict:
    km = _km(outer.point, inner.point)
    added = None if outer.rtt_ms is None or inner.rtt_ms is None else max(0.0, outer.rtt_ms - inner.rtt_ms)
    expected = km * FIBER_DETOUR / KM_PER_RTT_MS
    return {
        "coords": leg_curve(outer.point.lon_lat, inner.point.lon_lat),
        "km": round(km, 1),
        "added_ms": round(added, 1) if added is not None else None,
        "expected_ms": round(expected, 1),
        "status": _leg_status(added, expected),
        "hidden": outer.hidden_before,
        # Клиент не ответил: последний участок — по городу из базы, без задержки
        "last_mile": outer.kind is StopKind.CLIENT and outer.rtt_ms is None,
    }


def _stop_view(stop: _Stop) -> dict:
    return {
        "kind": stop.kind, "lat": stop.point.lat, "lon": stop.point.lon,
        "place": stop.point.place, "country_code": stop.point.country_code,
        "rtt_ms": round(stop.rtt_ms, 1) if stop.rtt_ms is not None else None,
        "hops": stop.hops,
    }


def assemble_route(trace: dict, node: RoutePoint, client: Optional[RoutePoint],
                   lookup: Callable[[str], Optional[GeoRecord]]) -> dict:
    """Остановки и участки от клиента к ноде по ходу трассы (можно звать на каждом опросе)."""
    hops = sorted(trace.get("hops") or [], key=lambda h: h["hop"])
    client_ip = trace.get("ip")
    smoothed = _smoothed_rtt(hops)

    stops = [_Stop(StopKind.NODE, node, 0.0)]
    pending: list[dict] = []
    client_hop: Optional[dict] = None
    for hop in hops:
        if hop.get("host") == client_ip:
            client_hop = hop if _answered(hop) else None
            continue
        place, reason = _locate(hop, node, lookup)
        if place is None:
            pending.append(_hop_view(hop, reason))
        elif _km(stops[-1].point, place) < SAME_STOP_KM:
            stops[-1].hops.append(_hop_view(hop))
        else:
            stops.append(_Stop(StopKind.ROUTER, place, smoothed[hop["hop"]], [_hop_view(hop)], pending))
            pending = []

    hidden_tail: list[dict] = []
    client_views = [_hop_view(client_hop)] if client_hop else []
    client_rtt = smoothed[client_hop["hop"]] if client_hop else None
    last = stops[-1]
    if client is None:
        hidden_tail = pending
    elif last.kind is StopKind.ROUTER and _km(last.point, client) < SAME_STOP_KM:
        last.kind, last.point = StopKind.CLIENT, client
        last.hops += client_views
        if client_rtt is not None:
            last.rtt_ms = client_rtt
        hidden_tail = pending
    else:
        stops.append(_Stop(StopKind.CLIENT, client, client_rtt, client_views, pending))

    ordered = stops[::-1]
    legs = [_leg_view(outer, inner) for outer, inner in zip(ordered, ordered[1:])]
    known_rtt = client_rtt if client_rtt is not None else max(smoothed.values(), default=None)
    return {
        "client_reached": client_hop is not None,
        "client_located": client is not None,
        "stops": [_stop_view(stop) for stop in ordered],
        "legs": legs,
        "hidden_tail": hidden_tail,
        "totals": {
            "rtt_ms": round(known_rtt, 1) if known_rtt is not None else None,
            "km": round(sum(leg["km"] for leg in legs), 1),
            "direct_km": round(_km(ordered[0].point, node), 1),
        },
    }


# ---------------------------------------------------------------------------
# Нода Remnawave ↔ сервер панели
# ---------------------------------------------------------------------------

async def find_node_server(db: AsyncSession, node_uuid: str) -> Optional[Server]:
    """Сервер панели, на котором стоит нода Remnawave: адрес ноды совпадает с хостом
    URL сервера или с одним из его публичных адресов из метрик."""
    node = await db.get(RemnawaveNodeCache, node_uuid)
    if node is None or not node.address:
        return None
    node_ip = await host_to_ip(node.address)
    servers = (await db.execute(select(Server).where(Server.is_active == True))).scalars().all()  # noqa: E712
    hosts = [url_host(server.url) or "" for server in servers]
    for server, host in zip(servers, hosts):
        if host in (node.address, node_ip) or (node_ip and node_ip in public_interface_ipv4(server.last_metrics)):
            return server
    if not node_ip:
        return None
    resolved = await asyncio.gather(*(host_to_ip(host) for host in hosts))
    return next((server for server, ip in zip(servers, resolved) if ip == node_ip), None)


async def node_route_point(db: AsyncSession, node_uuid: str, lookup: Callable[[str], Optional[GeoRecord]]) -> Optional[RoutePoint]:
    """Место ноды: координаты, которые сборщик уже нашёл по её адресу, — опрос раз в
    секунду не резолвит адрес заново; поиск по базе — только если их ещё нет."""
    node = await db.get(RemnawaveNodeCache, node_uuid)
    if node is None:
        return None
    if node.lat is not None and node.lon is not None:
        return RoutePoint(node.lat, node.lon, None, node.country_code)
    node_ip = await host_to_ip(node.address) if node.address else None
    return RoutePoint.from_geo(lookup(node_ip)) if node_ip else None
