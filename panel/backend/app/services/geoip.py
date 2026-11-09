"""Локальная база геолокации IP (DB-IP Lite: город + ASN).

Файлы качаются раз в месяц в /app/data/geoip и читаются через mmap: лукап —
микросекунды без сетевых запросов, поэтому коллектор может резолвить десятки
тысяч клиентских IP за цикл. Лицензия CC BY 4.0 требует подписи на карте.
"""

import asyncio
import gzip
import ipaddress
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal, Optional

import httpx
import maxminddb

logger = logging.getLogger(__name__)

GEOIP_DIR = Path("/app/data/geoip")
DBIP_URL = "https://download.db-ip.com/free/dbip-{kind}-lite-{year:04d}-{month:02d}.mmdb.gz"
DB_FILES = {"city": "dbip-city-lite.mmdb", "asn": "dbip-asn-lite.mmdb"}
MONTH_FILE = "dbip.month"

CHECK_INTERVAL_SEC = 6 * 3600
RETRY_INTERVAL_SEC = 3600
DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
DOWNLOAD_CHUNK = 1 << 20

PLACE_MAX_LEN = 120
AS_ORG_MAX_LEN = 200

# DB-IP делит крупные города на округа: «Moscow (Tsentralnyy administrativnyy okrug)».
# Для карты это один город, иначе Москва рассыпалась бы на десяток точек.
_DISTRICT_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")

DbKind = Literal["city", "asn"]


class GeoIpDownloadError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class GeoRecord:
    country_code: Optional[str]
    region: Optional[str]
    place: str
    lat: Optional[float]
    lon: Optional[float]
    asn: Optional[int]
    as_org: Optional[str]


def _english_name(node) -> Optional[str]:
    if not isinstance(node, dict):
        return None
    names = node.get("names")
    if not isinstance(names, dict):
        return None
    name = names.get("en")
    if not isinstance(name, str):
        return None
    return name.strip()[:PLACE_MAX_LEN] or None


def normalize_place(city: Optional[str], region: Optional[str]) -> str:
    if city:
        stripped = _DISTRICT_SUFFIX.sub("", city).strip()
        if stripped:
            return stripped
    return region or ""


def parse_record(city_rec: Optional[dict], asn_rec: Optional[dict]) -> Optional[GeoRecord]:
    city_rec = city_rec or {}
    asn_rec = asn_rec or {}
    if not city_rec and not asn_rec:
        return None

    subdivisions = city_rec.get("subdivisions") or []
    region = _english_name(subdivisions[0]) if subdivisions else None
    city = _english_name(city_rec.get("city"))

    location = city_rec.get("location") or {}
    lat, lon = location.get("latitude"), location.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        lat = lon = None

    country_code = (city_rec.get("country") or {}).get("iso_code") or None
    asn = asn_rec.get("autonomous_system_number")
    as_org = asn_rec.get("autonomous_system_organization")

    return GeoRecord(
        country_code=country_code,
        region=region,
        place=normalize_place(city, region),
        lat=float(lat) if lat is not None else None,
        lon=float(lon) if lon is not None else None,
        asn=asn if isinstance(asn, int) else None,
        as_org=as_org[:AS_ORG_MAX_LEN] if isinstance(as_org, str) and as_org else None,
    )


def dbip_url(kind: DbKind, year: int, month: int) -> str:
    return DBIP_URL.format(kind=kind, year=year, month=month)


def candidate_months(now: datetime) -> list[tuple[int, int]]:
    previous = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return [(now.year, now.month), previous]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class GeoIpDatabase:
    def __init__(self):
        self._city = None
        self._asn = None
        self.build: Optional[datetime] = None

    @classmethod
    def from_readers(cls, city, asn, build: Optional[datetime] = None) -> "GeoIpDatabase":
        db = cls()
        db._city, db._asn, db.build = city, asn, build
        return db

    @property
    def ready(self) -> bool:
        return self._city is not None

    def open(self, directory: Path) -> None:
        city_path = directory / DB_FILES["city"]
        if not city_path.exists():
            return
        asn_path = directory / DB_FILES["asn"]
        # MODE_AUTO берёт C-расширение: чистый Python держал бы GIL на десятках тысяч лукапов за цикл
        city = maxminddb.open_database(str(city_path), maxminddb.MODE_AUTO)
        asn = maxminddb.open_database(str(asn_path), maxminddb.MODE_AUTO) if asn_path.exists() else None
        # Старые читатели не закрываем: лукапы идут в потоках executor'а, и закрытый
        # посреди чтения mmap уронил бы их. Сборщик мусора освободит их сам.
        self._city, self._asn = city, asn
        self.build = datetime.fromtimestamp(city.metadata().build_epoch, timezone.utc).replace(tzinfo=None)

    def lookup(self, ip: str) -> Optional[GeoRecord]:
        city_reader, asn_reader = self._city, self._asn
        if city_reader is None:
            return None
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return None
        try:
            city_rec = city_reader.get(ip)
            asn_rec = asn_reader.get(ip) if asn_reader is not None else None
        except ValueError:
            return None
        return parse_record(city_rec, asn_rec)

    def lookup_many(self, ips: Iterable[str]) -> dict[str, GeoRecord]:
        found: dict[str, GeoRecord] = {}
        for ip in ips:
            geo = self.lookup(ip)
            if geo is not None:
                found[ip] = geo
        return found


