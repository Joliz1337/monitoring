"""
Background metrics collector for the panel.
Polls all servers on a fixed tick and stores metrics in local DB.
A node with the window sampler returns averages and peaks over the gap since
the previous successful poll; older nodes fall back to one-second rates or to
counter deltas (disk here, network by traffic_ingest).
"""

import asyncio
import json
import logging
import math
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import httpx
from sqlalchemy import Select, select, delete, update, bindparam, func, ColumnElement
from sqlalchemy.orm import defer
from app.services.http_client import get_node_client, node_auth_headers
from app.services.loss_registry import get_loss_registry
from app.services.node_capabilities import (
    Capability,
    normalize_map as normalize_capabilities_map,
    server_allows,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.database import async_session, Base
from app.models import (
    Server, ServerCache, MetricsSnapshot, AggregatedMetrics, PanelSettings,
    ServerDowntime, ServerTraffic,
)
from app.services.haproxy_load_weights import NodeLoad
from app.services.haproxy_load_weights_runner import get_load_weights_runner
from app.services.latest_metrics import get_latest_metrics
from app.services.metrics_rates import NodeRates, poll_window_seconds, snapshot_rates
from app.services.traffic_ingest import get_traffic_ingest
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)

# Default intervals (used if settings not in DB)
DEFAULT_METRICS_INTERVAL = 10  # seconds (recommended: 10-15s)
DEFAULT_HAPROXY_INTERVAL = 300  # seconds (5 minutes - HAProxy data changes rarely)

# Как часто последний ответ ноды сохраняется в servers.last_metrics. Читатели
# берут его из памяти (latest_metrics); копия в базе нужна, только чтобы поднять
# хранилище после перезапуска панели, а запись ~18 КБ JSON на ноду каждый цикл
# давала основной объём WAL.
LAST_METRICS_PERSIST_SECONDS = 300


def next_collection_tick(previous_tick: float, interval: float, now: float) -> float:
    """Следующая отметка фиксированного такта.

    Опоздание на период и больше не догоняется серией циклов подряд —
    такт перескакивает на ближайшую будущую отметку той же сетки.
    """
    tick = previous_tick + interval
    if tick > now:
        return tick
    missed = math.floor((now - tick) / interval) + 1
    return tick + missed * interval


class ErrorTypes:
    TIMEOUT = "Connection timeout"
    CONNECTION_REFUSED = "Connection refused"
    SSL_ERROR = "SSL certificate error"
    AUTH_ERROR = "Authentication failed"
    SERVER_ERROR = "Server error"
    PROXY_ERROR = "Proxy connection error"
    INVALID_RESPONSE = "Invalid metrics response"
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


@dataclass
class CycleBatch:
    """Итог разбора ответов нод за один цикл — то, что уходит в базу одной транзакцией."""
    snapshots: list[dict] = field(default_factory=list)
    server_updates: list[dict] = field(default_factory=list)
    recovered_ids: list[int] = field(default_factory=list)
    newly_down: list[tuple[int, datetime]] = field(default_factory=list)
    downtime_over: list[int] = field(default_factory=list)
    caps_changed: list[int] = field(default_factory=list)


def select_servers() -> Select:
    """Серверы без last_metrics: ответ ноды коллектор держит в памяти, а из базы
    он тянул бы десятки килобайт на сервер при каждом обходе парка."""
    return select(Server).options(defer(Server.last_metrics))


