"""Запись цикла сбора Remnawave для карты клиентов.

Из ответов нод собирается текущий снимок (с нодами, через которые виден IP) и
история; затем IP резолвятся по локальной гео-базе и складываются в почасовые
суммы по местам — по ним карта сравнивает «сейчас» с тем же часом вчера.
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import chain
from typing import Iterable, Optional
from urllib.parse import urlparse

from sqlalchemy import delete, select, text, true
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.models import IpGeo, RemnawaveGeoPresence, RemnawaveIpHistory, RemnawaveNodeCache, Server
from app.services.geoip import GeoRecord, get_geoip
from app.services.net_utils import host_to_ip

DEFAULT_IP_HISTORY_DAYS = 30
HISTORY_TOUCH_INTERVAL = timedelta(hours=1)
PRESENCE_KEEP = timedelta(days=8)
GEO_RESOLVE_LIMIT = 50_000
GEO_FETCH_CHUNK = 10_000
UPSERT_BATCH = 500
HISTORY_CHUNK = 5000
FLEET_IPS_TTL_SEC = 600
MAX_IP_LEN = 45

# Строка-счётчик циклов часа: среднее места = его сумма / число циклов часа,
# а не / число циклов, в которых место вообще было — иначе место, где клиенты
# были полчаса, выглядело бы как место, где они были весь час.
BUCKET_TOTAL_KEY = ("", "")

ParsedKey = tuple[int, str]
HistoryKey = tuple[int, str, str]


@dataclass
class NodeAnswer:
    node_uuid: str
    entries: list[dict]
    received_at: datetime
    # Нода на опрос не ответила: её клиенты на этот цикл выпадают из снимка
    failed: bool = False


@dataclass
class SnapshotEntry:
    last_seen: datetime
    node_uuids: set[str] = field(default_factory=set)


@dataclass
class PlaceCount:
    lat: float
    lon: float
    ips: int
    users: int


def parse_remnawave_time(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError:
        return None


def _user_id(raw) -> Optional[int]:
    try:
        user_id = int(raw)
    except (TypeError, ValueError):
        return None
    return user_id or None


def merge_node_results(
    answers: list[NodeAnswer],
    allowed_user_ids: set[int],
    fresh_window: timedelta,
) -> tuple[dict[ParsedKey, SnapshotEntry], dict[HistoryKey, datetime]]:
    snapshot: dict[ParsedKey, SnapshotEntry] = {}
    history: dict[HistoryKey, datetime] = {}
    for answer in answers:
        # Свежесть — от ответа самой ноды, а не от конца цикла: большой парк
        # опрашивается дольше интервала сбора, и общий порог выбросил бы из снимка
        # всех клиентов нод, опрошенных первыми.
        fresh_after = answer.received_at - fresh_window
        for entry in answer.entries:
            user_id = _user_id(entry.get("userId"))
            if user_id is None or user_id not in allowed_user_ids:
                continue
            for ip_entry in entry.get("ips") or []:
                ip = ip_entry.get("ip") or ""
                seen = parse_remnawave_time(ip_entry.get("lastSeen"))
                if not ip or len(ip) > MAX_IP_LEN or seen is None:
                    continue
                history_key = (user_id, ip, answer.node_uuid)
                known = history.get(history_key)
                if known is None or seen > known:
                    history[history_key] = seen
                if seen < fresh_after:
                    continue
                current = snapshot.get((user_id, ip))
                if current is None:
                    snapshot[(user_id, ip)] = SnapshotEntry(seen, {answer.node_uuid})
                    continue
                current.node_uuids.add(answer.node_uuid)
                if seen > current.last_seen:
                    current.last_seen = seen
    return snapshot, history


def place_counts(rows: Iterable[tuple[str, int, GeoRecord]]) -> dict[tuple[str, str], PlaceCount]:
    coords_by_place: dict[tuple[str, str], dict[str, tuple[float, float]]] = {}
    users_by_place: dict[tuple[str, str], set[int]] = {}
    for ip, user_id, geo in rows:
        if geo.lat is None or geo.lon is None or not geo.country_code:
            continue
        key = (geo.country_code, geo.place)
        coords_by_place.setdefault(key, {})[ip] = (geo.lat, geo.lon)
        users_by_place.setdefault(key, set()).add(user_id)

    places: dict[tuple[str, str], PlaceCount] = {}
    for key, coords in coords_by_place.items():
        count = len(coords)
        places[key] = PlaceCount(
            lat=sum(c[0] for c in coords.values()) / count,
            lon=sum(c[1] for c in coords.values()) / count,
            ips=count,
            users=len(users_by_place[key]),
        )
    return places


def plan_geo_resolve(
    priority: Iterable[str],
    others: Iterable[str],
    fresh: set[str],
    relays: dict[str, str],
    limit: int,
) -> list[str]:
    """Какие IP резолвить в этом цикле: сначала онлайн (по ним строится карта),
    потом остальные — пока не наберётся лимит."""
    planned: list[str] = []
    seen: set[str] = set()
    for ip in chain(priority, others):
        if ip in fresh or ip in relays or ip in seen:
            continue
        seen.add(ip)
        planned.append(ip)
        if len(planned) >= limit:
            break
    return planned


_fleet_cache: Optional[tuple[float, dict[str, str]]] = None
_cycle_version = 0


async def fleet_ip_names() -> dict[str, str]:
    """IP серверов парка → имя сервера: транзитный релей светится у Remnawave
    как source_ip клиента, и это не место и не провайдер самого клиента."""
    global _fleet_cache
    now = time.monotonic()
    if _fleet_cache and now - _fleet_cache[0] < FLEET_IPS_TTL_SEC:
        return _fleet_cache[1]
    async with async_session() as db:
        servers = (await db.execute(select(Server.name, Server.url))).all()
    names: dict[str, str] = {}
    for name, url in servers:
        ip = await host_to_ip(urlparse(url).hostname or "")
        if ip:
            names[ip] = name
    _fleet_cache = (now, names)
    return names


# Новые строки — вставкой без конфликта, а last_seen — отдельным UPDATE только там,
# где он сдвинулся больше чем на час. В одном upsert с WHERE PostgreSQL блокировал бы
# каждую конфликтующую строку (запись в заголовок кортежа + WAL) у всех онлайн-клиентов
# каждый цикл, даже когда менять нечего.
_HISTORY_INSERT = text("""
    INSERT INTO remnawave_ip_history (user_id, source_ip, node_uuid, first_seen, last_seen)
    SELECT v.user_id, v.source_ip, v.node_uuid, v.seen, v.seen
    FROM unnest(CAST(:uids AS integer[]), CAST(:ips AS varchar[]), CAST(:nodes AS varchar[]),
                CAST(:seen AS timestamptz[])) AS v(user_id, source_ip, node_uuid, seen)
    ON CONFLICT (user_id, source_ip, node_uuid) DO NOTHING