def _gunzip_and_verify(src_gz: Path, dst: Path) -> None:
    with gzip.open(src_gz, "rb") as fin, open(dst, "wb") as fout:
        shutil.copyfileobj(fin, fout, DOWNLOAD_CHUNK)
    src_gz.unlink(missing_ok=True)
    reader = maxminddb.open_database(str(dst), maxminddb.MODE_FILE)
    try:
        reader.metadata()
    finally:
        reader.close()


class GeoIpUpdater:
    def __init__(self, db: GeoIpDatabase, directory: Path = GEOIP_DIR):
        self._db = db
        self._dir = directory
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._force = False
        self.updating = False
        self.error: Optional[str] = None
        self.checked_at: Optional[datetime] = None

    async def start(self) -> None:
        if self._task:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.get_running_loop().run_in_executor(None, self._db.open, self._dir)
        except (OSError, maxminddb.InvalidDatabaseError) as e:
            logger.warning("geoip_open_failed dir=%s error=%s", self._dir, e)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def request_update(self) -> bool:
        if self.updating:
            return False
        self._force = True
        self._wake.set()
        return True

    def status(self) -> dict:
        return {
            "ready": self._db.ready,
            "month": self._installed_month_label(),
            "build": self._db.build.isoformat() if self._db.build else None,
            "updating": self.updating,
            "error": self.error,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
        }

    async def _loop(self) -> None:
        while True:
            delay = CHECK_INTERVAL_SEC
            self.updating = True
            try:
                await self._update(force=self._force)
                self.error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # фоновая задача: любая ошибка — повтор через час, не смерть цикла
                self.error = str(e) or type(e).__name__
                delay = RETRY_INTERVAL_SEC
                logger.warning("geoip_update_failed error=%s", self.error)
            finally:
                self.updating = False
                self._force = False
                self.checked_at = _utcnow()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _installed_month(self) -> Optional[tuple[int, int]]:
        if not self._db.ready:
            return None
        try:
            year, month = (self._dir / MONTH_FILE).read_text().strip().split("-")
            return int(year), int(month)
        except (OSError, ValueError):
            return (self._db.build.year, self._db.build.month) if self._db.build else None

    def _installed_month_label(self) -> Optional[str]:
        installed = self._installed_month()
        return f"{installed[0]:04d}-{installed[1]:02d}" if installed else None

    async def _update(self, force: bool) -> None:
        installed = None if force else self._installed_month()
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            for year, month in candidate_months(_utcnow()):
                if installed and (year, month) <= installed:
                    return
                city = await self._download(client, "city", year, month)
                if city is None:
                    continue
                asn = await self._download(client, "asn", year, month)
                os.replace(city, self._dir / DB_FILES["city"])
                if asn is not None:
                    os.replace(asn, self._dir / DB_FILES["asn"])
                (self._dir / MONTH_FILE).write_text(f"{year:04d}-{month:02d}")
                await asyncio.get_running_loop().run_in_executor(None, self._db.open, self._dir)
                logger.info("geoip_updated month=%04d-%02d", year, month)
                return
        if not self._db.ready:
            raise GeoIpDownloadError("DB-IP has no database for the current or previous month")

    async def _download(self, client: httpx.AsyncClient, kind: DbKind, year: int, month: int) -> Optional[Path]:
        url = dbip_url(kind, year, month)
        part_gz = self._dir / f"{DB_FILES[kind]}.gz.part"
        part = self._dir / f"{DB_FILES[kind]}.part"
        async with client.stream("GET", url) as resp:
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                raise GeoIpDownloadError(f"{url}: HTTP {resp.status_code}")
            with open(part_gz, "wb") as fh:
                async for chunk in resp.aiter_bytes(DOWNLOAD_CHUNK):
                    fh.write(chunk)
        await asyncio.get_running_loop().run_in_executor(None, _gunzip_and_verify, part_gz, part)
        return part


_db = GeoIpDatabase()
_updater = GeoIpUpdater(_db)


def get_geoip() -> GeoIpDatabase:
    return _db


def geoip_status() -> dict:
    return _updater.status()


def request_geoip_update() -> bool:
    return _updater.request_update()


async def start_geoip_updater() -> None:
    await _updater.start()


async def stop_geoip_updater() -> None:
    await _updater.stop()