class PersistSchedule:
    """Когда последний ответ ноды уходит в `servers.last_metrics`.

    Раз в период, и у каждого сервера свой сдвиг внутри периода: отсчёт от
    прошлого сохранения собрал бы весь парк в один цикл — тот же залп в WAL,
    только раз в пять минут. Первый ответ после старта панели сохраняется
    сразу: копия в базе успела устареть на всё время простоя.
    """

    def __init__(self, period_seconds: float, clock: Callable[[], float]) -> None:
        self._period = period_seconds
        self._clock = clock
        self._next_at: dict[int, float] = {}

    def is_due(self, server_id: int) -> bool:
        next_at = self._next_at.get(server_id)
        return next_at is None or self._clock() >= next_at

    def mark_persisted(self, server_ids: Iterable[int]) -> None:
        now = self._clock()
        for server_id in server_ids:
            # Сетка сроков сервера: offset, offset + период, offset + 2 периода…
            offset = server_id % self._period
            periods_passed = math.floor((now - offset) / self._period)
            self._next_at[server_id] = offset + (periods_passed + 1) * self._period


class MetricsCollector:
    """Collects metrics from all servers and stores in panel DB"""
    
    XRAY_CHECK_INTERVAL = 120
    HAPROXY_RETRY_INTERVAL = 30  # как часто досинхронизировать ожившие ноды
    ACTIVE_REFRESH_INTERVAL = 5
    ACTIVITY_TTL = 60
    
    HTTP_CONCURRENCY = 100  # одновременных запросов метрик к нодам
    # Кэш HAProxy и проверка Xray ходят своим лимитом: на общем их обход парка
    # вставал в очередь перед сбором метрик, и тот пропускал такты
    AUX_HTTP_CONCURRENCY = 20
    # Недоступной ноде хватает короткого connect: полный таймаут запроса на каждый
    # из пяти эндпоинтов держал слот больше минуты
    AUX_CONNECT_TIMEOUT = 3.0
    XRAY_PROBE_TIMEOUT = 12.0
    DB_CONCURRENCY = 10    # max parallel DB sessions
    BATCH_YIELD_EVERY = 100   # нод между передачами управления event loop при разборе ответов
    OVERRUN_LOG_EVERY = 30    # затянувшийся цикл логируется раз в столько повторов подряд
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

    # Колонки servers, которые цикл переписывает ответившей ноде. Самого ответа
    # (last_metrics) среди них нет: он добавляется, когда подходит срок PersistSchedule
    STATUS_COLUMNS = ("last_seen", "last_error", "error_code", "node_version", "tracked_ports", "node_capabilities")
    PERSIST_COLUMNS = STATUS_COLUMNS + ("last_metrics",)
    ERROR_COLUMNS = ("last_error", "error_code")

    # Правила файрвола живут под префиксом haproxy, но закрываются своим доменом
    HAPROXY_ENDPOINTS = (
        ("/api/haproxy/status", "status", Capability.HAPROXY),
        ("/api/haproxy/stats", "stats", Capability.HAPROXY),
        ("/api/haproxy/rules", "rules", Capability.HAPROXY),
        ("/api/haproxy/certs/all", "certs", Capability.HAPROXY),
        ("/api/haproxy/firewall/rules", "firewall", Capability.FIREWALL),
    )
    HAPROXY_FETCH_TIMEOUT = 15.0
    HAPROXY_FAST_FETCH_TIMEOUT = 10.0

    # Жёсткий потолок на весь запрос метрик, поверх httpx-таймаутов (2+2+10+2=16s).
    # httpx не покрывает SOCKS5-рукопожатие: запрос через прокси может повиснуть навсегда,
    # а один такой await замораживает gather цикла — вся флотилия уходит в offline (26.07.2026).
    FETCH_HARD_TIMEOUT = 20

    def __init__(self, clock: Callable[[], float] = time.monotonic):
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
        self._aux_http_sem = asyncio.Semaphore(self.AUX_HTTP_CONCURRENCY)
        self._db_sem = asyncio.Semaphore(self.DB_CONCURRENCY)
        self._overrun_streak = 0

        self._node_failures: dict[int, int] = {}
        self._node_skip_cycles: dict[int, int] = {}
        # Момент последнего удачного ответа ноды по monotonic — от него считается окно.
        self._last_success_mono: dict[int, float] = {}

        # Наблюдаемый переход offline → online для авто-восстановления состояния ноды.
        self._down_servers: set[int] = set()
        # Сервера с незакрытым интервалом простоя. Отдельно от _down_servers: интервал
        # мог открыть прошлый запуск панели, и закрыть его должен первый же удачный опрос.
        self._open_downtime: set[int] = set()
        self._reconcile_inflight: set[int] = set()
        self._reconcile_sem = asyncio.Semaphore(self.RECONCILE_CONCURRENCY)

        self._metrics_persist = PersistSchedule(LAST_METRICS_PERSIST_SECONDS, clock)
        # Серверы, чей ответ в памяти новее копии в базе: их досохраняет остановка панели
        self._unsaved_metrics: set[int] = set()

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
        await self._warm_latest_metrics()
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
        await get_load_weights_runner().start()
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
        await get_load_weights_runner().stop()
        # Финальный flush уже после остановки цикла сбора — иначе он гнался бы с observe().
        await get_traffic_ingest().stop()
        # Последним: если остановку оборвут по таймауту, потерять лучше эту запись
        # (копия в базе просто останется старше), чем хвост истории трафика
        await self._persist_unsaved_metrics()
        logger.info("Metrics collector stopped")

    async def _warm_latest_metrics(self) -> None:
        """Поднять хранилище ответов с копии в базе.

        Читателям ответ нужен до первого цикла сбора, а ответ ноды, которая
        сейчас лежит, новый цикл не принесёт вовсе: без копии её адреса выпали
        бы из белых списков всего парка.
        """
        try:
            async with async_session() as db:
                saved = (await db.execute(select(Server.id, Server.last_metrics))).all()
        except Exception as e:
            logger.error(f"Failed to load saved node metrics: {e}")
            return
        get_latest_metrics().load(saved)

    async def _persist_unsaved_metrics(self) -> None:
        """Досохранить ответы, которых ещё нет в базе: с них следующий запуск
        панели поднимет хранилище, пока ноды не ответят заново."""
        answers = get_latest_metrics().get_many(self._unsaved_metrics)
        if not answers:
            return
        # Остановка панели не должна упасть из-за базы: без этой записи копия
        # в базе просто останется старше на несколько минут
        try:
            async with async_session() as db:
                conn = await db.connection()
                await self._update_servers(
                    conn,
                    ("last_metrics",),
                    [{"id": server_id, "last_metrics": json.dumps(metrics)} for server_id, metrics in answers.items()],
                )
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to persist latest metrics of {len(answers)} server(s): {e}")
            return
        self._unsaved_metrics.difference_update(answers)

    async def _collection_loop(self):
        """Main collection loop on a fixed tick.

        Сон до следующей отметки, а не interval после работы: иначе время сбора
        прибавлялось бы к каждому циклу и снапшоты уплывали бы от сетки.
        """
        next_tick = time.monotonic()
        while self._running:
            started = time.monotonic()
            try:
                await self._collect_all_servers()
            except Exception as e:
                logger.error(f"Collection error: {e}")
            self._note_cycle_duration(time.monotonic() - started)

            next_tick = next_collection_tick(next_tick, self._collect_interval, time.monotonic())
            await asyncio.sleep(max(0.0, next_tick - time.monotonic()))

    def _note_cycle_duration(self, duration: float) -> None:
        """Цикл длиннее интервала пропускает такты: на графиках точки идут реже,
        а со стороны это ничем, кроме лога, не видно."""
        if duration <= self._collect_interval:
            self._overrun_streak = 0
            return
        self._overrun_streak += 1
        if self._overrun_streak % self.OVERRUN_LOG_EVERY == 1:
            logger.warning(
                f"Metrics cycle took {duration:.1f}s with a {self._collect_interval}s interval "
                f"({self._overrun_streak} in a row): ticks are being skipped"
            )

    async def _collect_all_servers(self):
        """Collect metrics from all active servers with bounded concurrency."""
        async with async_session() as db:
            result = await db.execute(
                select_servers().where(Server.is_active == True)
            )
            servers = result.scalars().all()

        # Phase 1: fetch metrics from all servers (HTTP-bound, limited by semaphore)
        results = await asyncio.gather(
            *[self._fetch_server_metrics(server) for server in servers],
            return_exceptions=True
        )

        # Phase 2: batch write all results in a single DB session
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        batch = await self._build_cycle_batch(servers, results, now_utc)
        snapshots, server_updates = batch.snapshots, batch.server_updates

        if snapshots:
            get_load_weights_runner().observe(
                {
                    s["server_id"]: NodeLoad(s["net_rx_bytes_per_sec"], s["net_tx_bytes_per_sec"], s["cpu_usage"])
                    for s in snapshots
                },
                self._collect_interval,
            )

        if snapshots or server_updates:
            success_updates = [u for u in server_updates if "last_seen" in u]
            error_updates = [u for u in server_updates if "last_seen" not in u]
            # Сам ответ ноды пишется в базу редко: остальным серверам строка
            # обновляется без него, и ~18 КБ JSON не попадают в WAL каждый цикл
            persist_updates = [u for u in success_updates if "last_metrics" in u]
            status_updates = [u for u in success_updates if "last_metrics" not in u]

            async with async_session() as db:
                if snapshots:
                    await db.execute(MetricsSnapshot.__table__.insert(), snapshots)

                if server_updates:
                    conn = await db.connection()
                    await self._update_servers(conn, self.STATUS_COLUMNS, status_updates)
                    await self._update_servers(conn, self.PERSIST_COLUMNS, persist_updates)
                    await self._update_servers(conn, self.ERROR_COLUMNS, error_updates)

                await db.commit()

            persisted_ids = [u["id"] for u in persist_updates]
            self._metrics_persist.mark_persisted(persisted_ids)
            self._unsaved_metrics.difference_update(persisted_ids)

        if batch.newly_down or batch.downtime_over:
            await self._record_downtime(batch.newly_down, batch.downtime_over, now_utc)

        # Восстановление состояния оживших нод — last_seen уже закоммичен (сервер считается online).
        for sid in batch.recovered_ids:
            asyncio.create_task(self._launch_reconcile(sid))

        # Права поменялись: снимаем долги, ставшие неисполнимыми, и заново
        # раскатываем то, что теперь снова разрешено.
        if batch.caps_changed:
            asyncio.create_task(self._handle_capability_change(batch.caps_changed))

        # Cleanup throttled — only every CLEANUP_INTERVAL seconds
        now = time.time()
        if now - self._last_cleanup >= self.CLEANUP_INTERVAL:
            async with async_session() as db:
                await self._cleanup_old_data(db)
            self._last_cleanup = now

    @staticmethod
    async def _update_servers(conn: AsyncConnection, columns: tuple[str, ...], updates: list[dict]) -> None:
        """Одним executemany переписать названные колонки серверам из `updates`."""
        if not updates:
            return
        # Bulk UPDATE через connection() — обходит ORM bulk-by-PK путь,
        # который требует id внутри values; нам нужен WHERE с bindparam.
        await conn.execute(
            update(Server)
            .where(Server.id == bindparam("sid"))
            .values({column: bindparam(column) for column in columns}),
            [{"sid": upd["id"], **{column: upd[column] for column in columns}} for upd in updates],
        )

    async def _build_cycle_batch(
        self, servers: list[Server], results: list, now_utc: datetime
    ) -> CycleBatch:
        """Разобрать ответы нод в строки для записи.

        Проход по всему парку — синхронный код, поэтому он периодически отдаёт
        управление event loop: иначе API замирал бы на всё время разбора.
        """
        batch = CycleBatch()
        for index, (server, result) in enumerate(zip(servers, results)):
            if index and index % self.BATCH_YIELD_EVERY == 0:
                await asyncio.sleep(0)
            if isinstance(result, Exception):
                continue

            metrics, error_info = result
            if metrics:
                try:
                    self._ingest_metrics(server, metrics, now_utc, batch)
                    continue
                except Exception as e:
                    # Ответ ноды разбирается как есть: без этого один кривой JSON
                    # срывал бы запись снапшотов всему парку каждый цикл
                    logger.warning(f"Malformed metrics from {server.name}: {type(e).__name__}: {e}")
                    error_info = {"message": ErrorTypes.INVALID_RESPONSE, "code": 502}
            if error_info:
                self._ingest_error(server, error_info, now_utc, batch)
        return batch

    def _ingest_metrics(
        self, server: Server, metrics: dict, now_utc: datetime, batch: CycleBatch
    ) -> None:
        # Сначала всё, что может упасть на кривом ответе, и только потом учёт состояния:
        # иначе нода с битыми метриками успела бы отметиться ожившей
        snapshot_data = self._build_snapshot(server.id, metrics, now_utc)
        agent_version = metrics.get("agent_version")
        # Старые ноды этих полей не присылают — пишем обратно то, что уже в базе.
        node_version = agent_version[:self.NODE_VERSION_MAX_LEN] if agent_version else server.node_version
        tracked_ports = self._tracked_ports_json(metrics)
        caps_present, capabilities = self._capabilities_json(metrics)
        # Сериализуется ответ только тем серверам, чей черёд сохраняться в базу
        persisted_answer = json.dumps(metrics) if self._metrics_persist.is_due(server.id) else None
        get_loss_registry().update(server.id, server.name, metrics)
        get_latest_metrics().put(server.id, metrics)
        self._unsaved_metrics.add(server.id)

        # Наблюдаемое оживание: нода молчала дольше порога, теперь снова ответила.
        if server.id in self._down_servers:
            self._down_servers.discard(server.id)
            batch.recovered_ids.append(server.id)
        if server.id in self._open_downtime:
            self._open_downtime.discard(server.id)
            batch.downtime_over.append(server.id)
        if snapshot_data:
            batch.snapshots.append(snapshot_data)
        if not caps_present:
            capabilities = server.node_capabilities
        elif capabilities != server.node_capabilities:
            batch.caps_changed.append(server.id)
        server_update = {
            "id": server.id,
            "last_seen": now_utc,
            "last_error": None,
            "error_code": None,
            "node_version": node_version,
            "tracked_ports": tracked_ports if tracked_ports is not None else server.tracked_ports,
            "node_capabilities": capabilities,
        }
        if persisted_answer is not None:
            server_update["last_metrics"] = persisted_answer
        batch.server_updates.append(server_update)

    def _ingest_error(
        self, server: Server, error_info: dict, now_utc: datetime, batch: CycleBatch
    ) -> None:
        # Помечаем "definitively offline" только если раньше нода была живой и давно молчит.
        if server.id not in self._down_servers and server.last_seen is not None and \
                self._seconds_since(server.last_seen, now_utc) >= self.DOWN_THRESHOLD_SECONDS:
            self._down_servers.add(server.id)
            if server.id not in self._open_downtime:
                self._open_downtime.add(server.id)
                batch.newly_down.append((server.id, self._to_naive_utc(server.last_seen)))
        batch.server_updates.append({
            "id": server.id,
            "last_error": error_info["message"],
            "error_code": error_info["code"],
        })

    def _answered_last_poll(self, server_id: int) -> bool:
        """Ответила ли нода на последний запрос метрик — иначе вспомогательные
        обходы только собрали бы на ней таймауты."""
        return self._node_failures.get(server_id, 0) == 0

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

    def _window_seconds(self, server_id: int) -> int:
        """Окно усреднения для ноды — секунды с её прошлого удачного опроса."""
        last_success = self._last_success_mono.get(server_id)
        elapsed = None if last_success is None else time.monotonic() - last_success
        return poll_window_seconds(elapsed, self._collect_interval)

    async def _fetch_metrics(self, server: Server) -> tuple[Optional[dict], Optional[dict]]:
        """Fetch current metrics from server API. Returns (metrics, error_info)"""
        url = f"{server.url}/api/metrics"

        try:
            client = get_node_client(server)
            # Таймаут берётся из клиента (_NODE_TIMEOUT = 2/5/2/2) — без лишних аллокаций.
            response = await client.get(
                url, headers=node_auth_headers(server), params={"window": self._window_seconds(server.id)}
            )
            if response.status_code == 200:
                data = response.json()
                self._last_success_mono[server.id] = time.monotonic()
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

    @staticmethod
    def _capabilities_json(metrics: dict) -> tuple[bool, Optional[str]]:
        """(нода прислала поле, карта прав). Отсутствие поля — старый агент.

        Отличать «поля нет» от «в поле null» обязательно: иначе снятое на ноде
        ограничение осталось бы в базе навсегда.
        """
        if "capabilities" not in metrics:
            return False, None
        return True, normalize_capabilities_map(metrics.get("capabilities"))

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

    async def _handle_capability_change(self, server_ids: list[int]):
        """Права ноды изменились: чистим очередь и досинхронизируем открывшееся.

        Реконсилер идемпотентен и дедуплицирован, поэтому годится для обеих
        сторон изменения: и когда раздел закрыли, и когда открыли обратно.
        """
        try:
            from app.services.node_sync_queue import drop_forbidden
            await drop_forbidden(server_ids)
        except Exception as e:
            logger.error(f"Failed to drop forbidden sync debts for {server_ids}: {e}")

        for server_id in server_ids:
            await self._launch_reconcile(server_id)

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

        # Учёт трафика наблюдает счётчики всегда — это история байт, не скорость.
        # Ответ ноды приходит сюда как есть, а снапшоты всей флотилии собираются
        # одним проходом — исключение на одной ноде не должно ронять остальные.
        try:
            speeds = get_traffic_ingest().observe(server_id, metrics, current_time)
            net_rx_speed, net_tx_speed = speeds.rx_bytes_per_sec, speeds.tx_bytes_per_sec
        except Exception as e:
            logger.warning(f"Traffic observe failed for server {server_id}: {e}")
            net_rx_speed, net_tx_speed = 0.0, 0.0

        # Дельты дисковых счётчиков считаются всегда: база должна быть тёплой
        # к моменту, когда семплер ноды замолчит и придётся на них откатиться.
        disk_read = sum(d.get("read_bytes", 0) for d in disk.get("io", {}).values())
        disk_write = sum(d.get("write_bytes", 0) for d in disk.get("io", {}).values())
        disk_read_speed, disk_write_speed = self._disk_speeds(
            server_id, disk_read, disk_write, current_time
        )
        rates = snapshot_rates(
            metrics,
            NodeRates(
                net_rx=net_rx_speed, net_tx=net_tx_speed,
                disk_read=disk_read_speed, disk_write=disk_write_speed,
            ),
        )

        disk_percent = 0.0
        partitions = disk.get("partitions", [])
        if partitions:
            disk_percent = partitions[0].get("percent", 0)
        
        connections = system.get("connections", {})
        connections_count = connections.get("established", 0) + connections.get("listen", 0)
        connections_detailed = system.get("connections_detailed", {})
        tcp_states = connections_detailed.get("tcp", {})

        return {
            "server_id": server_id,
            "timestamp": now_utc,
            "cpu_usage": rates.cpu_usage,
            "cpu_usage_max": rates.cpu_usage_max,
            "cpu_steal": rates.cpu_steal,
            "load_avg_1": cpu.get("load_avg_1", 0),
            "load_avg_5": cpu.get("load_avg_5", 0),
            "load_avg_15": cpu.get("load_avg_15", 0),
            "memory_total": memory.get("total", 0),
            "memory_used": memory.get("used", 0),
            "memory_available": memory.get("available", 0),
            "memory_percent": memory.get("percent", 0),
            "swap_used": swap.get("used", 0),
            "swap_percent": swap.get("percent", 0),
            "net_rx_bytes_per_sec": rates.net_rx,
            "net_tx_bytes_per_sec": rates.net_tx,
            "net_rx_bytes_per_sec_max": rates.net_rx_max,
            "net_tx_bytes_per_sec_max": rates.net_tx_max,
            "disk_percent": disk_percent,
            "disk_read_bytes_per_sec": rates.disk_read,
            "disk_write_bytes_per_sec": rates.disk_write,
            "process_count": processes.get("total", 0),
            "connections_count": connections_count,
            "tcp_established": tcp_states.get("established"),
            "tcp_listen": tcp_states.get("listen"),
            "tcp_time_wait": tcp_states.get("time_wait"),
            "tcp_close_wait": tcp_states.get("close_wait"),
            "tcp_syn_sent": tcp_states.get("syn_sent"),
            "tcp_syn_recv": tcp_states.get("syn_recv") if tcp_states.get("syn_recv") is not None else tcp_states.get("syn_received"),
            "tcp_fin_wait": tcp_states.get("fin_wait"),
            "per_cpu_percent": json.dumps(rates.per_cpu_percent) if rates.per_cpu_percent else None,
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
                        select_servers().where(Server.id.in_(active_ids), Server.is_active == True)
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
                select_servers().where(Server.is_active == True)
            )
            servers = result.scalars().all()

        # Each server uses its own session
        tasks = [self._cache_server_haproxy(server, self.HAPROXY_FETCH_TIMEOUT) for server in servers]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _cache_server_haproxy(self, server: Server, timeout: float):
        """Cache HAProxy data for a single server into server_cache table."""
        # Отбор до создания клиента: у ноды с закрытым haproxy этот цикл иначе
        # каждые 5 минут собирал бы четыре гарантированных отказа
        endpoints = [
            (endpoint, key) for endpoint, key, capability in self.HAPROXY_ENDPOINTS
            if server_allows(server, capability, write=False)
        ]
        if not endpoints or not self._answered_last_poll(server.id):
            return

        try:
            async with self._aux_http_sem:
                client = get_node_client(server)
                headers = node_auth_headers(server)
                request_timeout = httpx.Timeout(timeout, connect=self.AUX_CONNECT_TIMEOUT)
                haproxy_data = {}

                for endpoint, key in endpoints:
                    try:
                        res = await client.get(
                            f"{server.url}{endpoint}", headers=headers, timeout=request_timeout
                        )
                        if res.status_code == 200:
                            haproxy_data[key] = res.json()
                    except httpx.TransportError:
                        # Нода не отвечает — остальные эндпоинты дали бы те же таймауты
                        break
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
    
    async def _probe_xray(self, server: Server, install_path: str) -> tuple[int, bool, bool]:
        """(id, запущен ли remnanode, найдена ли установка Remnawave nginx)."""
        # Нода закрыла раздел или не ответила на метрики — оставляем как есть: обнулив,
        # панель погасила бы бейджи Remnawave, будто ноды с ним и не было
        if not server_allows(server, Capability.REMNAWAVE, write=False) \
                or not self._answered_last_poll(server.id):
            return server.id, server.has_xray_node, server.remnawave_nginx_detected

        request_timeout = httpx.Timeout(self.XRAY_PROBE_TIMEOUT, connect=self.AUX_CONNECT_TIMEOUT)
        async with self._aux_http_sem:
            available = False
            detected = False
            try:
                client = get_node_client(server)
                resp = await client.get(
                    f"{server.url}/api/remnawave/status",
                    headers=node_auth_headers(server),
                    timeout=request_timeout,
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
                    timeout=request_timeout,
                )
                if resp.status_code == 200:
                    detected = bool(resp.json().get("found", False))
            except Exception:
                pass
        return server.id, available, detected

    async def _check_xray_on_all_servers(self):
        """Check xray availability on every active server, batch update
        has_xray_node + remnawave_nginx_detected (обнаружение установки Remnawave)."""
        from app.services.remnawave_nginx_sync import get_remnawave_nginx_path

        async with async_session() as db:
            result = await db.execute(
                select_servers().where(Server.is_active == True)
            )
            servers = result.scalars().all()
            install_path = await get_remnawave_nginx_path(db)

        if not servers:
            return

        results = await asyncio.gather(
            *[self._probe_xray(s, install_path) for s in servers], return_exceptions=True
        )

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

        Пики берутся из окна ноды (`*_max`), а у строк без пика — из самого
        среднего: старая нода или снапшот до миграции не должны обнулять максимум.
        """
        from sqlalchemy import text

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        hour_end = now.replace(minute=0, second=0, microsecond=0)
        hour_start = hour_end - timedelta(hours=1)

        await db.execute(text("""
            INSERT INTO aggregated_metrics (
                server_id, timestamp, period_type,
                avg_cpu, max_cpu, avg_cpu_steal, avg_load, max_load,
                avg_memory_percent, max_memory_percent, avg_disk_percent,
                total_rx_bytes, total_tx_bytes,
                avg_rx_speed, avg_tx_speed, max_rx_speed, max_tx_speed,
                avg_disk_read_speed, avg_disk_write_speed,
                avg_tcp_established, avg_tcp_listen, avg_tcp_time_wait,
                avg_tcp_close_wait, avg_tcp_syn_sent, avg_tcp_syn_recv,
                avg_tcp_fin_wait, data_points
            )
            SELECT
                m.server_id, :hour_start, 'hour',
                AVG(m.cpu_usage), MAX(COALESCE(m.cpu_usage_max, m.cpu_usage)), AVG(m.cpu_steal),
                AVG(m.load_avg_1), MAX(m.load_avg_1),
                AVG(m.memory_percent), MAX(m.memory_percent), AVG(m.disk_percent),
                COALESCE(MAX(t.rx_bytes), 0), COALESCE(MAX(t.tx_bytes), 0),
                AVG(m.net_rx_bytes_per_sec), AVG(m.net_tx_bytes_per_sec),
                MAX(COALESCE(m.net_rx_bytes_per_sec_max, m.net_rx_bytes_per_sec)),
                MAX(COALESCE(m.net_tx_bytes_per_sec_max, m.net_tx_bytes_per_sec)),
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
                avg_cpu, max_cpu, avg_cpu_steal, avg_load, max_load,
                avg_memory_percent, max_memory_percent, avg_disk_percent,
                total_rx_bytes, total_tx_bytes,
                avg_rx_speed, avg_tx_speed, max_rx_speed, max_tx_speed,
                avg_disk_read_speed, avg_disk_write_speed,
                avg_tcp_established, avg_tcp_listen, avg_tcp_time_wait,
                avg_tcp_close_wait, avg_tcp_syn_sent, avg_tcp_syn_recv,
                avg_tcp_fin_wait, data_points
            )
            SELECT
                server_id, :day_start, 'day',
                AVG(avg_cpu), MAX(max_cpu), AVG(avg_cpu_steal), AVG(avg_load), MAX(COALESCE(max_load, avg_load)),
                AVG(avg_memory_percent), MAX(max_memory_percent), AVG(avg_disk_percent),
                COALESCE(SUM(total_rx_bytes), 0), COALESCE(SUM(total_tx_bytes), 0),
                AVG(avg_rx_speed), AVG(avg_tx_speed),
                MAX(COALESCE(max_rx_speed, avg_rx_speed)), MAX(COALESCE(max_tx_speed, avg_tx_speed)),
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
