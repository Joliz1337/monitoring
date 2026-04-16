import asyncio
import ipaddress
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update, desc
from sqlalchemy.ext.asyncio import AsyncSession
import re
from urllib.parse import urlparse
from pydantic import BaseModel, field_validator
from typing import Optional
import httpx
import json
from app.services.http_client import get_node_client, node_auth_headers

from app.database import get_db
from app.models import Server, ServerCache, MetricsSnapshot, PanelSettings
from app.auth import verify_auth
from app.services.blocklist_manager import get_blocklist_manager
from app.services.time_sync import get_time_sync_service
import socket
from app.config import get_settings
from app.services.pki import build_installer_token
from app.services.migration import (
    classify_server,
    push_shared_cert_to_node,
)


def _resolve_panel_ip() -> str | None:
    domain = get_settings().domain
    if not domain:
        return None
    try:
        return socket.gethostbyname(domain)
    except socket.gaierror:
        return None

DEFAULT_OFFLINE_THRESHOLD = 60

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


def _resolve_status(server: Server, threshold: int = DEFAULT_OFFLINE_THRESHOLD) -> str:
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


def enrich_metrics_with_speeds(metrics: dict, snapshot: MetricsSnapshot) -> dict:
    """Enrich raw metrics with calculated network/disk speeds from snapshot.

    Node returns raw bytes only, panel calculates speeds from byte differences.
    This function adds the calculated speeds to the metrics dict.
    Speed is distributed only to physical interfaces (is_virtual=false)
    to avoid double-counting traffic on Docker veth/bridge interfaces.
    """
    if not snapshot:
        return metrics

    if "network" in metrics:
        total_rx_speed = snapshot.net_rx_bytes_per_sec or 0
        total_tx_speed = snapshot.net_tx_bytes_per_sec or 0

        if "total" in metrics["network"]:
            metrics["network"]["total"]["rx_bytes_per_sec"] = total_rx_speed
            metrics["network"]["total"]["tx_bytes_per_sec"] = total_tx_speed

        interfaces = metrics["network"].get("interfaces", [])
        if interfaces:
            # Only distribute speed to physical interfaces
            physical = [i for i in interfaces if not i.get("is_virtual", False)]
            phys_rx = sum(i.get("rx_bytes", 0) for i in physical) if physical else 0
            phys_tx = sum(i.get("tx_bytes", 0) for i in physical) if physical else 0

            for iface in interfaces:
                if iface.get("is_virtual", False):
                    iface["rx_bytes_per_sec"] = 0.0
                    iface["tx_bytes_per_sec"] = 0.0
                    continue
                if phys_rx > 0:
                    iface["rx_bytes_per_sec"] = total_rx_speed * iface.get("rx_bytes", 0) / phys_rx
                if phys_tx > 0:
                    iface["tx_bytes_per_sec"] = total_tx_speed * iface.get("tx_bytes", 0) / phys_tx

    if "disk" in metrics and "io" in metrics["disk"]:
        disk_read_speed = snapshot.disk_read_bytes_per_sec or 0
        disk_write_speed = snapshot.disk_write_bytes_per_sec or 0

        io_stats = metrics["disk"]["io"]
        if io_stats:
            total_read = sum(d.get("read_bytes", 0) for d in io_stats.values())
            total_write = sum(d.get("write_bytes", 0) for d in io_stats.values())

            for disk_name, disk_io in io_stats.items():
                if total_read > 0:
                    ratio = disk_io.get("read_bytes", 0) / total_read
                    disk_io["read_bytes_per_sec"] = disk_read_speed * ratio
                if total_write > 0:
                    ratio = disk_io.get("write_bytes", 0) / total_write
                    disk_io["write_bytes_per_sec"] = disk_write_speed * ratio

    return metrics


