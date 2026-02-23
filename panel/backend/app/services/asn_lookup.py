"""
ASN Lookup Service with DB caching and CIDR matching.

Resolves IP addresses to ASN/prefix via RIPE Stat API.
Results are cached in PostgreSQL for 7 days + in-memory for fast repeated access.
Known prefixes are used for in-memory CIDR matching to minimize API calls.
"""

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from sqlalchemy import select, delete

from app.database import async_session
from app.models import ASNCache

logger = logging.getLogger(__name__)

RIPE_API_URL = "https://stat.ripe.net/data/network-info/data.json"
CACHE_TTL_DAYS = 7
BATCH_SIZE = 10
BATCH_DELAY_SEC = 0.3
REQUEST_TIMEOUT_SEC = 8

@dataclass
class ASNInfo:
    asn: Optional[str]
    prefix: Optional[str]


_memory_cache: dict[str, tuple[ASNInfo, float]] = {}
_MEMORY_TTL = 3600  # 1 hour


async def _fetch_asn_from_ripe(session: aiohttp.ClientSession, ip: str) -> ASNInfo:
    """Query RIPE Stat API for a single IP."""
    try:
        async with session.get(
            RIPE_API_URL,
            params={"resource": ip},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC)
        ) as resp:
            if resp.status != 200:
                logger.debug(f"RIPE API returned {resp.status} for {ip}")
                return ASNInfo(asn=None, prefix=None)

            data = await resp.json()
            inner = data.get("data", {})
            asns = inner.get("asns", [])
            prefix = inner.get("prefix")
            asn = asns[0] if asns else None
            return ASNInfo(asn=asn, prefix=prefix)
    except Exception as e:
        logger.debug(f"RIPE API error for {ip}: {e}")
        return ASNInfo(asn=None, prefix=None)


def _ip_in_prefix(ip_str: str, prefix_str: str) -> bool:
    """Check if IP belongs to a CIDR prefix."""
    try:
        return ipaddress.ip_address(ip_str) in ipaddress.ip_network(prefix_str, strict=False)
    except ValueError:
        return False


