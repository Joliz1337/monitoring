import asyncio
import gzip
import hashlib
import ipaddress
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import Row, select, text, update, bindparam, func
from sqlalchemy.ext.asyncio import AsyncSession
import re
from urllib.parse import urlparse
from pydantic import BaseModel, Field, field_validator
from typing import Optional
import httpx
import json
from app.services.http_client import get_node_client, node_auth_headers, validate_proxy_input
from app.services.node_capabilities import (
    capabilities_from_version,
    parse as parse_capabilities,
    remember_capabilities,
)

from app.database import get_db
from app.models import SERVER_NOTE_MAX_LENGTH, Server, ServerTraffic
from app.auth import verify_auth
from app.services import ping_block
from app.services.blocklist_manager import get_blocklist_manager
from app.services.fleet_history import FleetPeriod, load_fleet_history
from app.services.latest_metrics import get_latest_metrics
from app.services.server_card_metrics import card_metrics
from app.services.server_status import get_offline_threshold, resolve_status
from app.services.time_sync import get_time_sync_service
from app.services.net_utils import resolve_panel_ip
from app.services.pki import build_installer_token
from app.services.migration import (
    classify_server,
    push_shared_cert_to_node,
)


RUSSIAN_MONTHS = {
    "января": 1, "янв": 1,
    "февраля": 2, "фев": 2,
    "марта": 3, "мар": 3,
    "апреля": 4, "апр": 4,
    "мая": 5, "май": 5,
    "июня": 6, "июн": 6,
    "июля": 7, "июл": 7,
    "августа": 8, "авг": 8,
    "сентября": 9, "сен": 9,
    "октября": 10, "окт": 10,
    "ноября": 11, "ноя": 11,
    "декабря": 12, "дек": 12,
}


