"""Geo resolver — determines server region by IP for closest iperf3 server selection.

Uses ip-api.com (free, 45 req/min, no key required).
Caches results in Server.country / Server.geo_region fields.
"""

import logging
import re
from typing import Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import update
from app.services.http_client import get_external_client

from app.database import async_session
from app.models import Server

logger = logging.getLogger(__name__)

REGION_MAP = {
    "RU": "RU",
    "UA": "EU", "BY": "EU", "MD": "EU",
    "DE": "EU", "FR": "EU", "NL": "EU", "GB": "EU", "IT": "EU", "ES": "EU",
    "PL": "EU", "SE": "EU", "NO": "EU", "FI": "EU", "DK": "EU", "AT": "EU",
    "CH": "EU", "BE": "EU", "CZ": "EU", "RO": "EU", "BG": "EU", "HR": "EU",
    "PT": "EU", "IE": "EU", "LT": "EU", "LV": "EU", "EE": "EU", "SK": "EU",
    "HU": "EU", "SI": "EU", "LU": "EU", "RS": "EU", "AL": "EU", "MK": "EU",
    "BA": "EU", "ME": "EU", "IS": "EU", "MT": "EU", "CY": "EU",
    "TR": "ME", "IL": "ME", "AE": "ME", "SA": "ME", "QA": "ME",
    "KW": "ME", "BH": "ME", "OM": "ME", "JO": "ME", "LB": "ME",
    "US": "US", "CA": "US", "MX": "US",
    "BR": "SA", "AR": "SA", "CL": "SA", "CO": "SA", "PE": "SA",
    "JP": "ASIA", "KR": "ASIA", "CN": "ASIA", "TW": "ASIA", "HK": "ASIA",
    "SG": "ASIA", "TH": "ASIA", "VN": "ASIA", "MY": "ASIA", "ID": "ASIA",
    "PH": "ASIA", "IN": "ASIA", "BD": "ASIA", "PK": "ASIA",
    "KZ": "ASIA", "UZ": "ASIA", "KG": "ASIA", "TJ": "ASIA", "TM": "ASIA",
    "AU": "AU", "NZ": "AU",
    "ZA": "AF", "NG": "AF", "EG": "AF", "KE": "AF",
}

IPERF_REGION_TO_GEO = {
    "EU-FR": "EU", "EU-NL": "EU", "EU-DE": "EU", "EU-GB": "EU",
    "RU-MOW": "RU", "RU-SPB": "RU", "RU-SVE": "RU",
    "Asia-UZ": "ASIA", "Asia-SG": "ASIA", "Asia-JP": "ASIA", "Asia-HK": "ASIA",
    "US-NY": "US", "US-LA": "US", "US-MIA": "US",
    "SA-BR": "SA",
    "panel": "panel",
}

GEO_NEIGHBORS = {
    "RU": ["RU", "EU", "ASIA"],
    "EU": ["EU", "RU", "ME"],
    "US": ["US", "SA"],
    "ASIA": ["ASIA", "RU", "AU"],
    "ME": ["ME", "EU", "ASIA"],
    "SA": ["SA", "US"],
    "AU": ["AU", "ASIA"],
    "AF": ["AF", "EU", "ME"],
}


def _extract_ip_from_url(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
            return host
        return host if host else None
    except Exception:
        return None


async def _lookup_ip_geo(ip_or_host: str) -> Optional[dict]:
    try:
        client = get_external_client()
        resp = await client.get(
            f"http://ip-api.com/json/{ip_or_host}",
            params={"fields": "status,countryCode,regionName,city,lat,lon"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                return data
    except Exception as e:
        logger.debug(f"Geo lookup failed for {ip_or_host}: {e}")
    return None


async def resolve_server_geo(server: Server) -> Optional[str]:
    """Resolve geo_region for a server by its URL IP. Saves to DB. Returns geo_region."""
    if server.geo_region:
        return server.geo_region

    host = _extract_ip_from_url(server.url)
    if not host:
        return None

    geo_data = await _lookup_ip_geo(host)
    if not geo_data:
        return None

    country_code = geo_data.get("countryCode", "")
    geo_region = REGION_MAP.get(country_code)
    if not geo_region:
        geo_region = "OTHER"

    try:
        async with async_session() as db:
            await db.execute(
                update(Server).where(Server.id == server.id).values(
                    country=country_code,
                    geo_region=geo_region,
                )
            )
            await db.commit()
        server.country = country_code
        server.geo_region = geo_region
        logger.info(f"Geo resolved: {server.name} -> {country_code} ({geo_region})")
    except Exception as e:
        logger.debug(f"Failed to save geo for {server.name}: {e}")

    return geo_region


def filter_servers_by_geo(iperf_servers: list[dict], node_geo_region: str) -> list[dict]:
    """Filter iperf3 servers to those closest to the node's geo region.

    Always includes 'panel' servers. Returns 2-4 best servers.
    """
    if not node_geo_region or node_geo_region == "OTHER":
        return iperf_servers

    neighbors = GEO_NEIGHBORS.get(node_geo_region, [node_geo_region])

    panel_servers = []
    scored: list[tuple[int, dict]] = []

    for srv in iperf_servers:
        region_tag = srv.get("region", "")

        if region_tag == "panel":
            panel_servers.append(srv)
            continue

        srv_geo = IPERF_REGION_TO_GEO.get(region_tag)
        if not srv_geo:
            for prefix, geo in IPERF_REGION_TO_GEO.items():
                if region_tag.startswith(prefix.split("-")[0]):
                    srv_geo = geo
                    break

        if not srv_geo:
            scored.append((99, srv))
            continue

        if srv_geo in neighbors:
            priority = neighbors.index(srv_geo)
            scored.append((priority, srv))
        else:
            scored.append((50, srv))

    scored.sort(key=lambda x: x[0])

    result = panel_servers + [s[1] for s in scored[:3]]
    return result