async def lookup_ips(ip_list: list[str]) -> dict[str, ASNInfo]:
    """Resolve a list of IPs to ASN info using DB cache + RIPE API.

    Returns dict mapping each IP to its ASNInfo.
    """
    if not ip_list:
        return {}

    unique_ips = list(set(ip_list))
    result: dict[str, ASNInfo] = {}
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=CACHE_TTL_DAYS)

    # 1. Check DB cache
    async with async_session() as db:
        # Cleanup expired entries
        await db.execute(delete(ASNCache).where(ASNCache.cached_at < cutoff))
        await db.commit()

        cached = (await db.execute(
            select(ASNCache).where(ASNCache.ip.in_(unique_ips))
        )).scalars().all()

    known_prefixes: dict[str, ASNInfo] = {}  # prefix -> ASNInfo
    for row in cached:
        result[row.ip] = ASNInfo(asn=row.asn, prefix=row.prefix)
        if row.prefix:
            known_prefixes[row.prefix] = ASNInfo(asn=row.asn, prefix=row.prefix)

    uncached = [ip for ip in unique_ips if ip not in result]
    if not uncached:
        return result

    # 2. Try CIDR match against known prefixes
    still_unknown: list[str] = []
    cidr_matched: list[tuple[str, ASNInfo]] = []

    for ip in uncached:
        matched = False
        for prefix_str, info in known_prefixes.items():
            if _ip_in_prefix(ip, prefix_str):
                result[ip] = info
                cidr_matched.append((ip, info))
                matched = True
                break
        if not matched:
            still_unknown.append(ip)

    # Save CIDR-matched IPs to cache
    if cidr_matched:
        async with async_session() as db:
            for ip, info in cidr_matched:
                db.add(ASNCache(
                    ip=ip, asn=info.asn, prefix=info.prefix,
                    cached_at=datetime.now(timezone.utc).replace(tzinfo=None)
                ))
            try:
                await db.commit()
            except Exception:
                await db.rollback()

    if not still_unknown:
        return result

    # 3. Fetch from RIPE API in batches
    fetched: list[tuple[str, ASNInfo]] = []

    async with aiohttp.ClientSession() as session:
        for i in range(0, len(still_unknown), BATCH_SIZE):
            batch = still_unknown[i:i + BATCH_SIZE]
            tasks = [_fetch_asn_from_ripe(session, ip) for ip in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for ip, res in zip(batch, batch_results):
                if isinstance(res, Exception):
                    info = ASNInfo(asn=None, prefix=None)
                else:
                    info = res

                result[ip] = info
                fetched.append((ip, info))

                # Add newly discovered prefix for subsequent CIDR matching
                if info.prefix:
                    known_prefixes[info.prefix] = info

            # Rate limit between batches
            if i + BATCH_SIZE < len(still_unknown):
                await asyncio.sleep(BATCH_DELAY_SEC)

    # After API fetches, check remaining unknown IPs against new prefixes
    # (some IPs fetched later in batches may share prefixes with earlier ones)
    # This is already handled because we add to known_prefixes during fetch

    # 4. Save fetched results to DB cache
    if fetched:
        async with async_session() as db:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            for ip, info in fetched:
                db.add(ASNCache(ip=ip, asn=info.asn, prefix=info.prefix, cached_at=now))
            try:
                await db.commit()
            except Exception:
                await db.rollback()

    logger.debug(
        f"ASN lookup: {len(unique_ips)} IPs total, "
        f"{len(cached)} cached, {len(cidr_matched)} CIDR-matched, "
        f"{len(fetched)} fetched from API"
    )

    return result


async def lookup_ips_cached(ip_list: list[str]) -> dict[str, ASNInfo]:
    """Fast ASN lookup with in-memory cache layer on top of DB + API.
    
    Memory cache avoids repeated DB queries for the same IPs within 1 hour.
    Only IPs missing from memory cache go through the full lookup_ips path.
    """
    if not ip_list:
        return {}
    
    now = time.time()
    result: dict[str, ASNInfo] = {}
    miss: list[str] = []
    
    for ip in set(ip_list):
        entry = _memory_cache.get(ip)
        if entry and (now - entry[1]) < _MEMORY_TTL:
            result[ip] = entry[0]
        else:
            miss.append(ip)
    
    if miss:
        fetched = await lookup_ips(miss)
        for ip, info in fetched.items():
            _memory_cache[ip] = (info, now)
            result[ip] = info
    
    return result


def group_ips_by_asn(asn_map: dict[str, ASNInfo]) -> list[dict]:
    """Group IPs by ASN and return structured list (without visit filtering).

    Returns list of ASN groups:
    [
        {"asn": "8359", "prefix": "91.76.0.0/14", "ips": [...], "count": 45, "visits": 0},
        {"asn": null, "ips": ["1.2.3.4"], "count": 1, "visits": 0},
    ]
    """
    return group_ips_by_asn_with_visits(asn_map, {}, min_visits=0)


def group_ips_by_asn_with_visits(
    asn_map: dict[str, ASNInfo],
    ip_visits: dict[str, int],
    min_visits: int = 0
) -> list[dict]:
    """Group IPs by ASN, aggregate visit counts, and filter by min_visits.

    Args:
        asn_map: IP -> ASNInfo mapping
        ip_visits: IP -> visit count mapping
        min_visits: minimum total visits for an ASN group to be included.
            For ASN groups: sum of visits across all IPs in the ASN.
            For IPs without ASN: individual visit count.

    Returns list of active ASN groups:
    [
        {"asn": "8359", "prefix": "91.76.0.0/14", "ips": [...], "count": 45, "visits": 12500},
        {"asn": null, "ips": ["1.2.3.4"], "count": 1, "visits": 1500},
    ]
    """
    groups: dict[Optional[str], dict] = {}

    for ip, info in asn_map.items():
        key = info.asn
        if key not in groups:
            groups[key] = {
                "asn": info.asn,
                "prefix": info.prefix,
                "ips": [],
                "visits": 0,
            }
        groups[key]["ips"].append(ip)
        groups[key]["visits"] += ip_visits.get(ip, 0)
        if info.prefix and not groups[key]["prefix"]:
            groups[key]["prefix"] = info.prefix

    result = []
    for group in groups.values():
        group["count"] = len(group["ips"])

        if group["asn"] is not None:
            # ASN group: check total visits across all IPs
            if group["visits"] >= min_visits:
                result.append(group)
        else:
            # No ASN: filter each IP individually by its visits
            if min_visits > 0:
                active_ips = [ip for ip in group["ips"] if ip_visits.get(ip, 0) >= min_visits]
                if active_ips:
                    group["ips"] = active_ips
                    group["count"] = len(active_ips)
                    group["visits"] = sum(ip_visits.get(ip, 0) for ip in active_ips)
                    result.append(group)
            else:
                result.append(group)

    # Sort: ASN groups first (by visits desc), then no-ASN IPs
    result.sort(key=lambda g: (g["asn"] is None, -g["visits"]))
    return result


def effective_ip_count(asn_groups: list[dict]) -> int:
    """Count effective IP groups: each ASN = 1, IPs without ASN = 1 each."""
    count = 0
    for group in asn_groups:
        if group["asn"] is not None:
            count += 1  # Whole ASN counts as 1
        else:
            count += group["count"]  # Each IP without ASN counts separately
    return count
