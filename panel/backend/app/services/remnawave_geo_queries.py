"""Чтение карты клиентов Remnawave: точки карты, таблица IP, история IP пользователя.

Данные меняются только раз в цикл сбора, поэтому ответы кэшируются до следующего
цикла (номер цикла — remnawave_geo_ingest.cycle_version()).
"""

import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Optional

from sqlalchemy import distinct, func as sql_func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    IpGeo, RemnawaveGeoPresence, RemnawaveIpHistory, RemnawaveNodeCache, RemnawaveSettings,
    RemnawaveUserCache, XrayStats,
)
from app.services.geoip import GeoRecord, geoip_status, get_geoip
from app.services.remnawave_geo_ingest import (
    BUCKET_TOTAL_KEY, DEFAULT_IP_HISTORY_DAYS, PlaceCount, cycle_version, fleet_ip_names, place_counts,
)

COMPARE_LAG = timedelta(hours=24)
NODE_LIST_FRESHNESS = timedelta(days=1)
USERS_PER_ROW = 3
USER_HISTORY_LIMIT = 500
CACHE_MAX_ENTRIES = 256

SortColumn = Literal["last_seen", "ip", "city", "provider"]
SORT_SQL: dict[str, str] = {
    "last_seen": "COALESCE(o.last_seen, b.last_seen)",
    "ip": "b.ip",
    "city": "g.place",
    "provider": "g.as_org",
}

BBox = tuple[float, float, float, float]


@dataclass
class PrevPlace:
    lat: float
    lon: float
    ips: float


@dataclass(frozen=True)
class IpTableQuery:
    bbox: Optional[tuple[BBox, ...]] = None
    place: Optional[tuple[str, str]] = None
    node: Optional[str] = None
    search: Optional[str] = None
    active_only: bool = True
    sort: SortColumn = "last_seen"
    descending: bool = True
    offset: int = 0
    limit: int = 50


class CycleCache:
    """Кэш ответов в пределах одного цикла сбора: записи прошлых циклов выбрасываются
    сразу, а число записей ограничено — ключи (фильтры таблицы) приходят от клиента."""

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES):
        self._max_entries = max_entries
        self._cycle: Optional[int] = None
        self._entries: OrderedDict[tuple, dict] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def _sync(self, cycle: int) -> None:
        if cycle != self._cycle:
            self._cycle = cycle
            self._entries.clear()

    def get(self, cycle: int, key: tuple) -> Optional[dict]:
        self._sync(cycle)
        value = self._entries.get(key)
        if value is not None:
            self._entries.move_to_end(key)
        return value

    def put(self, cycle: int, key: tuple, value: dict) -> dict:
        self._sync(cycle)
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return value


_cache = CycleCache()


def place_key(country_code: str, place: str) -> str:
    return f"{country_code}|{place}"


def split_place_key(key: Optional[str]) -> Optional[tuple[str, str]]:
    if not key or "|" not in key:
        return None
    country_code, place = key.split("|", 1)
    return (country_code, place) if len(country_code) == 2 else None


def parse_bbox(raw: Optional[str]) -> Optional[tuple[BBox, ...]]:
    """Границы карты «w,s,e,n» → прямоугольники в [-180, 180].

    Карта, прокрученная на соседнюю копию мира или через антимеридиан, отдаёт
    долготы за пределами ±180 — их нужно свернуть, иначе таблица опустеет.
    """
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        return None
    try:
        west, south, east, north = (float(p) for p in parts)
    except ValueError:
        return None
    if any(math.isnan(v) or math.isinf(v) for v in (west, south, east, north)) or south > north:
        return None
    south, north = max(south, -90.0), min(north, 90.0)
    if east < west:
        east += 360
    if east - west >= 360:
        return ((-180.0, south, 180.0, north),)
    west_norm = ((west + 180) % 360) - 180
    east_norm = west_norm + (east - west)
    if east_norm <= 180:
        return ((west_norm, south, east_norm, north),)
    return ((west_norm, south, 180.0, north), (-180.0, south, east_norm - 360, north))


