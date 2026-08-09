"""
Background metrics collector for the panel.
Polls all servers every N seconds and stores metrics in local DB.
Disk speeds are derived here; network speed and traffic history belong to traffic_ingest.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select, delete, update, bindparam, func, ColumnElement
from app.services.http_client import get_node_client, node_auth_headers
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session, Base
from app.models import (
    Server, ServerCache, MetricsSnapshot, AggregatedMetrics, PanelSettings,
    ServerDowntime, ServerTraffic,
)
from app.services.traffic_ingest import get_traffic_ingest
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)

# Default intervals (used if settings not in DB)
DEFAULT_METRICS_INTERVAL = 10  # seconds (recommended: 10-15s)
DEFAULT_HAPROXY_INTERVAL = 300  # seconds (5 minutes - HAProxy data changes rarely)


class ErrorTypes:
    TIMEOUT = "Connection timeout"
    CONNECTION_REFUSED = "Connection refused"
    SSL_ERROR = "SSL certificate error"
    AUTH_ERROR = "Authentication failed"
    SERVER_ERROR = "Server error"
    PROXY_ERROR = "Proxy connection error"
    UNKNOWN = "Unknown error"


class DiskIoState:
    """Предыдущая выборка дисковых счётчиков — база для расчёта скорости.

    Сетевые счётчики здесь не хранятся: их база лежит в PostgreSQL (traffic_ingest),
    иначе рестарт панели терял бы её и первый цикл после старта давал нули.
    Для дисков историческая точность не нужна, память переживает цикл.
    """

    def __init__(self):
        self.prev_read: int = 0
        self.prev_write: int = 0
        self.prev_time: float = 0.0


class MetricsCollector:
    """Collects metrics from all servers and stores in panel DB"""
    
    XRAY_CHECK_INTERVAL = 120
    HAPROXY_RETRY_INTERVAL = 30  # как часто досинхронизировать ожившие ноды
    ACTIVE_REFRESH_INTERVAL = 5
    ACTIVITY_TTL = 60
    
    HTTP_CONCURRENCY = 50  # max parallel HTTP requests to nodes
    DB_CONCURRENCY = 10    # max parallel DB sessions
    CLEANUP_INTERVAL = 300 # cleanup every 5 minutes, not every cycle
    CLEANUP_CHUNK_ROWS = 50_000  # строк за один DELETE ретеншена
    CLEANUP_MAX_CHUNKS = 20      # потолок проходов за вызов, хвост уйдёт в следующий

    CB_FAILURE_THRESHOLD = 3   # после стольких подряд неудач нода уходит в skip
    CB_SKIP_CYCLES = 3         # пропускать N циклов перед повторной попыткой

    DOWN_THRESHOLD_SECONDS = 120  # сколько молчания считаем "definitively offline" для recovery
    RECONCILE_CONCURRENCY = 5     # одновременных восстановлений оживших нод

    DOWNTIME_KIND_NODE = "node"
    DOWNTIME_KIND_PANEL = "panel"
    # Открытый интервал простоя старше суток — след падения самой панели, а не живого простоя.
    ORPHANED_DOWNTIME_AFTER = timedelta(days=1)
    # Ниже этого перерыва в сборе интервал не заводим: обычный рестарт панели
    # укладывается в секунды, а дельту счётчиков за него нода донесёт сама.
    PANEL_GAP_THRESHOLD = timedelta(minutes=5)

    MIN_SPEED_INTERVAL_SECONDS = 0.5  # ниже этого dt деление даёт шум, а не скорость
    NODE_VERSION_MAX_LEN = 20         # ширина servers.node_version

    HAPROXY_ENDPOINTS = (
        ("/api/haproxy/status", "status"),
        ("/api/haproxy/rules", "rules"),
        ("/api/haproxy/certs/all", "certs"),
        ("/api/haproxy/firewall/rules", "firewall"),
    )
    HAPROXY_FETCH_TIMEOUT = 15.0
    HAPROXY_FAST_FETCH_TIMEOUT = 10.0

    # Жёсткий потолок на весь запрос метрик, поверх httpx-таймаутов (2+2+10+2=16s).
    # httpx не покрывает SOCKS5-рукопожатие: запрос через прокси может повиснуть навсегда,
    # а один такой await замораживает gather цикла — вся флотилия уходит в offline (26.07.2026).
    FETCH_HARD_TIMEOUT = 20

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._aggregation_task: Optional[asyncio.Task] = None
        self._haproxy_task: Optional[asyncio.Task] = None
        self._settings_task: Optional[asyncio.Task] = None
        self._xray_check_task: Optional[asyncio.Task] = None
        self._active_cache_task: Optional[asyncio.Task] = None
        self._haproxy_retry_task: Optional[asyncio.Task] = None
        self._disk_states: dict[int, DiskIoState] = {}
        self._collect_interval = DEFAULT_METRICS_INTERVAL
        self._haproxy_interval = DEFAULT_HAPROXY_INTERVAL

        self._active_servers: dict[int, float] = {}
        
        self._raw_retention_hours = 24
        self._hourly_retention_days = 30
        self._daily_retention_days = 365
        self._traffic_hour_retention_days = 14
        self._traffic_day_retention_days = 400
        
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        self._last_hourly_aggregation: datetime = now_utc - timedelta(hours=2)
        self._last_daily_aggregation: datetime = now_utc - timedelta(days=2)
        self._last_cleanup: float = 0
        
        self._http_sem = asyncio.Semaphore(self.HTTP_CONCURRENCY)
        self._db_sem = asyncio.Semaphore(self.DB_CONCURRENCY)

        self._node_failures: dict[int, int] = {}
        self._node_skip_cycles: dict[int, int] = {}

        # Наблюдаемый переход offline → online для авто-восстановления состояния ноды.
        self._down_servers: set[int] = set()
        # Сервера с незакрытым интервалом простоя. Отдельно от _down_servers: интервал
        # мог открыть прошлый запуск панели, и закрыть его должен первый же удачный опрос.
        self._open_downtime: set[int] = set()
        self._reconcile_inflight: set[int] = set()
        self._reconcile_sem = asyncio.Semaphore(self.RECONCILE_CONCURRENCY)

    def notify_activity(self, server_id: int):
        """Mark server as having client activity — triggers fast HAProxy cache refresh (every 5s)"""
        self._active_servers[server_id] = time.time()
    
    async def _load_settings(self):
        """Load collector intervals from database settings"""
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(PanelSettings).where(
                        PanelSettings.key.in_([
                            'metrics_collect_interval', 'haproxy_collect_interval'
                        ])
                    )
                )
                settings = {s.key: s.value for s in result.scalars().all()}
                
                if 'metrics_collect_interval' in settings:
                    new_interval = int(settings['metrics_collect_interval'])
                    if 5 <= new_interval <= 300:
                        if new_interval != self._collect_interval:
                            logger.info(f"Metrics interval changed: {self._collect_interval}s -> {new_interval}s")
                            self._collect_interval = new_interval
                
                if 'haproxy_collect_interval' in settings:
                    new_interval = int(settings['haproxy_collect_interval'])
                    if 30 <= new_interval <= 600:
                        if new_interval != self._haproxy_interval:
                            logger.info(f"HAProxy interval changed: {self._haproxy_interval}s -> {new_interval}s")
                            self._haproxy_interval = new_interval
        except Exception as e:
            logger.debug(f"Failed to load collector settings: {e}")
    
    async def _settings_loop(self):
        """Background loop to reload settings periodically"""
        while self._running:
            try:
                await asyncio.sleep(30)  # Check settings every 30 seconds
                await self._load_settings()
            except Exception as e:
                logger.debug(f"Settings reload error: {e}")
    
    async def start(self):
        """Start background collection"""
        if self._running:
            return
        
        # Load settings before starting
        await self._load_settings()
        await self._record_panel_gap()
        await self._close_orphaned_downtime()
        await self._load_open_downtime()
        # start() читает счётчики из БД и поднимает собственный цикл флаша. Единичный сбой
        # БД здесь не должен ронять подъём панели: start_collector() в lifespan вызывается
        # без try/except, и исключение отсюда остановило бы весь бэкенд.
        try:
            await get_traffic_ingest().start()
        except Exception as e:
            logger.error(f"Traffic ingest failed to start: {e}")

        self._running = True
        self._task = asyncio.create_task(self._collection_loop())
        self._aggregation_task = asyncio.create_task(self._aggregation_loop())
        self._haproxy_task = asyncio.create_task(self._haproxy_cache_loop())
        self._settings_task = asyncio.create_task(self._settings_loop())
        self._xray_check_task = asyncio.create_task(self._xray_check_loop())
        self._active_cache_task = asyncio.create_task(self._active_haproxy_cache_loop())
        self._haproxy_retry_task = asyncio.create_task(self._haproxy_pending_sync_loop())
        logger.info(f"Metrics collector started (interval: {self._collect_interval}s, haproxy: {self._haproxy_interval}s)")

    async def stop(self):
        """Stop background collection"""
        self._running = False
        for task in [self._task, self._aggregation_task, self._haproxy_task, self._settings_task, self._xray_check_task, self._active_cache_task, self._haproxy_retry_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        # Финальный flush уже после остановки цикла сбора — иначе он гнался бы с observe().
        await get_traffic_ingest().stop()
        logger.info("Metrics collector stopped")

    async def _collection_loop(self):
        """Main collection loop"""
        while self._running:
            try:
                await self._collect_all_servers()
            except Exception as e:
                logger.error(f"Collection error: {e}")
            
            await asyncio.sleep(self._collect_interval)
    
    async def _collect_all_servers(self):
        """Collect metrics from all active servers with bounded concurrency."""
        async with async_session() as db:
            result = await db.execute(
                select(Server).where(Server.is_active == True)
            )
            servers = result.scalars().all()
        
        # Phase 1: fetch metrics from all servers (HTTP-bound, limited by semaphore)
        results = await asyncio.gather(
            *[self._fetch_server_metrics(server) for server in servers],
            return_exceptions=True
        )
        
        # Phase 2: batch write all results in a single DB session
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        snapshots = []
        server_updates = []
        recovered_ids: list[int] = []
        newly_down: list[tuple[int, datetime]] = []
        downtime_over: list[int] = []

        for server, result in zip(servers, results):
            if isinstance(result, Exception):
                continue

            metrics, error_info = result
            if metrics:
                # Наблюдаемое оживание: нода молчала дольше порога, теперь снова ответила.
                if server.id in self._down_servers:
                    self._down_servers.discard(server.id)
                    recovered_ids.append(server.id)
                if server.id in self._open_downtime:
                    self._open_downtime.discard(server.id)
                    downtime_over.append(server.id)
                snapshot_data = self._build_snapshot(server.id, metrics, now_utc)
                if snapshot_data:
                    snapshots.append(snapshot_data)
                node_version = metrics.get("agent_version")
                tracked_ports = self._tracked_ports_json(metrics)
                server_updates.append({
                    "id": server.id,
                    "last_seen": now_utc,
                    "last_error": None,
                    "error_code": None,
                    "last_metrics": json.dumps(metrics),
                    # Старые ноды этих полей не присылают — пишем обратно то, что уже в базе.
                    "node_version": node_version[:self.NODE_VERSION_MAX_LEN] if node_version else server.node_version,
                    "tracked_ports": tracked_ports if tracked_ports is not None else server.tracked_ports,
                })
            elif error_info:
                # Помечаем "definitively offline" только если раньше нода была живой и давно молчит.
                if server.id not in self._down_servers and server.last_seen is not None and \
                        self._seconds_since(server.last_seen, now_utc) >= self.DOWN_THRESHOLD_SECONDS:
                    self._down_servers.add(server.id)
                    if server.id not in self._open_downtime:
                        self._open_downtime.add(server.id)
                        newly_down.append((server.id, self._to_naive_utc(server.last_seen)))
                server_updates.append({
                    "id": server.id,
                    "last_error": error_info["message"],
                    "error_code": error_info["code"]
                })

        if snapshots or server_updates:
            success_updates = [u for u in server_updates if "last_seen" in u]
            error_updates = [u for u in server_updates if "last_seen" not in u]

            async with async_session() as db:
                if snapshots:
                    await db.execute(MetricsSnapshot.__table__.insert(), snapshots)

                # Bulk UPDATE через connection() — обходит ORM bulk-by-PK путь,
                # который требует id внутри values; нам нужен WHERE с bindparam.
                if success_updates or error_updates:
                    conn = await db.connection()

                if success_updates:
                    success_params = [
                        {
                            "sid": upd["id"],
                            "last_seen": upd["last_seen"],
                            "last_error": upd["last_error"],
                            "error_code": upd["error_code"],
                            "last_metrics": upd["last_metrics"],
                            "node_version": upd["node_version"],
                            "tracked_ports": upd["tracked_ports"],
                        }
                        for upd in success_updates
                    ]
                    await conn.execute(
                        update(Server)
                        .where(Server.id == bindparam("sid"))
                        .values(
                            last_seen=bindparam("last_seen"),
                            last_error=bindparam("last_error"),
                            error_code=bindparam("error_code"),
                            last_metrics=bindparam("last_metrics"),
                            node_version=bindparam("node_version"),
                            tracked_ports=bindparam("tracked_ports"),
                        ),
                        success_params,
                    )

                if error_updates:
                    error_params = [
                        {
                            "sid": upd["id"],
                            "last_error": upd["last_error"],
                            "error_code": upd["error_code"],
                        }
                        for upd in error_updates
                    ]
                    await conn.execute(
                        update(Server)
                        .where(Server.id == bindparam("sid"))
                        .values(
                            last_error=bindparam("last_error"),
                            error_code=bindparam("error_code"),
                        ),
                        error_params,
                    )

                await db.commit()

        if newly_down or downtime_over:
            await self._record_downtime(newly_down, downtime_over, now_utc)

        # Восстановление состояния оживших нод — last_seen уже закоммичен (сервер считается online).
        for sid in recovered_ids:
            asyncio.create_task(self._launch_reconcile(sid))

        # Cleanup throttled — only every CLEANUP_INTERVAL seconds
        now = time.time()
        if now - self._last_cleanup >= self.CLEANUP_INTERVAL:
            async with async_session() as db:
                await self._cleanup_old_data(db)
            self._last_cleanup = now
    
    async def _fetch_server_metrics(self, server: Server) -> tuple[Optional[dict], Optional[dict]]:
        """Fetch metrics from a single server with HTTP semaphore + circuit breaker."""
        # Circuit breaker: пропускаем нескольких циклов для сбоящих нод
        skip_left = self._node_skip_cycles.get(server.id, 0)
        if skip_left > 0:
            self._node_skip_cycles[server.id] = skip_left - 1
            return None, {"message": ErrorTypes.TIMEOUT, "code": 504}

        async with self._http_sem:
            try:
                result = await asyncio.wait_for(self._fetch_metrics(server), self.FETCH_HARD_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(f"Hard timeout ({self.FETCH_HARD_TIMEOUT}s) fetching metrics from {server.name}")
                result = None, {"message": ErrorTypes.TIMEOUT, "code": 504}

        metrics, error_info = result
        if metrics is not None:
            if self._node_failures.pop(server.id, 0):
                self._node_skip_cycles.pop(server.id, None)
        elif error_info is not None:
            failures = self._node_failures.get(server.id, 0) + 1
            self._node_failures[server.id] = failures
            if failures >= self.CB_FAILURE_THRESHOLD:
                self._node_skip_cycles[server.id] = self.CB_SKIP_CYCLES
        return result

    async def _fetch_metrics(self, server: Server) -> tuple[Optional[dict], Optional[dict]]:
        """Fetch current metrics from server API. Returns (metrics, error_info)"""
        url = f"{server.url}/api/metrics"

        try:
            client = get_node_client(server)
            # Таймаут берётся из клиента (_NODE_TIMEOUT = 2/5/2/2) — без лишних аллокаций.
            response = await client.get(url, headers=node_auth_headers(server))
            if response.status_code == 200:
                data = response.json()
                # Момент ответа ноды — база времени для расчёта скоростей.
                # Сборка снапшотов идёт после gather по всем нодам, и её момент
                # плавает вместе с самой медленной нодой (см. _build_snapshot).
                data["collected_at"] = time.time()
                return data, None
            elif response.status_code == 401 or response.status_code == 403:
                return None, {"message": ErrorTypes.AUTH_ERROR, "code": response.status_code}
            else:
                return None, {"message": f"{ErrorTypes.SERVER_ERROR}: HTTP {response.status_code}", "code": response.status_code}
        except httpx.TimeoutException:
            logger.debug(f"Timeout connecting to {server.name}")
            return None, {"message": ErrorTypes.TIMEOUT, "code": 504}
        except httpx.ProxyError:
            # Отдельный тип: оператор должен отличать мёртвый прокси от мёртвой ноды.
            # str(e) не пишем — текст SOCKS-ошибки бесполезен, а прокси-креды секретны.
            logger.debug(f"Proxy error connecting to {server.name}")
            return None, {"message": ErrorTypes.PROXY_ERROR, "code": 502}
        except httpx.ConnectError as e:
            error_str = str(e).lower()
            if "refused" in error_str:
                return None, {"message": ErrorTypes.CONNECTION_REFUSED, "code": 502}
            elif "ssl" in error_str or "certificate" in error_str:
                return None, {"message": ErrorTypes.SSL_ERROR, "code": 495}
            return None, {"message": f"{ErrorTypes.CONNECTION_REFUSED}: {str(e)[:100]}", "code": 502}
        except Exception as e:
            logger.debug(f"Request to {server.name} failed: {e}")
            return None, {"message": f"{ErrorTypes.UNKNOWN}: {str(e)[:100]}", "code": 500}
    
    @staticmethod
    def _to_naive_utc(value: datetime) -> datetime:
        """Колонки timestamptz приходят из PostgreSQL tz-aware, а весь коллектор считает
        в naive UTC — вычитание aware из naive иначе бросает TypeError."""
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def _seconds_since(self, last_seen: datetime, now_utc_naive: datetime) -> float:
        """Возраст last_seen в секундах."""
        return (now_utc_naive - self._to_naive_utc(last_seen)).total_seconds()

    @staticmethod
    def _tracked_ports_json(metrics: dict) -> Optional[str]:
        """Список портов, за которыми нода считает трафик. None — старая нода без поля."""
        ports = (metrics.get("network") or {}).get("ports")
        if not isinstance(ports, list):
            return None
        return json.dumps([p["port"] for p in ports if isinstance(p, dict) and "port" in p])

    async def _record_downtime(
        self, newly_down: list[tuple[int, datetime]], downtime_over: list[int], now_utc: datetime
    ):
        """Границы наблюдаемых простоев нод.

        alert_history для этого не годится: она пишется только при включённых алертах
        и под кулдауном, то есть дырявая по построению, а провал в истории трафика
        без интервала простоя не отличить от честного нуля.
        """
        try:
            async with async_session() as db:
                if newly_down:
                    db.add_all([
                        ServerDowntime(
                            server_id=sid,
                            started_at=started_at,
                            kind=self.DOWNTIME_KIND_NODE,
                        )
                        for sid, started_at in newly_down
                    ])

                if downtime_over:
                    await db.execute(
                        update(ServerDowntime)
                        .where(
                            ServerDowntime.server_id.in_(downtime_over),
                            ServerDowntime.ended_at.is_(None),
                        )
                        .values(ended_at=now_utc)
                    )

                await db.commit()
        except Exception as e:
            logger.error(f"Failed to record downtime intervals: {e}")

    async def _record_panel_gap(self):
        """Отметить перерыв, когда метрики не собирала сама панель.

        Простой ноды видно по её молчанию, а простой панели не видно ниоткуда:
        сбора просто не было. Без отметки такой перерыв на графике трафика
        выглядит как обычный провал наблюдения, и оператор ищет проблему на
        сервере, которого не было. Граница берётся по последнему снятому
        снапшоту — момент, до которого сбор точно шёл; он же переживает падение
        панели, в отличие от записи «на выходе».
        """
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            async with async_session() as db:
                last_seen = (await db.execute(select(func.max(MetricsSnapshot.timestamp)))).scalar()
                if last_seen is None:
                    return

                gap_start = self._to_naive_utc(last_seen)
                if now_utc - gap_start < self.PANEL_GAP_THRESHOLD:
                    return

                server_ids = (
                    await db.execute(select(Server.id).where(Server.is_active == True))
                ).scalars().all()
                if not server_ids:
                    return

                db.add_all([
                    ServerDowntime(
                        server_id=sid,
                        started_at=gap_start,
                        ended_at=now_utc,
                        kind=self.DOWNTIME_KIND_PANEL,
                    )
                    for sid in server_ids
                ])
                await db.commit()
                logger.info(
                    f"Recorded panel collection gap {gap_start} → {now_utc} "
                    f"for {len(server_ids)} server(s)"
                )
        except Exception as e:
            logger.error(f"Failed to record panel collection gap: {e}")

    async def _load_open_downtime(self):
        """Незакрытые интервалы простоя из прошлого запуска панели.

        Без них перезапуск панели терял бы знание об открытом интервале: нода
        ответила бы, а закрывать было бы нечего, и простой остался бы бесконечным.
        """
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(ServerDowntime.server_id).where(ServerDowntime.ended_at.is_(None))
                )
                self._open_downtime = set(result.scalars().all())
        except Exception as e:
            logger.error(f"Failed to load open downtime intervals: {e}")

    async def _close_orphaned_downtime(self):
        """Закрывает интервалы, осиротевшие после падения самой панели.

        Признак сироты — открытый интервал старше суток у ноды, которая с тех пор
        отвечала: границей берём last_seen, последний момент, когда нода точно была
        жива. Если last_seen не сдвинулся с начала интервала, нода лежит до сих пор
        и интервал остаётся открытым.
        """
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - self.ORPHANED_DOWNTIME_AFTER
        try:
            async with async_session() as db:
                orphans = (
                    await db.execute(
                        select(ServerDowntime.id, ServerDowntime.started_at, Server.last_seen)
                        .join(Server, Server.id == ServerDowntime.server_id)
                        .where(
                            ServerDowntime.ended_at.is_(None),
                            ServerDowntime.started_at < cutoff,
                            Server.last_seen.is_not(None),
                        )
                    )
                ).all()

                closed = 0
                for downtime_id, started_at, last_seen in orphans:
                    ended_at = self._to_naive_utc(last_seen)
                    if ended_at <= self._to_naive_utc(started_at):
                        continue
                    await db.execute(
                        update(ServerDowntime)
                        .where(ServerDowntime.id == downtime_id)
                        .values(ended_at=ended_at)
                    )
                    closed += 1

                if closed:
                    await db.commit()
                    logger.info(f"Closed {closed} orphaned downtime interval(s) left by a panel restart")
        except Exception as e:
            logger.error(f"Failed to close orphaned downtime intervals: {e}")

    async def _launch_reconcile(self, server_id: int):
        """Восстанавливает привязанное состояние ноды после наблюдаемого down→up.
        Запускается отдельным task — гасит исключения, чтобы не уронить коллектор."""
        if server_id in self._reconcile_inflight:
            return
        self._reconcile_inflight.add(server_id)
        try:
            from app.services.recovery_reconciler import reconcile_recovered_server
            await reconcile_recovered_server(server_id, self._reconcile_sem)
        except Exception as e:
            logger.error(f"Recovery reconcile failed for server {server_id}: {e}")
        finally:
            self._reconcile_inflight.discard(server_id)

    def _build_snapshot(self, server_id: int, metrics: dict, now_utc: datetime) -> Optional[dict]:
        """Build a snapshot dict for batch insert. Returns None if data invalid."""
        # Счётчики байт сняты в момент ответа конкретной ноды, поэтому и dt считаем
        # между этими моментами. time.time() здесь (после gather по всем нодам) даёт
        # dt, не совпадающий с реальным интервалом счётчиков: при скачке длительности
        # цикла (зависшая нода → circuit breaker) скорость завышалась до ~x2
        # или занижалась до ~x0.5 на один цикл у всех нод сразу.
        current_time = metrics.get("collected_at") or time.time()

        cpu = metrics.get("cpu", {})
        memory = metrics.get("memory", {}).get("ram", {})
        swap = metrics.get("memory", {}).get("swap", {})
        disk = metrics.get("disk", {})
        processes = metrics.get("processes", {})
        system = metrics.get("system", {})

        # Скорость сети берём у учёта трафика: счётчик и формула дельты должны быть
        # одни на всю панель, иначе график скорости разойдётся с историей трафика.
        # Ответ ноды приходит сюда как есть, а снапшоты всей флотилии собираются
        # одним проходом — исключение на одной ноде не должно ронять остальные.
        try:
            speeds = get_traffic_ingest().observe(server_id, metrics, current_time)
            net_rx_speed, net_tx_speed = speeds.rx_bytes_per_sec, speeds.tx_bytes_per_sec
        except Exception as e:
            logger.warning(f"Traffic observe failed for server {server_id}: {e}")
            net_rx_speed, net_tx_speed = 0.0, 0.0

        disk_read = sum(d.get("read_bytes", 0) for d in disk.get("io", {}).values())
        disk_write = sum(d.get("write_bytes", 0) for d in disk.get("io", {}).values())
        disk_read_speed, disk_write_speed = self._disk_speeds(
            server_id, disk_read, disk_write, current_time
        )

        disk_percent = 0.0
        partitions = disk.get("partitions", [])
        if partitions:
            disk_percent = partitions[0].get("percent", 0)
        
        connections = system.get("connections", {})
        connections_count = connections.get("established", 0) + connections.get("listen", 0)
        connections_detailed = system.get("connections_detailed", {})
        tcp_states = connections_detailed.get("tcp", {})
        per_cpu = cpu.get("per_cpu_percent", [])
        
        return {
            "server_id": server_id,
            "timestamp": now_utc,
            "cpu_usage": cpu.get("usage_percent", 0),
            "load_avg_1": cpu.get("load_avg_1", 0),
            "load_avg_5": cpu.get("load_avg_5", 0),
            "load_avg_15": cpu.get("load_avg_15", 0),
            "memory_total": memory.get("total", 0),
            "memory_used": memory.get("used", 0),
            "memory_available": memory.get("available", 0),
            "memory_percent": memory.get("percent", 0),
            "swap_used": swap.get("used", 0),
            "swap_percent": swap.get("percent", 0),
            "net_rx_bytes_per_sec": net_rx_speed,
            "net_tx_bytes_per_sec": net_tx_speed,
            "disk_percent": disk_percent,
            "disk_read_bytes_per_sec": disk_read_speed,
            "disk_write_bytes_per_sec": disk_write_speed,
            "process_count": processes.get("total", 0),
            "connections_count": connections_count,
            "tcp_established": tcp_states.get("established"),
            "tcp_listen": tcp_states.get("listen"),
            "tcp_time_wait": tcp_states.get("time_wait"),
            "tcp_close_wait": tcp_states.get("close_wait"),
            "tcp_syn_sent": tcp_states.get("syn_sent"),
            "tcp_syn_recv": tcp_states.get("syn_recv") if tcp_states.get("syn_recv") is not None else tcp_states.get("syn_received"),
            "tcp_fin_wait": tcp_states.get("fin_wait"),
            "per_cpu_percent": json.dumps(per_cpu) if per_cpu else None,
        }

    def _disk_speeds(
        self, server_id: int, read_bytes: int, write_bytes: int, sampled_at: float
    ) -> tuple[float, float]:
        """Скорость чтения/записи по разнице с предыдущей выборкой этой же ноды."""
        state = self._disk_states.get(server_id)
        if state is None:
            state = DiskIoState()
            self._disk_states[server_id] = state

        read_speed = 0.0
        write_speed = 0.0
        dt = sampled_at - state.prev_time
        if state.prev_time > 0 and dt > self.MIN_SPEED_INTERVAL_SECONDS:
            read_diff = read_bytes - state.prev_read
            write_diff = write_bytes - state.prev_write
            if read_diff >= 0:
                read_speed = read_diff / dt
            if write_diff >= 0:
                write_speed = write_diff / dt

        state.prev_read = read_bytes
        state.prev_write = write_bytes
        state.prev_time = sampled_at
        return read_speed, write_speed

    async def _delete_in_chunks(
        self, db: AsyncSession, model: type[Base], *conditions: ColumnElement[bool]
    ) -> int:
        """Удаляет подходящие строки порциями, коммитя каждый проход.

        Один DELETE на миллионы строк держал бы длинную транзакцию и всплеск WAL
        прямо внутри цикла сбора метрик. Потолок проходов не даёт очистке залипнуть
        на одном вызове — необработанный хвост уйдёт в следующий.
        """
        deleted = 0
        for _ in range(self.CLEANUP_MAX_CHUNKS):
            chunk = (
                select(model.id)
                .where(*conditions)
                .limit(self.CLEANUP_CHUNK_ROWS)
                .scalar_subquery()
            )
            result = await db.execute(
                delete(model)
                .where(model.id.in_(chunk))
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            deleted += result.rowcount
            if result.rowcount < self.CLEANUP_CHUNK_ROWS:
                return deleted

        logger.info(
            f"Retention cleanup of {model.__tablename__} hit the chunk cap: "
            f"{deleted} rows removed, rest deferred to next run"
        )
        return deleted

    async def _cleanup_old_data(self, db: AsyncSession):
        """Remove data older than retention periods.

        Note: We use naive UTC datetime for consistent comparisons across platforms.
        """
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

        # Cleanup raw data (24 hours)
        raw_cutoff = now_utc - timedelta(hours=self._raw_retention_hours)
        await self._delete_in_chunks(
            db, MetricsSnapshot, MetricsSnapshot.timestamp < raw_cutoff
        )

        # Cleanup hourly aggregated data (30 days)
        hourly_cutoff = now_utc - timedelta(days=self._hourly_retention_days)
        await self._delete_in_chunks(
            db, AggregatedMetrics,
            AggregatedMetrics.period_type == 'hour',
            AggregatedMetrics.timestamp < hourly_cutoff,
        )

        # Cleanup daily aggregated data (365 days)
        daily_cutoff = now_utc - timedelta(days=self._daily_retention_days)
        await self._delete_in_chunks(
            db, AggregatedMetrics,
            AggregatedMetrics.period_type == 'day',
            AggregatedMetrics.timestamp < daily_cutoff,
        )

        # История трафика. Часовые бакеты нужны только графикам за сутки и неделю,
        # поэтому живут две недели; суточные держим дольше годового окна графиков,
        # чтобы ретеншн не съел только что импортированную легаси-историю.
        await self._delete_in_chunks(
            db, ServerTraffic,
            ServerTraffic.period_type == 'hour',
            ServerTraffic.bucket < now_utc - timedelta(days=self._traffic_hour_retention_days),
        )
        await self._delete_in_chunks(
            db, ServerTraffic,
            ServerTraffic.period_type == 'day',
            ServerTraffic.bucket < now_utc - timedelta(days=self._traffic_day_retention_days),
        )

        # Закрытые интервалы простоя нужны ровно для разрывов на графиках, дальше горизонта
        # часовых данных они не читаются. Открытые не трогаем — простой может длиться.
        await self._delete_in_chunks(
            db, ServerDowntime,
            ServerDowntime.ended_at.isnot(None),
            ServerDowntime.ended_at < now_utc - timedelta(days=self._traffic_day_retention_days),
        )

    async def _aggregation_loop(self):
        """Background loop for data aggregation"""
        while self._running:
            try:
                await asyncio.sleep(60)  # Check every minute
                
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                
                # Hourly aggregation (run at the start of each hour)
                if now - self._last_hourly_aggregation >= timedelta(hours=1):
                    # Хвост дельт закрывшегося часа ещё лежит в аккумуляторе: без флаша
                    # агрегация прочитала бы бакет неполным, а ON CONFLICT DO NOTHING
                    # потом уже не исправил бы строку.
                    await get_traffic_ingest().flush()
                    async with async_session() as db:
                        await self._aggregate_hourly(db)
                    self._last_hourly_aggregation = now.replace(minute=0, second=0, microsecond=0)
                
                # Daily aggregation (run once per day at midnight)
                if now - self._last_daily_aggregation >= timedelta(days=1):
                    async with async_session() as db:
                        await self._aggregate_daily(db)
                    self._last_daily_aggregation = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    
            except Exception as e:
                logger.error(f"Aggregation error: {e}")
    
    async def _haproxy_cache_loop(self):
        """Background loop for caching HAProxy data"""
        while self._running:
            try:
                await asyncio.sleep(self._haproxy_interval)
                await self._cache_haproxy_data()
            except Exception as e:
                logger.error(f"HAProxy cache error: {e}")

    async def _active_haproxy_cache_loop(self):
        """Fast refresh loop (every 5s) for servers with recent client activity."""
        while self._running:
            try:
                await asyncio.sleep(self.ACTIVE_REFRESH_INTERVAL)
                
                now = time.time()
                expired = [sid for sid, ts in self._active_servers.items()
                           if now - ts > self.ACTIVITY_TTL]
                for sid in expired:
                    del self._active_servers[sid]
                
                if not self._active_servers:
                    continue
                
                active_ids = list(self._active_servers.keys())
                async with async_session() as db:
                    result = await db.execute(
                        select(Server).where(Server.id.in_(active_ids), Server.is_active == True)
                    )
                    servers = result.scalars().all()
                
                if servers:
                    tasks = [
                        self._cache_server_haproxy(s, self.HAPROXY_FAST_FETCH_TIMEOUT)
                        for s in servers
                    ]
                    await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as e:
                logger.error(f"Active HAProxy cache error: {e}")

    async def _cache_haproxy_data(self):
        """Cache HAProxy status/rules/certs/firewall for all servers"""
        async with async_session() as db:
            result = await db.execute(
                select(Server).where(Server.is_active == True)
            )
            servers = result.scalars().all()

        # Each server uses its own session
        tasks = [self._cache_server_haproxy(server, self.HAPROXY_FETCH_TIMEOUT) for server in servers]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _cache_server_haproxy(self, server: Server, timeout: float):
        """Cache HAProxy data for a single server into server_cache table."""
        try:
            async with self._http_sem:
                client = get_node_client(server)
                headers = node_auth_headers(server)
                haproxy_data = {}

                for endpoint, key in self.HAPROXY_ENDPOINTS:
                    try:
                        res = await client.get(f"{server.url}{endpoint}", headers=headers, timeout=timeout)
                        if res.status_code == 200:
                            haproxy_data[key] = res.json()
                    except Exception:
                        pass

            if not haproxy_data:
                return

            async with self._db_sem:
                async with async_session() as db:
                    result = await db.execute(
                        select(ServerCache).where(ServerCache.server_id == server.id)
                    )
                    cache_row = result.scalar_one_or_none()

                    existing = {}
                    if cache_row and cache_row.last_haproxy_data:
                        try:
                            existing = json.loads(cache_row.last_haproxy_data)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    existing.update(haproxy_data)
                    existing["cached_at"] = datetime.now(timezone.utc).isoformat()

                    haproxy_json = json.dumps(existing)
                    stmt = pg_insert(ServerCache).values(
                        server_id=server.id,
                        last_haproxy_data=haproxy_json
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=['server_id'],
                        set_={'last_haproxy_data': haproxy_json}
                    )
                    await db.execute(stmt)
                    await db.commit()
        except Exception as e:
            logger.debug(f"Failed to cache HAProxy for {server.name}: {e}")

    async def _haproxy_pending_sync_loop(self):
        """Досинхронизирует ожившие ноды с отложенной раскаткой конфигов (offline → online):
        HAProxy-профили и nginx-профили Remnawave в одном цикле."""
        from app.services.haproxy_profile_sync import retry_pending_haproxy_syncs
        from app.services.remnawave_nginx_sync import retry_pending_remnawave_nginx_syncs

        await asyncio.sleep(20)  # дать нодам подняться после старта
        while self._running:
            try:
                async with async_session() as db:
                    synced = await retry_pending_haproxy_syncs(db)
                if synced:
                    logger.info("HAProxy: досинхронизировано %d оживших нод", synced)
            except Exception as e:
                logger.error(f"HAProxy pending sync error: {e}")
            try:
                async with async_session() as db:
                    synced = await retry_pending_remnawave_nginx_syncs(db)
                if synced:
                    logger.info("Remnawave nginx: досинхронизировано %d оживших нод", synced)
            except Exception as e:
                logger.error(f"Remnawave nginx pending sync error: {e}")
            await asyncio.sleep(self.HAPROXY_RETRY_INTERVAL)

    async def _xray_check_loop(self):
        """Periodically check which servers have a running remnanode container."""
        await asyncio.sleep(10)  # Initial delay to let servers come online
        while self._running:
            try:
                await self._check_xray_on_all_servers()
            except Exception as e:
                logger.error(f"Xray check error: {e}")
            await asyncio.sleep(self.XRAY_CHECK_INTERVAL)
    
    async def _check_xray_on_all_servers(self):
        """Check xray availability on every active server, batch update
        has_xray_node + remnawave_nginx_detected (обнаружение установки Remnawave)."""
        from app.services.remnawave_nginx_sync import get_remnawave_nginx_path

        async with async_session() as db:
            result = await db.execute(
                select(Server).where(Server.is_active == True)
            )
            servers = result.scalars().all()
            install_path = await get_remnawave_nginx_path(db)

        if not servers:
            return

        async def _probe(server: Server) -> tuple[int, bool, bool]:
            async with self._http_sem:
                available = False
                detected = False
                try:
                    client = get_node_client(server)
                    resp = await client.get(
                        f"{server.url}/api/remnawave/status",
                        headers=node_auth_headers(server),
                        timeout=12.0,
                    )
                    if resp.status_code == 200:
                        available = bool(resp.json().get("available", False))
                except Exception:
                    return server.id, False, False
                try:
                    # 404 на старых нодах без эндпоинта — просто «не обнаружено»
                    resp = await client.get(
                        f"{server.url}/api/remnawave/nginx/discover",
                        headers=node_auth_headers(server),
                        params={"path": install_path},
                        timeout=12.0,
                    )
                    if resp.status_code == 200:
                        detected = bool(resp.json().get("found", False))
                except Exception:
                    pass
            return server.id, available, detected

        results = await asyncio.gather(*[_probe(s) for s in servers], return_exceptions=True)

        # Batch update changed servers in one session
        changes = []
        for server, r in zip(servers, results):
            if isinstance(r, tuple):
                sid, available, detected = r
                if available != server.has_xray_node or detected != server.remnawave_nginx_detected:
                    changes.append((sid, available, detected))

        if changes:
            try:
                async with async_session() as db:
                    for sid, has_xray, detected in changes:
                        await db.execute(
                            update(Server).where(Server.id == sid).values(
                                has_xray_node=has_xray,
                                remnawave_nginx_detected=detected,
                            )
                        )
                    await db.commit()
                for sid, has_xray, detected in changes:
                    logger.info(
                        f"Server {sid}: has_xray_node = {has_xray}, remnawave_nginx_detected = {detected}"
                    )
            except Exception as e:
                logger.warning(f"Failed to batch update xray/remnawave detection: {e}")
    
    async def _aggregate_hourly(self, db: AsyncSession):
        """Aggregate raw metrics to hourly summaries — single SQL for all servers.

        Суммарный трафик берётся готовым из server_traffic, а не интегрированием
        скорости по снапшотам: интеграл занижал бы час с пропущенными циклами
        и врал бы задним числом при смене metrics_collect_interval. LEFT JOIN даёт
        не больше одной строки на сервер (uq_server_traffic), поэтому MAX() —
        просто способ вытащить её значение в группе.
        """
        from sqlalchemy import text

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        hour_end = now.replace(minute=0, second=0, microsecond=0)
        hour_start = hour_end - timedelta(hours=1)

        await db.execute(text("""
            INSERT INTO aggregated_metrics (
                server_id, timestamp, period_type,
                avg_cpu, max_cpu, avg_load,
                avg_memory_percent, max_memory_percent, avg_disk_percent,
                total_rx_bytes, total_tx_bytes, avg_rx_speed, avg_tx_speed,
                avg_disk_read_speed, avg_disk_write_speed,
                avg_tcp_established, avg_tcp_listen, avg_tcp_time_wait,
                avg_tcp_close_wait, avg_tcp_syn_sent, avg_tcp_syn_recv,
                avg_tcp_fin_wait, data_points
            )
            SELECT
                m.server_id, :hour_start, 'hour',
                AVG(m.cpu_usage), MAX(m.cpu_usage), AVG(m.load_avg_1),
                AVG(m.memory_percent), MAX(m.memory_percent), AVG(m.disk_percent),
                COALESCE(MAX(t.rx_bytes), 0), COALESCE(MAX(t.tx_bytes), 0),
                AVG(m.net_rx_bytes_per_sec), AVG(m.net_tx_bytes_per_sec),
                AVG(m.disk_read_bytes_per_sec), AVG(m.disk_write_bytes_per_sec),
                AVG(m.tcp_established), AVG(m.tcp_listen), AVG(m.tcp_time_wait),
                AVG(m.tcp_close_wait), AVG(m.tcp_syn_sent), AVG(m.tcp_syn_recv),
                AVG(m.tcp_fin_wait), COUNT(*)
            FROM metrics_snapshots m
            LEFT JOIN server_traffic t
                   ON t.server_id = m.server_id
                  AND t.period_type = 'hour'
                  AND t.scope = 'total'
                  AND t.scope_key = ''
                  AND t.bucket = :hour_start
            WHERE m.timestamp >= :hour_start AND m.timestamp < :hour_end
            GROUP BY m.server_id
            ON CONFLICT DO NOTHING
        """), {"hour_start": hour_start, "hour_end": hour_end})

        await db.commit()
        logger.info(f"Hourly aggregation completed for {hour_start}")
    
    async def _aggregate_daily(self, db: AsyncSession):
        """Aggregate hourly metrics to daily summaries — single SQL for all servers."""
        from sqlalchemy import text
        
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start = day_end - timedelta(days=1)
        
        await db.execute(text("""
            INSERT INTO aggregated_metrics (
                server_id, timestamp, period_type,
                avg_cpu, max_cpu, avg_load,
                avg_memory_percent, max_memory_percent, avg_disk_percent,
                total_rx_bytes, total_tx_bytes, avg_rx_speed, avg_tx_speed,
                avg_disk_read_speed, avg_disk_write_speed,
                avg_tcp_established, avg_tcp_listen, avg_tcp_time_wait,
                avg_tcp_close_wait, avg_tcp_syn_sent, avg_tcp_syn_recv,
                avg_tcp_fin_wait, data_points
            )
            SELECT
                server_id, :day_start, 'day',
                AVG(avg_cpu), MAX(max_cpu), AVG(avg_load),
                AVG(avg_memory_percent), MAX(max_memory_percent), AVG(avg_disk_percent),
                COALESCE(SUM(total_rx_bytes), 0), COALESCE(SUM(total_tx_bytes), 0),
                AVG(avg_rx_speed), AVG(avg_tx_speed),
                AVG(avg_disk_read_speed), AVG(avg_disk_write_speed),
                AVG(avg_tcp_established), AVG(avg_tcp_listen), AVG(avg_tcp_time_wait),
                AVG(avg_tcp_close_wait), AVG(avg_tcp_syn_sent), AVG(avg_tcp_syn_recv),
                AVG(avg_tcp_fin_wait), SUM(data_points)
            FROM aggregated_metrics
            WHERE period_type = 'hour' AND timestamp >= :day_start AND timestamp < :day_end
            GROUP BY server_id
            ON CONFLICT DO NOTHING
        """), {"day_start": day_start, "day_end": day_end})
        
        await db.commit()
        logger.info(f"Daily aggregation completed for {day_start}")


# Singleton instance
_collector: Optional[MetricsCollector] = None


def get_collector() -> MetricsCollector:
    """Get or create metrics collector instance"""
    global _collector
    if _collector is None:
        _collector = MetricsCollector()
    return _collector


async def start_collector():
    """Start the metrics collector"""
    collector = get_collector()
    await collector.start()


async def stop_collector():
    """Stop the metrics collector"""
    collector = get_collector()
    await collector.stop()