""")
_HISTORY_TOUCH = text("""
    UPDATE remnawave_ip_history h SET last_seen = v.seen
    FROM unnest(CAST(:uids AS integer[]), CAST(:ips AS varchar[]), CAST(:nodes AS varchar[]),
                CAST(:seen AS timestamptz[])) AS v(user_id, source_ip, node_uuid, seen)
    WHERE h.user_id = v.user_id AND h.source_ip = v.source_ip AND h.node_uuid = v.node_uuid
      AND h.last_seen < v.seen - CAST(:touch AS interval)
""")


async def _save_history(history: dict[HistoryKey, datetime]) -> None:
    items = list(history.items())
    if not items:
        return
    async with async_session() as db:
        for i in range(0, len(items), HISTORY_CHUNK):
            chunk = items[i:i + HISTORY_CHUNK]
            params = {
                "uids": [uid for (uid, _, _), _ in chunk],
                "ips": [ip for (_, ip, _), _ in chunk],
                "nodes": [node for (_, _, node), _ in chunk],
                "seen": [seen for _, seen in chunk],
            }
            await db.execute(_HISTORY_INSERT, params)
            await db.execute(_HISTORY_TOUCH, {**params, "touch": HISTORY_TOUCH_INTERVAL})
        await db.commit()


async def _save_node_cache(nodes: list[dict], now: datetime) -> None:
    geoip = get_geoip()
    values = []
    for node in nodes:
        uuid = node.get("uuid")
        if not uuid:
            continue
        address = (node.get("address") or "")[:255]
        ip = await host_to_ip(address) if address else None
        geo = geoip.lookup(ip) if ip else None
        values.append({
            "uuid": uuid,
            "name": (node.get("name") or "")[:200] or None,
            "country_code": (node.get("countryCode") or "")[:8] or None,
            "address": address or None,
            "lat": geo.lat if geo else None,
            "lon": geo.lon if geo else None,
            "updated_at": now,
        })
    if not values:
        return
    async with async_session() as db:
        for i in range(0, len(values), UPSERT_BATCH):
            stmt = pg_insert(RemnawaveNodeCache).values(values[i:i + UPSERT_BATCH])
            stmt = stmt.on_conflict_do_update(
                index_elements=["uuid"],
                set_={col: stmt.excluded[col]
                      for col in ("name", "country_code", "address", "lat", "lon", "updated_at")},
            )
            await db.execute(stmt)
        await db.commit()


async def _load_geo(ips: list[str], build: Optional[datetime]) -> tuple[dict[str, GeoRecord], set[str]]:
    """Известное гео IP и те из них, что уже резолвлены текущей сборкой базы.
    Устаревшие записи остаются в ходу, пока их не перерезолвят: прошлый месяц
    лучше пустого места на карте."""
    known: dict[str, GeoRecord] = {}
    fresh: set[str] = set()
    is_fresh = (IpGeo.resolved_at >= build) if build else true()
    async with async_session() as db:
        for i in range(0, len(ips), GEO_FETCH_CHUNK):
            rows = await db.execute(
                select(IpGeo.ip, IpGeo.country_code, IpGeo.region, IpGeo.place, IpGeo.lat, IpGeo.lon,
                       IpGeo.asn, IpGeo.as_org, is_fresh.label("fresh"))
                .where(IpGeo.ip.in_(ips[i:i + GEO_FETCH_CHUNK]))
            )
            for ip, cc, region, place, lat, lon, asn, as_org, row_fresh in rows.all():
                known[ip] = GeoRecord(cc, region, place or "", lat, lon, asn, as_org)
                if row_fresh:
                    fresh.add(ip)
    return known, fresh


async def _history_backfill(build: Optional[datetime], limit: int) -> list[str]:
    """IP истории без гео или с гео прошлой сборки: Remnawave про них уже не
    сообщает, и без этого они остались бы без города навсегда."""
    stale = "g.ip IS NULL OR g.resolved_at < :build" if build else "g.ip IS NULL"
    query = text(
        "SELECT DISTINCT h.source_ip FROM remnawave_ip_history h"
        " LEFT JOIN ip_geo g ON g.ip = h.source_ip"
        f" WHERE {stale} LIMIT :limit"
    )
    params = {"limit": limit, **({"build": build} if build else {})}
    async with async_session() as db:
        return list((await db.execute(query, params)).scalars())


async def resolve_geo(
    online_ips: list[str],
    other_ips: list[str],
    relays: dict[str, str],
    now: datetime,
) -> dict[str, GeoRecord]:
    geoip = get_geoip()
    candidates = [ip for ip in dict.fromkeys(chain(online_ips, other_ips)) if ip not in relays]
    known, fresh = await _load_geo(candidates, geoip.build)
    if not geoip.ready:
        return known

    planned = plan_geo_resolve(online_ips, other_ips, fresh, relays, GEO_RESOLVE_LIMIT)
    if len(planned) < GEO_RESOLVE_LIMIT:
        backfill = await _history_backfill(geoip.build, GEO_RESOLVE_LIMIT - len(planned))
        planned += plan_geo_resolve([], backfill, fresh | set(planned), relays, GEO_RESOLVE_LIMIT - len(planned))
    if not planned:
        return known

    resolved = await asyncio.get_running_loop().run_in_executor(None, geoip.lookup_many, planned)
    if resolved:
        async with async_session() as db:
            items = list(resolved.items())
            for i in range(0, len(items), UPSERT_BATCH):
                values = [
                    {"ip": ip, "country_code": g.country_code, "region": g.region, "place": g.place,
                     "lat": g.lat, "lon": g.lon, "asn": g.asn, "as_org": g.as_org, "resolved_at": now}
                    for ip, g in items[i:i + UPSERT_BATCH]
                ]
                stmt = pg_insert(IpGeo).values(values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["ip"],
                    set_={col: stmt.excluded[col] for col in
                          ("country_code", "region", "place", "lat", "lon", "asn", "as_org", "resolved_at")},
                )
                await db.execute(stmt)
            await db.commit()
    known.update(resolved)
    return known


async def _save_presence(places: dict[tuple[str, str], PlaceCount], total_ips: int, now: datetime) -> None:
    bucket = now.replace(minute=0, second=0, microsecond=0)
    values = [
        {"bucket": bucket, "country_code": cc, "place": place, "lat": p.lat, "lon": p.lon,
         "ips_sum": p.ips, "samples": 1}
        for (cc, place), p in places.items()
    ]
    values.append({
        "bucket": bucket, "country_code": BUCKET_TOTAL_KEY[0], "place": BUCKET_TOTAL_KEY[1],
        "lat": 0.0, "lon": 0.0, "ips_sum": total_ips, "samples": 1,
    })
    async with async_session() as db:
        for i in range(0, len(values), UPSERT_BATCH):
            stmt = pg_insert(RemnawaveGeoPresence).values(values[i:i + UPSERT_BATCH])
            stmt = stmt.on_conflict_do_update(
                index_elements=["bucket", "country_code", "place"],
                set_={
                    "lat": stmt.excluded.lat,
                    "lon": stmt.excluded.lon,
                    "ips_sum": RemnawaveGeoPresence.ips_sum + stmt.excluded.ips_sum,
                    "samples": RemnawaveGeoPresence.samples + 1,
                },
            )
            await db.execute(stmt)
        await db.commit()


async def ingest_cycle(
    snapshot: dict[ParsedKey, SnapshotEntry],
    history: dict[HistoryKey, datetime],
    nodes: list[dict],
    now: datetime,
) -> None:
    await _save_history(history)
    await _save_node_cache(nodes, now)

    relays = await fleet_ip_names()
    online_ips = list(dict.fromkeys(ip for _, ip in snapshot))
    other_ips = list(dict.fromkeys(ip for _, ip, _ in history))
    geo = await resolve_geo(online_ips, other_ips, relays, now)

    located = [(ip, uid, geo[ip]) for uid, ip in snapshot if ip in geo]
    await _save_presence(place_counts(located), len(online_ips), now)


def mark_cycle() -> None:
    """Снимок обновился: читатели карты и таблицы сбрасывают кэши. Зовётся и
    при сбое записи карты — иначе карта застыла бы, пока снимок идёт дальше."""
    global _cycle_version
    _cycle_version += 1


def cycle_version() -> int:
    return _cycle_version


async def cleanup_geo(now: datetime, history_days: int) -> None:
    async with async_session() as db:
        await db.execute(delete(RemnawaveIpHistory).where(
            RemnawaveIpHistory.last_seen < now - timedelta(days=history_days)
        ))
        await db.execute(delete(RemnawaveGeoPresence).where(RemnawaveGeoPresence.bucket < now - PRESENCE_KEEP))
        await db.execute(text(
            "DELETE FROM ip_geo g"
            " WHERE NOT EXISTS (SELECT 1 FROM xray_stats s WHERE s.source_ip = g.ip)"
            " AND NOT EXISTS (SELECT 1 FROM remnawave_ip_history h WHERE h.source_ip = g.ip)"
        ))
        await db.commit()