def merge_prev(
    now: dict[tuple[str, str], PlaceCount],
    prev: Optional[dict[tuple[str, str], PrevPlace]],
) -> list[dict]:
    rows = []
    for (country_code, place), count in now.items():
        prev_ips = None
        if prev is not None:
            known = prev.get((country_code, place))
            prev_ips = round(known.ips) if known else 0
        rows.append({
            "key": place_key(country_code, place), "country_code": country_code, "place": place,
            "lat": round(count.lat, 4), "lon": round(count.lon, 4),
            "ips": count.ips, "users": count.users, "prev": prev_ips,
        })
    # Места, где вчера были клиенты, а сейчас никого, отдаются все: их вчерашние
    # клиенты нужны суммам кластеров и стран — регион из многих мелких городов,
    # где все пропали, иначе просто исчез бы с карты. Рисовать ли место отдельным
    # красным контуром, решает фронт по своему порогу.
    for (country_code, place), old in (prev or {}).items():
        prev_ips = round(old.ips)
        if (country_code, place) in now or prev_ips < 1:
            continue
        rows.append({
            "key": place_key(country_code, place), "country_code": country_code, "place": place,
            "lat": round(old.lat, 4), "lon": round(old.lon, 4),
            "ips": 0, "users": 0, "prev": prev_ips,
        })
    return rows


def country_rollup(locations: list[dict]) -> list[dict]:
    totals: dict[str, dict] = {}
    for loc in locations:
        weight = max(loc["ips"], loc["prev"] or 0, 1)
        acc = totals.setdefault(loc["country_code"], {"weight": 0.0, "lat": 0.0, "lon": 0.0, "ips": 0, "prev": None})
        acc["weight"] += weight
        acc["lat"] += loc["lat"] * weight
        acc["lon"] += loc["lon"] * weight
        acc["ips"] += loc["ips"]
        if loc["prev"] is not None:
            acc["prev"] = (acc["prev"] or 0) + loc["prev"]
    return [
        {
            "country_code": country_code,
            "lat": round(acc["lat"] / acc["weight"], 4),
            "lon": round(acc["lon"] / acc["weight"], 4),
            "ips": acc["ips"],
            "prev": acc["prev"],
        }
        for country_code, acc in totals.items()
    ]


def _geo_record(country_code, region, place, lat, lon, asn, as_org) -> Optional[GeoRecord]:
    if country_code is None and asn is None:
        return None
    return GeoRecord(country_code, region, place or "", lat, lon, asn, as_org)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


async def history_days(db: AsyncSession) -> int:
    days = (await db.execute(select(RemnawaveSettings.ip_history_days).limit(1))).scalar()
    return days or DEFAULT_IP_HISTORY_DAYS


async def summary_extra(db: AsyncSession, now: datetime) -> dict:
    cycle = cycle_version()
    cached = _cache.get(cycle, ("summary",))
    if cached:
        return cached
    days = await history_days(db)
    relays = list(await fleet_ip_names())

    cities_online = (await db.execute(
        select(sql_func.count()).select_from(
            select(IpGeo.country_code, IpGeo.place)
            .join(XrayStats, XrayStats.source_ip == IpGeo.ip)
            .where(IpGeo.lat.is_not(None), IpGeo.place != "")
            .distinct()
            .subquery()
        )
    )).scalar() or 0
    ips_history = (await db.execute(
        select(sql_func.count(distinct(RemnawaveIpHistory.source_ip)))
        .where(RemnawaveIpHistory.last_seen >= now - timedelta(days=days))
    )).scalar() or 0
    relay_users = 0
    if relays:
        relay_users = (await db.execute(
            select(sql_func.count(distinct(XrayStats.email))).where(XrayStats.source_ip.in_(relays))
        )).scalar() or 0

    return _cache.put(cycle, ("summary",), {
        "cities_online": cities_online,
        "ips_history": ips_history,
        "history_days": days,
        "relay_users": relay_users,
    })