def parse_flexible_date(raw: str) -> datetime:
    """Парсит дату из разных форматов хостингов.

    Поддерживаемые форматы:
      - ISO: 2026-05-09, 2026-05-09T21:30:00
      - Русские: 05 мая 2026, 05 мая 2026 21:30
      - Короткие: 1/10/26, 01/10/2026, 1.10.26, 01.10.2026
      - Пустая строка / None → очистка поля (ValueError)
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("empty")

    # ISO: 2026-05-09 или 2026-05-09T21:30:00
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?)?$', raw)
    if m:
        return datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0),
            tzinfo=timezone.utc,
        )

    # Русские месяцы: 05 мая 2026 [21:30]
    m = re.match(
        r'^(\d{1,2})\s+([а-яё]+)\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?$',
        raw, re.IGNORECASE,
    )
    if m:
        month = RUSSIAN_MONTHS.get(m.group(2).lower())
        if month:
            return datetime(
                int(m.group(3)), month, int(m.group(1)),
                int(m.group(4) or 0), int(m.group(5) or 0),
                tzinfo=timezone.utc,
            )

    # Короткие: dd/mm/yy, dd.mm.yy, dd/mm/yyyy, dd.mm.yyyy
    m = re.match(r'^(\d{1,2})[./](\d{1,2})[./](\d{2,4})$', raw)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        return datetime(year, month, day, tzinfo=timezone.utc)

    raise ValueError(f"Unrecognized date format: {raw}")


router = APIRouter(prefix="/servers", tags=["servers"])


def to_iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Convert datetime to ISO format with explicit UTC timezone suffix.
    
    All timestamps are stored as naive UTC, so we add 'Z' suffix for frontend.
    Truncates microseconds to milliseconds for better JS compatibility.
    """
    if dt is None:
        return None
    # Truncate to milliseconds (JS ISO format standard)
    dt = dt.replace(microsecond=(dt.microsecond // 1000) * 1000)
    # Format as ISO and append Z (all our times are UTC)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


_LATEST_SPEEDS_SQL = text("""
    SELECT i.server_id,
           s.net_rx_bytes_per_sec, s.net_tx_bytes_per_sec,
           s.disk_read_bytes_per_sec, s.disk_write_bytes_per_sec
    FROM unnest(CAST(:ids AS int[])) AS i(server_id)
    CROSS JOIN LATERAL (
        SELECT m.net_rx_bytes_per_sec, m.net_tx_bytes_per_sec,
               m.disk_read_bytes_per_sec, m.disk_write_bytes_per_sec
        FROM metrics_snapshots m
        WHERE m.server_id = i.server_id
        ORDER BY m.timestamp DESC
        LIMIT 1
    ) s
""")


async def get_latest_snapshots_bulk(server_ids: list[int], db: AsyncSession) -> dict[int, Row]:
    """Скорости из последнего снапшота каждого сервера.

    На сервер — один спуск с конца по индексу (server_id, timestamp). DISTINCT ON
    по списку серверов читал и сортировал таблицу снапшотов целиком.
    """
    if not server_ids:
        return {}

    result = await db.execute(_LATEST_SPEEDS_SQL, {"ids": server_ids})
    return {row.server_id: row for row in result}


# Окно трафика на карточке сервера — то же, что по умолчанию на странице трафика.
TRAFFIC_CARD_DAYS = 30


async def get_traffic_totals_bulk(
    server_ids: list[int],
    db: AsyncSession,
    days: int = TRAFFIC_CARD_DAYS,
) -> dict[int, dict]:
    """Суммарный трафик за окно по всем серверам одним GROUP BY.

    Суточные бакеты вместо часовых: на 30 днях это ~30 строк на сервер против ~720,
    а карточке сервера точность до часа не нужна.
    """
    if not server_ids:
        return {}

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    result = await db.execute(
        select(
            ServerTraffic.server_id,
            func.sum(ServerTraffic.rx_bytes),
            func.sum(ServerTraffic.tx_bytes),
        )
        .where(ServerTraffic.server_id.in_(server_ids))
        .where(ServerTraffic.period_type == "day")
        .where(ServerTraffic.scope == "total")
        .where(ServerTraffic.bucket >= since)
        .group_by(ServerTraffic.server_id)
    )
    return {
        server_id: {"rx_bytes": int(rx or 0), "tx_bytes": int(tx or 0), "days": days}
        for server_id, rx, tx in result.all()
    }


def _clean_url(v: str) -> str:
    """Валидация URL ноды: формат + отсечка loopback/link-local/multicast."""
    v = re.sub(r'[^\x20-\x7E]', '', v).strip().rstrip('/')
    if not v:
        raise ValueError('URL is empty after cleanup')
    if not v.startswith(('http://', 'https://')):
        raise ValueError('URL must start with http:// or https://')
    host = (urlparse(v).hostname or "").strip()
    if host:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                raise ValueError(f'Disallowed host: {host}')
    return v


FOLDER_MAX_LENGTH = 200  # длина колонки servers.folder


class ServerCreate(BaseModel):
    name: str
    url: str
    proxy_url: Optional[str] = None
    folder: Optional[str] = Field(None, max_length=FOLDER_MAX_LENGTH)

    @field_validator('url')
    @classmethod
    def validate_url(cls, v: str) -> str:
        return _clean_url(v)

    @field_validator('proxy_url')
    @classmethod
    def validate_proxy(cls, v: str | None) -> str | None:
        return validate_proxy_input(v)


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    api_key: Optional[str] = None
    is_active: Optional[bool] = None
    folder: Optional[str] = None
    proxy_url: Optional[str] = None
    note: Optional[str] = None

    @field_validator('url')
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _clean_url(v)

    @field_validator('proxy_url')
    @classmethod
    def validate_proxy(cls, v: str | None) -> str | None:
        return validate_proxy_input(v)

    @field_validator('note')
    @classmethod
    def validate_note(cls, v: str | None) -> str | None:
        return clean_server_note(v)


def clean_server_note(note: Optional[str]) -> Optional[str]:
    note = (note or "").strip()
    if len(note) > SERVER_NOTE_MAX_LENGTH:
        raise ValueError(f"Note is longer than {SERVER_NOTE_MAX_LENGTH} characters")
    return note or None


def clean_folder_name(folder: Optional[str]) -> Optional[str]:
    return (folder or "").strip() or None


class ServerReorder(BaseModel):
    server_ids: list[int]


class MoveServersToFolder(BaseModel):
    server_ids: list[int]
    folder: Optional[str] = None


class RenameServerFolder(BaseModel):
    old_name: str
    new_name: str


# Кэш тяжёлого ответа /servers?include_metrics=true. Метрики меняются раз в цикл
# коллектора (~10с), поэтому в пределах TTL все вкладки операторов получают готовый
# JSON без повторных запросов к БД и пересборки. ETag даёт браузеру 304 на неизменных
# данных — вместо сотен килобайт на каждый поллинг при тысячах нод.
_LIST_CACHE_TTL = 3.0
# Список сжимается один раз на сборку и вне event loop: сжатие мегабайтного ответа
# на каждый запрос останавливало бы бэкенд вместе со сбором метрик
_LIST_GZIP_LEVEL = 5
_monotonic = time.monotonic


@dataclass(frozen=True)
class CachedList:
    body: bytes
    gzipped: bytes
    etag: str
    built_at: float


_list_cache_lock = asyncio.Lock()
_list_cache: Optional[CachedList] = None
_list_generation = 0


def _invalidate_list_cache() -> None:
    # Мутации раскладки (порядок, папки) должны быть видны сразу: иначе поллинг
    # в пределах TTL возвращает старый JSON и фронт откатывает optimistic-обновление.
    global _list_cache, _list_generation
    _list_cache = None
    _list_generation += 1


def _fresh_list() -> Optional[CachedList]:
    if _list_cache is not None and (_monotonic() - _list_cache.built_at) < _LIST_CACHE_TTL:
        return _list_cache
    return None


def _encode_list(payload: dict) -> tuple[bytes, bytes, str]:
    body = json.dumps(payload, default=str).encode("utf-8")
    etag = '"' + hashlib.md5(body).hexdigest() + '"'
    return body, gzip.compress(body, compresslevel=_LIST_GZIP_LEVEL), etag


async def _get_cached_servers_list(db: AsyncSession) -> CachedList:
    global _list_cache
    cached = _fresh_list()
    if cached is not None:
        return cached

    async with _list_cache_lock:
        # Повторная проверка: пока ждали лок, другой запрос мог пересобрать кэш.
        cached = _fresh_list()
        if cached is not None:
            return cached

        generation = _list_generation
        payload = await _build_servers_list(db, include_metrics=True)
        body, gzipped, etag = await asyncio.to_thread(_encode_list, payload)
        # TTL отсчитывается от конца сборки: на большом парке она длиннее TTL, и кэш,
        # помеченный её началом, рождался бы протухшим — каждый запрос собирал бы заново
        built = CachedList(body=body, gzipped=gzipped, etag=etag, built_at=_monotonic())
        # Список, собранный до мутации раскладки, в кэш не кладём
        if generation == _list_generation:
            _list_cache = built
        return built


def _list_response(
    cached: CachedList, accept_encoding: str, if_none_match: Optional[str]
) -> Response:
    headers = {"ETag": cached.etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
    if if_none_match == cached.etag:
        return Response(status_code=304, headers=headers)
    if "gzip" in accept_encoding.lower():
        return Response(
            content=cached.gzipped,
            media_type="application/json",
            headers={**headers, "Content-Encoding": "gzip"},
        )
    return Response(content=cached.body, media_type="application/json", headers=headers)


@router.get("")
async def list_servers(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
    include_metrics: bool = False
):
    # Лёгкий путь (без метрик) дёшев и не кэшируется; тяжёлый — через кэш + ETag.
    if not include_metrics:
        return await _build_servers_list(db, include_metrics=False)

    cached = await _get_cached_servers_list(db)
    return _list_response(
        cached, request.headers.get("accept-encoding", ""), request.headers.get("if-none-match")
    )


def _ids_needing_snapshot(latest: dict[int, dict]) -> list[int]:
    """Скорости из снапшота нужны только старым агентам: остальные присылают свои."""
    return [server_id for server_id, metrics in latest.items() if not metrics.get("live_rates")]


async def _build_servers_list(db: AsyncSession, include_metrics: bool) -> dict:
    result = await db.execute(select(Server).order_by(Server.position, Server.id))
    servers = result.scalars().all()

    latest: dict[int, dict] = {}
    snapshots_map = {}
    traffic_map: dict[int, dict] = {}
    offline_threshold = await get_offline_threshold(db)
    if include_metrics:
        latest = get_latest_metrics().get_many(s.id for s in servers)
        snapshots_map = await get_latest_snapshots_bulk(_ids_needing_snapshot(latest), db)
        traffic_map = await get_traffic_totals_bulk([s.id for s in servers], db)

    servers_data = []
    for s in servers:
        server_info = {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "proxy_url": s.proxy_url,
            "position": s.position,
            "is_active": s.is_active,
            "folder": s.folder,
            "note": s.note,
            "last_seen": to_iso_utc(s.last_seen),
            "last_error": s.last_error,
            "error_code": s.error_code,
            "pki_enabled": bool(s.pki_enabled),
            "uses_shared_cert": bool(s.uses_shared_cert),
            "auth_kind": classify_server(s),
            "antiddos_emergency_mode": bool(s.antiddos_emergency_mode),
            "has_xray_node": bool(s.has_xray_node),
            # null — нода без ограничений; интерфейс по этой карте прячет
            # разделы, которых у неё всё равно нет
            "node_capabilities": parse_capabilities(s.node_capabilities),
        }

        metrics = latest.get(s.id)
        if include_metrics and metrics:
            server_info["metrics"] = card_metrics(metrics, snapshots_map.get(s.id))
            server_info["status"] = resolve_status(s, offline_threshold)
        elif include_metrics:
            server_info["metrics"] = None
            server_info["status"] = resolve_status(s, offline_threshold) if s.last_seen else "loading"
        else:
            server_info["status"] = resolve_status(s, offline_threshold)
        
        traffic = traffic_map.get(s.id)
        if traffic:
            server_info["traffic"] = traffic

        servers_data.append(server_info)
    
    return {
        "count": len(servers),
        "servers": servers_data
    }


@router.get("/installer-token")
async def get_installer_token(
    request: Request,
    _: dict = Depends(verify_auth),
):
    """Вернуть общий NODE_SECRET для установки на любую ноду.

    Идентичен между вызовами — оператор копирует один раз и переиспользует.
    В payload вшит IP панели — deploy.sh ноды берёт его автоматически для UFW.
    """
    keygen = request.app.state.pki
    return {"token": build_installer_token(keygen, panel_ip=await resolve_panel_ip())}


@router.get("/migration-status")
async def get_migration_status(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Сколько нод ещё не на shared cert — для условного показа баннера."""
    # Проекция трёх колонок вместо полных ORM-объектов (с тяжёлыми TEXT last_metrics
    # и т.п.) — classify_server читает только их; Row даёт доступ по имени.
    result = await db.execute(
        select(Server.uses_shared_cert, Server.pki_enabled, Server.dedicated_cert)
    )
    rows = result.all()
    counters = {"shared": 0, "dedicated": 0, "per_server": 0, "legacy": 0}
    for r in rows:
        counters[classify_server(r)] += 1
    counters["total"] = len(rows)
    # dedicated — осознанный выбор персонального ключа, мигрировать его не надо
    counters["needs_migration"] = counters["per_server"] + counters["legacy"]
    return counters


@router.get("/fleet/history")
async def get_fleet_history(
    period: FleetPeriod = Query(default="24h"),
    folder: Optional[str] = Query(default=None, min_length=1, max_length=FOLDER_MAX_LENGTH),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Сводная история парка или одной папки: CPU, память и скорости сети."""
    return await load_fleet_history(db, period, folder)


@router.post("")
async def create_server(
    server: ServerCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    result = await db.execute(select(Server).order_by(Server.position.desc()))
    last_server = result.scalars().first()
    next_position = (last_server.position + 1) if last_server else 0

    new_server = Server(
        name=server.name,
        url=server.url.rstrip("/"),
        api_key=None,
        proxy_url=server.proxy_url,
        folder=clean_folder_name(server.folder),
        pki_enabled=True,
        uses_shared_cert=True,
        position=next_position,
    )
    db.add(new_server)
    await db.commit()
    await db.refresh(new_server)
    _invalidate_list_cache()

    blocklist = get_blocklist_manager()
    asyncio.ensure_future(blocklist.sync_single_node_by_id(new_server.id))
    blocklist.request_allowlist_push()
    asyncio.ensure_future(
        get_time_sync_service().sync_single_server(new_server.id)
    )

    return {
        "success": True,
        "server": {
            "id": new_server.id,
            "name": new_server.name,
            "url": new_server.url,
            "proxy_url": new_server.proxy_url,
            "position": new_server.position,
            "pki_enabled": True,
            "uses_shared_cert": True,
            "auth_kind": "shared",
        },
    }


@router.post("/move-to-folder")
async def move_servers_to_folder(
    data: MoveServersToFolder,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    folder_value = clean_folder_name(data.folder)
    result = await db.execute(select(Server).where(Server.id.in_(data.server_ids)))
    servers = result.scalars().all()
    for s in servers:
        s.folder = folder_value
    await db.commit()
    _invalidate_list_cache()
    # Папка может быть выбрана для закрытого ping — у перенесённых он меняется
    get_blocklist_manager().request_allowlist_push()
    return {"success": True, "moved": len(servers)}


@router.post("/folders/rename")
async def rename_server_folder(
    data: RenameServerFolder,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    new_name = data.new_name.strip() if data.new_name else None
    if not new_name:
        raise HTTPException(400, "new_name is required")
    result = await db.execute(select(Server).where(Server.folder == data.old_name))
    servers = result.scalars().all()
    for s in servers:
        s.folder = new_name
    await db.commit()
    await ping_block.rename_folder(db, data.old_name, new_name)
    _invalidate_list_cache()
    # При слиянии с уже выбранной папкой ping закрывается и у пришедших серверов
    get_blocklist_manager().request_allowlist_push()
    return {"success": True, "renamed": len(servers)}


@router.delete("/folders/{folder_name}")
async def delete_server_folder(
    folder_name: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    result = await db.execute(select(Server).where(Server.folder == folder_name))
    servers = result.scalars().all()
    for s in servers:
        s.folder = None
    await db.commit()
    await ping_block.forget_folder(db, folder_name)
    _invalidate_list_cache()
    get_blocklist_manager().request_allowlist_push()
    return {"success": True, "unfoldered": len(servers)}


@router.post("/{server_id}/confirm-migration")
async def confirm_migration(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Подтвердить ручную миграцию legacy-ноды после переустановки.

    Проверяем что нода доступна по mTLS — это значит NODE_SECRET установлен.
    """
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404)

    probe = Server(
        id=server.id,
        url=server.url,
        proxy_url=server.proxy_url,
        pki_enabled=True,
        api_key=None,
    )
    client = get_node_client(probe)
    try:
        response = await client.get(
            f"{server.url.rstrip('/')}/api/version",
            headers=node_auth_headers(probe),
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Node not reachable via mTLS: {exc}",
        ) from exc

    server.pki_enabled = True
    server.uses_shared_cert = True
    server.api_key = None
    await db.commit()
    return {"success": True}


@router.post("/migrate-all")
async def migrate_all_servers(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Параллельно мигрировать все per-server ноды и собрать список legacy."""
    result = await db.execute(select(Server))
    servers = result.scalars().all()
    keygen = request.app.state.pki

    # Ноды на осознанном персональном сертификате (dedicated_cert) не трогаем —
    # заливка общего cert вернула бы на них ключ, общий для всего парка
    candidates = [s for s in servers if not s.uses_shared_cert and not s.dedicated_cert]
    auto_targets = [s for s in candidates if s.pki_enabled]
    manual_targets = [s for s in candidates if not s.pki_enabled]

    auto_migrated: list[dict] = []
    failed: list[dict] = []

    # Bounded fan-out: на сотнях нод не пушим сертификат разом во все mTLS-соединения.
    sem = asyncio.Semaphore(20)

    async def _one(s: Server) -> None:
        async with sem:
            try:
                await push_shared_cert_to_node(s, keygen)
                s.uses_shared_cert = True
                auto_migrated.append({"id": s.id, "name": s.name})
            except Exception as exc:
                failed.append({"id": s.id, "name": s.name, "error": str(exc)})

    if auto_targets:
        await asyncio.gather(*(_one(s) for s in auto_targets))
        await db.commit()

    return {
        "auto_migrated": auto_migrated,
        "failed": failed,
        "manual_required": [{"id": s.id, "name": s.name} for s in manual_targets],
        "token": build_installer_token(keygen, panel_ip=await resolve_panel_ip()) if manual_targets else None,
    }


@router.put("/{server_id}")
async def update_server(
    server_id: int,
    data: ServerUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    
    if not server:
        raise HTTPException(status_code=404)
    
    was_active = server.is_active
    old_folder = server.folder
    old_url = server.url
    old_api_key = server.api_key
    old_proxy = server.proxy_url

    update_data = data.model_dump(exclude_unset=True)
    if "url" in update_data:
        update_data["url"] = update_data["url"].rstrip("/")

    for key, value in update_data.items():
        setattr(server, key, value)

    await db.commit()
    _invalidate_list_cache()

    node_changed = (
        server.url != old_url
        or server.api_key != old_api_key
        or server.proxy_url != old_proxy
    )
    activated = not was_active and server.is_active

    blocklist = get_blocklist_manager()
    if server.is_active and (activated or node_changed):
        asyncio.ensure_future(blocklist.sync_single_node_by_id(server_id))
    # Адрес или активность сервера меняют набор IP нод в белом списке всего парка,
    # папка — закрыт ли на нём ping
    if server.url != old_url or server.is_active != was_active or server.folder != old_folder:
        blocklist.request_allowlist_push()

    return {"success": True, "message": "Server updated"}


@router.delete("/{server_id}")
async def delete_server(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    
    if not server:
        raise HTTPException(status_code=404)
    
    await db.delete(server)
    await db.commit()
    get_latest_metrics().forget(server_id)
    # IP удалённой ноды мог уйти хостеру и достаться чужому — из белого списка его убрать сразу
    get_blocklist_manager().request_allowlist_push()

    return {"success": True, "message": "Server deleted"}


@router.post("/reorder")
async def reorder_servers(
    data: ServerReorder,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    if not data.server_ids:
        return {"success": True, "message": "Servers reordered"}

    # Один executemany вместо UPDATE на каждый сервер. Через connection(), а не
    # session.execute(): ORM-путь bulk-by-PK требует id внутри values, нам нужен
    # WHERE по bindparam.
    conn = await db.connection()
    await conn.execute(
        update(Server)
        .where(Server.id == bindparam("sid"))
        .values(position=bindparam("pos")),
        [
            {"sid": server_id, "pos": position}
            for position, server_id in enumerate(data.server_ids)
        ],
    )

    await db.commit()
    _invalidate_list_cache()

    return {"success": True, "message": "Servers reordered"}


@router.post("/{server_id}/test")
async def test_server_connection(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    
    if not server:
        raise HTTPException(status_code=404)
    
    try:
        client = get_node_client(server)
        response = await client.get(
            f"{server.url}/api/version",
            headers=node_auth_headers(server),
            timeout=10.0,
        )

        if response.status_code == 200:
            data = response.json()
            # Живая проба — заодно самый быстрый способ узнать права: не ждём
            # цикла метрик и показываем оператору непонятые ноде слова
            found, caps = capabilities_from_version(data)
            if found:
                await remember_capabilities(server.id, caps)
            return {
                "success": True,
                "status": "online",
                "server_name": data.get("node_name", "Unknown"),
                "version": data.get("version"),
                "capabilities": caps,
                "capabilities_unknown": data.get("capabilities_unknown") or [],
            }
        else:
            return {
                "success": False,
                "status": "error",
                "message": f"HTTP {response.status_code}"
            }
    except httpx.TimeoutException:
        return {"success": False, "status": "timeout", "message": "Connection timeout"}
    except Exception as e:
        return {"success": False, "status": "error", "message": str(e)}