async def get_latest_snapshot(server_id: int, db: AsyncSession) -> Optional[MetricsSnapshot]:
    """Get the most recent metrics snapshot for a server."""
    result = await db.execute(
        select(MetricsSnapshot)
        .where(MetricsSnapshot.server_id == server_id)
        .order_by(desc(MetricsSnapshot.timestamp))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_latest_snapshots_bulk(server_ids: list[int], db: AsyncSession) -> dict[int, MetricsSnapshot]:
    """Get the most recent metrics snapshot for multiple servers in one query.
    
    Uses PostgreSQL DISTINCT ON for efficient index-only lookups on (server_id, timestamp DESC).
    """
    if not server_ids:
        return {}
    
    result = await db.execute(
        select(MetricsSnapshot)
        .where(MetricsSnapshot.server_id.in_(server_ids))
        .order_by(MetricsSnapshot.server_id, MetricsSnapshot.timestamp.desc())
        .distinct(MetricsSnapshot.server_id)
    )
    
    snapshots = result.scalars().all()
    return {s.server_id: s for s in snapshots}


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


class ServerCreate(BaseModel):
    name: str
    url: str

    @field_validator('url')
    @classmethod
    def validate_url(cls, v: str) -> str:
        return _clean_url(v)


class ServerUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    api_key: Optional[str] = None
    is_active: Optional[bool] = None
    folder: Optional[str] = None

    @field_validator('url')
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _clean_url(v)


class ServerReorder(BaseModel):
    server_ids: list[int]


class MoveServersToFolder(BaseModel):
    server_ids: list[int]
    folder: Optional[str] = None


class RenameServerFolder(BaseModel):
    old_name: str
    new_name: str


class ServerResponse(BaseModel):
    id: int
    name: str
    url: str
    position: int
    is_active: bool

    class Config:
        from_attributes = True


@router.get("")
async def list_servers(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
    include_metrics: bool = False
):
    result = await db.execute(
        select(Server).order_by(Server.position, Server.id)
    )
    servers = result.scalars().all()
    
    snapshots_map = {}
    cache_map: dict[int, ServerCache] = {}
    offline_threshold = DEFAULT_OFFLINE_THRESHOLD
    if include_metrics:
        server_ids = [s.id for s in servers]
        snapshots_map = await get_latest_snapshots_bulk(server_ids, db)

        if server_ids:
            cache_result = await db.execute(
                select(ServerCache).where(ServerCache.server_id.in_(server_ids))
            )
            cache_map = {c.server_id: c for c in cache_result.scalars().all()}

        interval_row = await db.execute(
            select(PanelSettings.value).where(PanelSettings.key == "metrics_collect_interval")
        )
        interval_val = interval_row.scalar_one_or_none()
        collect_interval = int(interval_val) if interval_val else 10
        offline_threshold = max(DEFAULT_OFFLINE_THRESHOLD, collect_interval * 3 + 30)

    servers_data = []
    for s in servers:
        server_info = {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "position": s.position,
            "is_active": s.is_active,
            "folder": s.folder,
            "last_seen": to_iso_utc(s.last_seen),
            "last_error": s.last_error,
            "error_code": s.error_code,
            "pki_enabled": bool(s.pki_enabled),
            "uses_shared_cert": bool(s.uses_shared_cert),
            "auth_kind": classify_server(s),
        }

        if include_metrics and s.last_metrics:
            try:
                metrics = json.loads(s.last_metrics)
                snapshot = snapshots_map.get(s.id)
                server_info["metrics"] = enrich_metrics_with_speeds(metrics, snapshot)
                server_info["status"] = _resolve_status(s, offline_threshold)
            except json.JSONDecodeError:
                server_info["metrics"] = None
                server_info["status"] = "error" if s.last_error else "loading"
        elif include_metrics:
            server_info["metrics"] = None
            server_info["status"] = _resolve_status(s, offline_threshold) if s.last_seen else "loading"
        
        if include_metrics and s.last_speedtest:
            try:
                speedtest_data = json.loads(s.last_speedtest)
                server_info["speedtest"] = {
                    "best_speed_mbps": speedtest_data.get("best_speed_mbps", 0),
                    "best_server": speedtest_data.get("best_server", ""),
                    "ok": speedtest_data.get("ok", False),
                    "tested_at": speedtest_data.get("tested_at"),
                }
            except json.JSONDecodeError:
                pass
        
        # Traffic data from server_cache table
        cache = cache_map.get(s.id)
        if include_metrics and cache and cache.last_traffic_data:
            try:
                traffic_data = json.loads(cache.last_traffic_data)
                summary = traffic_data.get("summary", {})
                total = summary.get("total", {})
                if total:
                    server_info["traffic"] = {
                        "rx_bytes": total.get("rx_bytes", 0),
                        "tx_bytes": total.get("tx_bytes", 0),
                        "days": total.get("days", 30)
                    }
            except json.JSONDecodeError:
                pass
        
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
    return {"token": build_installer_token(keygen, panel_ip=_resolve_panel_ip())}


@router.get("/migration-status")
async def get_migration_status(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Сколько нод ещё не на shared cert — для условного показа баннера."""
    result = await db.execute(select(Server))
    servers = result.scalars().all()
    counters = {"shared": 0, "per_server": 0, "legacy": 0}
    for s in servers:
        counters[classify_server(s)] += 1
    counters["total"] = len(servers)
    counters["needs_migration"] = counters["per_server"] + counters["legacy"]
    return counters


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
        pki_enabled=True,
        uses_shared_cert=True,
        position=next_position,
    )
    db.add(new_server)
    await db.commit()
    await db.refresh(new_server)

    asyncio.ensure_future(
        get_blocklist_manager().sync_single_node_by_id(new_server.id)
    )
    asyncio.ensure_future(
        get_time_sync_service().sync_single_server(new_server.id)
    )

    return {
        "success": True,
        "server": {
            "id": new_server.id,
            "name": new_server.name,
            "url": new_server.url,
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
    folder_value = data.folder.strip() if data.folder and data.folder.strip() else None
    result = await db.execute(select(Server).where(Server.id.in_(data.server_ids)))
    servers = result.scalars().all()
    for s in servers:
        s.folder = folder_value
    await db.commit()
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
    return {"success": True, "unfoldered": len(servers)}


@router.get("/{server_id}")
async def get_server(
    server_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()

    if not server:
        raise HTTPException(status_code=404)

    return {
        "id": server.id,
        "name": server.name,
        "url": server.url,
        "position": server.position,
        "is_active": server.is_active,
        "folder": server.folder,
        "last_seen": to_iso_utc(server.last_seen),
        "last_error": server.last_error,
        "error_code": server.error_code,
        "pki_enabled": bool(server.pki_enabled),
        "uses_shared_cert": bool(server.uses_shared_cert),
        "auth_kind": classify_server(server),
    }


@router.post("/{server_id}/migrate")
async def migrate_server(
    server_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth),
):
    """Перевести ноду на shared cert.

    pki_enabled (per-server) → автоматически через push новой пары cert/key.
    legacy (api_key) → возвращаем installer token, оператор переустанавливает ноду
    и затем подтверждает через `/confirm-migration`.
    """
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        raise HTTPException(status_code=404)

    if server.uses_shared_cert:
        return {"status": "already_shared"}

    keygen = request.app.state.pki

    if server.pki_enabled:
        try:
            await push_shared_cert_to_node(server, keygen)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Node refused cert replacement: {exc}",
            ) from exc
        server.uses_shared_cert = True
        await db.commit()
        return {"status": "auto", "success": True}

    # legacy
    return {
        "status": "manual",
        "token": build_installer_token(keygen, panel_ip=_resolve_panel_ip()),
    }


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

    auto_targets = [s for s in servers if not s.uses_shared_cert and s.pki_enabled]
    manual_targets = [s for s in servers if not s.uses_shared_cert and not s.pki_enabled]

    auto_migrated: list[dict] = []
    failed: list[dict] = []

    async def _one(s: Server) -> None:
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
        "token": build_installer_token(keygen, panel_ip=_resolve_panel_ip()) if manual_targets else None,
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
    
    was_inactive = not server.is_active
    old_url = server.url
    old_api_key = server.api_key

    update_data = data.model_dump(exclude_unset=True)
    if "url" in update_data:
        update_data["url"] = update_data["url"].rstrip("/")

    for key, value in update_data.items():
        setattr(server, key, value)
    
    await db.commit()

    node_changed = server.url != old_url or server.api_key != old_api_key
    activated = was_inactive and server.is_active

    if server.is_active and (activated or node_changed):
        asyncio.ensure_future(
            get_blocklist_manager().sync_single_node_by_id(server_id)
        )
    
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
    
    return {"success": True, "message": "Server deleted"}


@router.post("/reorder")
async def reorder_servers(
    data: ServerReorder,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_auth)
):
    for position, server_id in enumerate(data.server_ids):
        await db.execute(
            update(Server).where(Server.id == server_id).values(position=position)
        )
    
    await db.commit()
    
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
            return {
                "success": True,
                "status": "online",
                "server_name": data.get("node_name", "Unknown"),
                "version": data.get("version")
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