async def _prev_places(db: AsyncSession, now: datetime) -> Optional[dict[tuple[str, str], PrevPlace]]:
    bucket = (now - COMPARE_LAG).replace(minute=0, second=0, microsecond=0)
    rows = (await db.execute(
        select(RemnawaveGeoPresence).where(RemnawaveGeoPresence.bucket == bucket)
    )).scalars().all()
    total = next((r for r in rows if (r.country_code, r.place) == BUCKET_TOTAL_KEY), None)
    if total is None or not total.samples:
        return None
    return {
        (r.country_code, r.place): PrevPlace(r.lat, r.lon, r.ips_sum / total.samples)
        for r in rows
        if (r.country_code, r.place) != BUCKET_TOTAL_KEY
    }


async def _node_counts(db: AsyncSession, now: datetime) -> list[dict]:
    expanded = select(
        sql_func.unnest(XrayStats.node_uuids).label("node"), XrayStats.source_ip,
    ).subquery()
    counts = dict((await db.execute(
        select(expanded.c.node, sql_func.count(distinct(expanded.c.source_ip))).group_by(expanded.c.node)
    )).all())
    nodes = (await db.execute(
        select(RemnawaveNodeCache)
        .where(RemnawaveNodeCache.updated_at >= now - NODE_LIST_FRESHNESS)
        .order_by(RemnawaveNodeCache.name)
    )).scalars().all()
    return [
        {"uuid": n.uuid, "name": n.name, "country_code": n.country_code, "ips": counts.get(n.uuid, 0)}
        for n in nodes
    ]


async def map_data(db: AsyncSession, node: Optional[str], now: datetime) -> dict:
    cycle = cycle_version()
    cached = _cache.get(cycle, ("map", node))
    if cached:
        return cached

    relays = await fleet_ip_names()
    query = select(
        XrayStats.source_ip, XrayStats.email,
        IpGeo.country_code, IpGeo.region, IpGeo.place, IpGeo.lat, IpGeo.lon, IpGeo.asn, IpGeo.as_org,
    ).outerjoin(IpGeo, IpGeo.ip == XrayStats.source_ip)
    if node:
        query = query.where(XrayStats.node_uuids.any(node))
    snapshot = (await db.execute(query)).all()

    located, unlocated, relay_users = [], set(), set()
    for ip, user_id, *geo_cols in snapshot:
        if ip in relays:
            relay_users.add(user_id)
            continue
        geo = _geo_record(*geo_cols)
        if geo is None or geo.lat is None:
            unlocated.add(ip)
            continue
        located.append((ip, user_id, geo))

    prev = None if node else await _prev_places(db, now)
    locations = merge_prev(place_counts(located), prev)
    geo_ready = get_geoip().ready
    return _cache.put(cycle, ("map", node), {
        "locations": locations,
        "countries": country_rollup(locations),
        "nodes": await _node_counts(db, now),
        "compare_available": prev is not None,
        "relay_users": len(relay_users),
        "unlocated_ips": len(unlocated),
        "geo_ready": geo_ready,
        "geo_error": None if geo_ready else geoip_status()["error"],
    })


def _geo_filter_sql(query: IpTableQuery, params: dict) -> Optional[str]:
    """Фильтр по видимой области или месту — подзапросом по ip_geo, чтобы он
    срабатывал до группировки, а не после агрегации всего снимка или истории."""
    conditions = []
    if query.bbox:
        boxes = []
        for i, (west, south, east, north) in enumerate(query.bbox):
            boxes.append(f"(g.lon BETWEEN :w{i} AND :e{i} AND g.lat BETWEEN :s{i} AND :n{i})")
            params.update({f"w{i}": west, f"s{i}": south, f"e{i}": east, f"n{i}": north})
        conditions.append("(" + " OR ".join(boxes) + ")")
    if query.place:
        conditions.append("g.country_code = :place_cc AND g.place = :place_name")
        params["place_cc"], params["place_name"] = query.place
    if not conditions:
        return None
    return "SELECT g.ip FROM ip_geo g WHERE " + " AND ".join(conditions)


def _ips_table_sql(query: IpTableQuery) -> tuple[str, dict]:
    params: dict = {"limit": query.limit, "offset": query.offset}
    geo_ips = _geo_filter_sql(query, params)
    if query.active_only:
        filters = []
        if query.node:
            filters.append(":node = ANY(s.node_uuids)")
        if geo_ips:
            filters.append(f"s.source_ip IN ({geo_ips})")
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        base = f"""
            SELECT s.source_ip AS ip, max(s.last_seen) AS last_seen,
                   array_agg(DISTINCT s.email) AS user_ids,
                   array_remove(array_agg(DISTINCT n.node), NULL) AS node_uuids
            FROM xray_stats s LEFT JOIN LATERAL unnest(s.node_uuids) AS n(node) ON true
            {where}
            GROUP BY s.source_ip"""
    else:
        filters = ["h.last_seen >= :since"]
        if query.node:
            filters.append("h.node_uuid = :node")
        if geo_ips:
            filters.append(f"h.source_ip IN ({geo_ips})")
        base = f"""
            SELECT h.source_ip AS ip, max(h.last_seen) AS last_seen,
                   array_agg(DISTINCT h.user_id) AS user_ids,
                   array_agg(DISTINCT h.node_uuid) AS node_uuids
            FROM remnawave_ip_history h
            WHERE {" AND ".join(filters)}
            GROUP BY h.source_ip"""
    if query.node:
        params["node"] = query.node

    where = ""
    if query.search:
        where = (
            "WHERE (b.ip ILIKE :q OR g.place ILIKE :q OR g.as_org ILIKE :q OR EXISTS ("
            " SELECT 1 FROM remnawave_user_cache u WHERE u.email = ANY(b.user_ids)"
            " AND (u.username ILIKE :q OR CAST(u.email AS TEXT) = :q_exact)))"
        )
        params["q"] = f"%{query.search}%"
        params["q_exact"] = query.search

    direction = "DESC" if query.descending else "ASC"
    sql = f"""
        WITH b AS ({base}),
             o AS (SELECT source_ip, max(last_seen) AS last_seen FROM xray_stats GROUP BY source_ip)
        SELECT b.ip, COALESCE(o.last_seen, b.last_seen) AS last_seen, (o.source_ip IS NOT NULL) AS online,
               b.user_ids, b.node_uuids,
               g.country_code, g.place, g.lat, g.lon, g.asn, g.as_org,
               count(*) OVER () AS total
        FROM b
        LEFT JOIN o ON o.source_ip = b.ip
        LEFT JOIN ip_geo g ON g.ip = b.ip
        {where}
        ORDER BY {SORT_SQL[query.sort]} {direction} NULLS LAST, b.ip
        LIMIT :limit OFFSET :offset"""
    return sql, params


async def _usernames(db: AsyncSession, user_ids: set[int]) -> dict[int, Optional[str]]:
    if not user_ids:
        return {}
    rows = await db.execute(
        select(RemnawaveUserCache.email, RemnawaveUserCache.username)
        .where(RemnawaveUserCache.email.in_(list(user_ids)))
    )
    return dict(rows.all())


async def _nodes_by_uuid(db: AsyncSession, uuids: set[str]) -> dict[str, RemnawaveNodeCache]:
    if not uuids:
        return {}
    rows = await db.execute(select(RemnawaveNodeCache).where(RemnawaveNodeCache.uuid.in_(list(uuids))))
    return {n.uuid: n for n in rows.scalars().all()}


def _node_brief(uuid: str, nodes: dict[str, RemnawaveNodeCache]) -> dict:
    node = nodes.get(uuid)
    return {"uuid": uuid, "name": node.name if node else None, "country_code": node.country_code if node else None}


async def ips_table(db: AsyncSession, query: IpTableQuery, now: datetime) -> dict:
    cycle = cycle_version()
    cache_key = ("ips", query)
    cached = _cache.get(cycle, cache_key)
    if cached:
        return cached

    sql, params = _ips_table_sql(query)
    if not query.active_only:
        params["since"] = now - timedelta(days=await history_days(db))
    rows = (await db.execute(text(sql), params)).mappings().all()

    shown_users = {uid for r in rows for uid in sorted(r["user_ids"])[:USERS_PER_ROW]}
    usernames = await _usernames(db, shown_users)
    nodes = await _nodes_by_uuid(db, {u for r in rows for u in (r["node_uuids"] or [])})
    relays = await fleet_ip_names()

    return _cache.put(cycle, cache_key, {
        "rows": [
            {
                "ip": r["ip"],
                "last_seen": _iso(r["last_seen"]),
                "online": r["online"],
                "users": [{"id": uid, "username": usernames.get(uid)} for uid in sorted(r["user_ids"])[:USERS_PER_ROW]],
                "users_total": len(r["user_ids"]),
                "nodes": [_node_brief(u, nodes) for u in sorted(r["node_uuids"] or [])],
                "country_code": r["country_code"],
                "place": r["place"],
                "asn": r["asn"],
                "as_org": r["as_org"],
                "lat": r["lat"],
                "lon": r["lon"],
                "relay": relays.get(r["ip"]),
            }
            for r in rows
        ],
        "total": rows[0]["total"] if rows else 0,
    })


async def user_geo(db: AsyncSession, user_id: int, now: datetime) -> dict:
    days = await history_days(db)
    since = now - timedelta(days=days)
    history_filter = (RemnawaveIpHistory.user_id == user_id, RemnawaveIpHistory.last_seen >= since)
    ips_total = (await db.execute(
        select(sql_func.count(distinct(RemnawaveIpHistory.source_ip))).where(*history_filter)
    )).scalar() or 0
    # Карточка показывает последние адреса; у общей подписки их бывают тысячи,
    # и список целиком не нужен ни человеку, ни ответу API
    history_rows = (await db.execute(
        select(
            RemnawaveIpHistory.source_ip,
            sql_func.min(RemnawaveIpHistory.first_seen),
            sql_func.max(RemnawaveIpHistory.last_seen).label("last_seen"),
            sql_func.array_agg(distinct(RemnawaveIpHistory.node_uuid)),
        )
        .where(*history_filter)
        .group_by(RemnawaveIpHistory.source_ip)
        .order_by(sql_func.max(RemnawaveIpHistory.last_seen).desc())
        .limit(USER_HISTORY_LIMIT)
    )).all()
    snapshot_rows = (await db.execute(
        select(XrayStats.source_ip, XrayStats.last_seen, XrayStats.node_uuids).where(XrayStats.email == user_id)
    )).all()

    ips: dict[str, dict] = {
        ip: {"first_seen": first, "last_seen": last, "nodes": set(node_uuids or []), "online": False}
        for ip, first, last, node_uuids in history_rows
    }
    for ip, last_seen, node_uuids in snapshot_rows:
        entry = ips.setdefault(ip, {"first_seen": last_seen, "last_seen": last_seen, "nodes": set(), "online": True})
        entry["online"] = True
        entry["last_seen"] = last_seen
        entry["nodes"].update(node_uuids or [])

    geo_rows = (await db.execute(select(IpGeo).where(IpGeo.ip.in_(list(ips))))).scalars().all() if ips else []
    geo = {g.ip: g for g in geo_rows}
    relays = await fleet_ip_names()
    all_nodes = {u for e in ips.values() for u in e["nodes"]}
    nodes = await _nodes_by_uuid(db, all_nodes)

    history = []
    for ip, entry in ips.items():
        g = geo.get(ip)
        history.append({
            "source_ip": ip,
            "first_seen": _iso(entry["first_seen"]),
            "last_seen": _iso(entry["last_seen"]),
            "online": entry["online"],
            "nodes": sorted(entry["nodes"]),
            "country_code": g.country_code if g else None,
            "place": g.place if g else None,
            "lat": g.lat if g else None,
            "lon": g.lon if g else None,
            "asn": g.asn if g else None,
            "as_org": g.as_org if g else None,
            "relay": relays.get(ip),
        })
    history.sort(key=lambda h: (h["online"], h["last_seen"] or ""), reverse=True)

    return {
        "history": history,
        "history_days": days,
        "nodes": [
            {**_node_brief(u, nodes), "lat": nodes[u].lat if u in nodes else None,
             "lon": nodes[u].lon if u in nodes else None}
            for u in sorted(all_nodes)
        ],
        "summary": {
            "cities": len({(h["country_code"], h["place"]) for h in history if h["place"]}),
            "providers": len({h["asn"] for h in history if h["asn"]}),
            "nodes": len(all_nodes),
            "ips_history": max(ips_total, len(history)),
        },
    }
