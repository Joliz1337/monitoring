"""Одноразовый перенос истории трафика с нод в PostgreSQL.

Начиная с 10.13.0 нода не ведёт историю сама — отдаёт только сырые счётчики.
На уже работающих серверах остался SQLite с накопленными сутками: сервис
забирает его по одному разу на ноду, раскладывает в server_traffic как
source=legacy и просит ноду стереть файл. Purge вызывается строго после коммита
импорта, поэтому при любом порядке обновления панели и нод история не теряется.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Optional

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from app.database import async_session
from app.models import Server, ServerTraffic, TrafficImportState
from app.services.http_client import get_node_client, node_auth_headers
from app.services.node_capabilities import Capability, server_allows

logger = logging.getLogger(__name__)

MIN_NODE_VERSION_FOR_TRAFFIC_V2 = "10.13.0"

SCAN_INTERVAL_SECONDS = 300
STARTUP_DELAY_SECONDS = 90       # дать коллектору проставить Server.node_version
SERVERS_PER_CYCLE = 5
EXPORT_MAX_DAYS = 400
INSERT_CHUNK_ROWS = 1000
SCOPE_KEY_MAX_LEN = 32           # ширина server_traffic.scope_key

# Захваченная строка помечается будущим next_attempt_at: если панель умрёт на
# середине переноса, следующий цикл подберёт сервер сам, без ручного сброса.
CLAIM_LEASE = timedelta(minutes=15)
NODE_TOO_OLD_RETRY = timedelta(minutes=30)
# Обрезанную выгрузку добираем раз в сутки: нода отдаёт окно в EXPORT_MAX_DAYS
# от сегодняшнего дня, и с каждым днём из него выпадает самый старый день —
# хвост, не поместившийся в лимит строк, постепенно влезает целиком.
TRUNCATED_RETRY = timedelta(hours=24)
BACKOFF_FIRST = timedelta(minutes=5)
BACKOFF_MAX = timedelta(hours=6)
BACKOFF_MAX_DOUBLINGS = 8

# Виртуальные интерфейсы, чей трафик уже посчитан на физических. Список тот же,
# что у ноды (VIRTUAL_IFACE_PREFIXES в node/app/services/metrics_collector.py):
# легаси-итоги считались вместе с docker0/veth*/br-*, и без фильтра
# импортированный total систематически превышал бы живой.
VIRTUAL_IFACE_PREFIXES = (
    "veth", "docker", "br-", "virbr", "flannel", "cni", "cali",
    "wg", "tun", "tap", "warp", "gre", "sit", "ip6tnl",
)

_EXPORT_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)
_PURGE_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
# Потолок поверх httpx-таймаутов: SOCKS5-рукопожатие ими не покрыто и способно
# повиснуть навсегда, утащив за собой весь цикл.
HARD_TIMEOUT_SECONDS = 90


class ImportStatus(str, Enum):
    PENDING = "pending"
    NODE_TOO_OLD = "node_too_old"
    NODE_DENIED = "node_denied"
    IMPORTED = "imported"
    PURGED = "purged"
    EMPTY = "empty"
    FAILED = "failed"


# Что ещё предстоит доделать: pending/failed — забрать выгрузку, node_too_old —
# дождаться обновления ноды, imported — добить purge (или дозабрать обрезанный
# хвост). purged/empty терминальны и в выборку не попадают.
RETRY_STATUSES = (
    ImportStatus.PENDING.value,
    ImportStatus.FAILED.value,
    ImportStatus.NODE_TOO_OLD.value,
    ImportStatus.NODE_DENIED.value,
    ImportStatus.IMPORTED.value,
)


class LegacyExportError(Exception):
    """Нода не отдала легаси-выгрузку."""


class LegacyPurgeError(Exception):
    """Нода не подтвердила удаление легаси-хранилища."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _version_tuple(value: str) -> tuple[int, ...]:
    """Версию в кортеж для сравнения; мусор и пустое сортируются ниже всех.

    Зеркалит парсер версий ноды из routers/system.py — сервис не тянет роутер
    ради шести строк.
    """
    parts = []
    for chunk in (value or "").strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def node_supports_traffic_v2(node_version: Optional[str]) -> bool:
    if not node_version:
        return False
    return _version_tuple(node_version) >= _version_tuple(MIN_NODE_VERSION_FOR_TRAFFIC_V2)


def is_virtual_interface(name: str) -> bool:
    return name.startswith(VIRTUAL_IFACE_PREFIXES)


def _parse_day(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day)


def _parse_iso_utc(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _to_bytes(value: object) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _as_port(value: object) -> Optional[int]:
    try:
        port = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return port if 0 < port <= 65535 else None


def _traffic_row(server_id: int, scope: str, scope_key: str, bucket: datetime, rx: int, tx: int) -> dict:
    return {
        "server_id": server_id,
        "period_type": "day",
        "scope": scope,
        "scope_key": scope_key,
        "bucket": bucket,
        "rx_bytes": rx,
        "tx_bytes": tx,
        # Легаси-строка — суточный итог без окна измерений: сколько секунд суток
        # реально покрыто наблюдениями, из неё не восстановить.
        "covered_seconds": 0,
        "source": "legacy",
    }


def _accumulate(store: dict, key: tuple, rx: int, tx: int) -> None:
    slot = store.setdefault(key, [0, 0])
    slot[0] += rx
    slot[1] += tx


def build_traffic_rows(server_id: int, daily: list) -> list[dict]:
    """Дневные строки выгрузки → строки server_traffic.

    Чистая функция без I/O: интерфейсы и порты переносятся один в один, total
    пересобирается заново как сумма по невиртуальным интерфейсам.
    """
    scoped: dict[tuple[str, str, datetime], list[int]] = {}
    totals: dict[datetime, list[int]] = {}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = datetime(now.year, now.month, now.day)

    for entry in daily:
        if not isinstance(entry, dict):
            continue
        bucket = _parse_day(entry.get("date"))
        if bucket is None or bucket >= today:
            # Текущие сутки пропускаем: живой учёт уже пишет в этот же бакет и складывает
            # байты через ON CONFLICT DO UPDATE, так что перекрывающаяся часть дня
            # посчиталась бы дважды.
            continue

        rx = _to_bytes(entry.get("rx_bytes"))
        tx = _to_bytes(entry.get("tx_bytes"))
        interface = str(entry.get("interface") or "").strip()
        port = _as_port(entry.get("port"))

        if interface and port is None:
            if len(interface) > SCOPE_KEY_MAX_LEN:
                continue
            _accumulate(scoped, ("iface", interface, bucket), rx, tx)
            if not is_virtual_interface(interface):
                total = totals.setdefault(bucket, [0, 0])
                total[0] += rx
                total[1] += tx
        elif port is not None and not interface:
            _accumulate(scoped, ("port", str(port), bucket), rx, tx)

    # Схлопывать обязательно: строки с одинаковым ключом конфликта уходят одним
    # INSERT ... ON CONFLICT DO NOTHING, где вторая молча отбрасывается PostgreSQL —
    # интерфейсы разошлись бы с total, который суммируется здесь же. Выгрузка ноды
    # группирует средствами SQLite, а там '443' и 443 — разные ключи группировки.
    rows = [
        _traffic_row(server_id, scope, scope_key, bucket, rx, tx)
        for (scope, scope_key, bucket), (rx, tx) in scoped.items()
    ]
    rows.extend(
        _traffic_row(server_id, "total", "", bucket, rx, tx)
        for bucket, (rx, tx) in totals.items()
    )
    return rows


def _backoff(attempts: int) -> timedelta:
    """5 минут, дальше удвоение до 6 часов. Попытки не прекращаются никогда —
    нода может ожить и через месяц."""
    doublings = min(max(attempts, 1) - 1, BACKOFF_MAX_DOUBLINGS)
    return min(BACKOFF_FIRST * (2 ** doublings), BACKOFF_MAX)


class TrafficImporter:
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Legacy traffic importer started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Legacy traffic importer stopped")

    async def _loop(self) -> None:
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        while self._running:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"Legacy traffic import cycle failed: {e}")
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    async def run_cycle(self) -> int:
        await self._ensure_state_rows()
        claimed = await self._claim_servers()
        if not claimed:
            return 0
        await asyncio.gather(*(self._process(server) for server in claimed), return_exceptions=True)
        return len(claimed)

    # ── выбор кандидатов ───────────────────────────────────────────────────

    async def _ensure_state_rows(self) -> None:
        async with async_session() as db:
            stmt = pg_insert(TrafficImportState).from_select(
                ["server_id"],
                select(Server.id).where(Server.is_active.is_(True)),
            ).on_conflict_do_nothing(index_elements=["server_id"])
            await db.execute(stmt)
            await db.commit()

    async def _claim_servers(self) -> list[Server]:
        """Захватить пачку серверов под FOR UPDATE SKIP LOCKED и сразу отпустить
        соединение: сеть к ноде идёт уже без удерживаемых блокировок."""
        now = _utcnow()
        async with async_session() as db:
            result = await db.execute(
                select(TrafficImportState, Server)
                .join(Server, Server.id == TrafficImportState.server_id)
                .where(
                    Server.is_active.is_(True),
                    TrafficImportState.status.in_(RETRY_STATUSES),
                    or_(
                        TrafficImportState.next_attempt_at.is_(None),
                        TrafficImportState.next_attempt_at <= now,
                    ),
                )
                .order_by(TrafficImportState.server_id)
                .limit(SERVERS_PER_CYCLE)
                .with_for_update(of=TrafficImportState, skip_locked=True)
            )
            claimed = []
            for state, server in result.all():
                claimed.append(server)
                state.next_attempt_at = now + CLAIM_LEASE
            await db.commit()
        return claimed

    # ── перенос одной ноды ─────────────────────────────────────────────────

    async def _process(self, server: Server) -> None:
        try:
            await self._import_and_purge(server)
        except Exception as e:
            # Граница фоновой задачи: неучтённый сбой не должен уносить весь цикл
            logger.error(f"Legacy traffic import for {server.name} crashed: {e}")
            await self._mark_failed(server, f"unexpected: {e}")

    async def _import_and_purge(self, server: Server) -> None:
        node_version = server.node_version
        if not server_allows(server, Capability.TRAFFIC, write=False):
            # Раздел закрыт на самой ноде: ждём не обновления, а решения владельца
            await self._save_state(
                server.id,
                status=ImportStatus.NODE_DENIED.value,
                node_version=node_version,
                last_error=None,
                next_attempt_at=_utcnow() + NODE_TOO_OLD_RETRY,
            )
            return

        if not node_supports_traffic_v2(node_version):
            # Нода просто ещё не обновилась — штатное ожидание, не ошибка.
            logger.debug(f"Legacy traffic import postponed: {server.name} runs {node_version or 'unknown'}")
            await self._save_state(
                server.id,
                status=ImportStatus.NODE_TOO_OLD.value,
                node_version=node_version,
                last_error=None,
                next_attempt_at=_utcnow() + NODE_TOO_OLD_RETRY,
            )
            return

        try:
            export = await self._fetch_export(server)
        except LegacyExportError as e:
            await self._mark_failed(server, f"export: {e}")
            return

        if not export.get("available"):
            await self._save_unavailable(server, export, node_version)
            return

        rows = build_traffic_rows(server.id, export.get("daily") or [])
        try:
            await self._store_rows(rows)
        except SQLAlchemyError as e:
            await self._mark_failed(server, f"insert: {e}")
            return

        truncated = bool(export.get("truncated"))
        await self._save_state(
            server.id,
            status=ImportStatus.IMPORTED.value,
            node_version=node_version,
            fingerprint=str(export.get("fingerprint") or "")[:64] or None,
            rows_imported=len(rows),
            imported_at=_utcnow(),
            last_error="export truncated by node row limit" if truncated else None,
            # Строка остаётся в выборке: не пройдёт purge — повторится он один,
            # импорт уже закоммичен и второй раз строк не удвоит.
            next_attempt_at=_utcnow() + (TRUNCATED_RETRY if truncated else BACKOFF_FIRST),
        )
        logger.info(f"Legacy traffic imported from {server.name}: {len(rows)} rows")

        if truncated:
            # Нода отдаёт дни от старых к новым и режет по лимиту строк, значит
            # за бортом остались самые свежие сутки — стирать её БД пока нельзя.
            logger.warning(f"Legacy traffic export from {server.name} truncated, purge postponed")
            return

        await self._finish_purge(server)

    async def _save_unavailable(self, server: Server, export: dict, node_version: Optional[str]) -> None:
        purged_at = _parse_iso_utc(export.get("purged_at"))
        if purged_at is not None:
            await self._save_state(
                server.id,
                status=ImportStatus.PURGED.value,
                node_version=node_version,
                purged_at=purged_at,
                last_error=None,
                next_attempt_at=None,
            )
            return

        reason = str(export.get("reason") or "")
        if reason != "no_db":
            await self._mark_failed(server, f"export unavailable: {reason or 'unknown reason'}")
            return

        await self._save_state(
            server.id,
            status=ImportStatus.EMPTY.value,
            node_version=node_version,
            last_error=None,
            next_attempt_at=None,
        )

    async def _finish_purge(self, server: Server) -> None:
        # Чтение разрешено, а запись нет: историю мы забрали, удалить копию на
        # ноде не можем. Считаем работу законченной, иначе она повторялась бы
        # каждые полчаса до конца времён.
        if not server_allows(server, Capability.TRAFFIC, write=True):
            await self._save_state(
                server.id,
                status=ImportStatus.IMPORTED.value,
                last_error=None,
                next_attempt_at=None,
            )
            return

        try:
            await self._purge_node(server)
        except LegacyPurgeError as e:
            # Импорт уже закоммичен, поэтому повтор безопасен: он пройдёт выгрузку
            # заново (строки не удвоятся) и снова дойдёт до purge.
            await self._mark_failed(server, f"purge: {e}")
            return

        await self._save_state(
            server.id,
            status=ImportStatus.PURGED.value,
            purged_at=_utcnow(),
            last_error=None,
            next_attempt_at=None,
        )

    # ── ввод-вывод ─────────────────────────────────────────────────────────

    async def _fetch_export(self, server: Server) -> dict:
        try:
            response = await asyncio.wait_for(
                get_node_client(server).get(
                    f"{server.url}/api/traffic/legacy/export",
                    headers=node_auth_headers(server),
                    params={"max_days": EXPORT_MAX_DAYS},
                    timeout=_EXPORT_TIMEOUT,
                ),
                timeout=HARD_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as e:
            raise LegacyExportError("hard timeout") from e
        except httpx.HTTPError as e:
            # У оборвавшегося SOCKS-туннеля str(e) бывает пустой — имя класса
            # хотя бы называет причину.
            raise LegacyExportError(str(e) or type(e).__name__) from e

        if response.status_code != 200:
            raise LegacyExportError(f"HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as e:
            raise LegacyExportError("malformed JSON") from e
        if not isinstance(body, dict):
            raise LegacyExportError("unexpected payload")
        return body

    async def _purge_node(self, server: Server) -> None:
        try:
            response = await asyncio.wait_for(
                get_node_client(server).post(
                    f"{server.url}/api/traffic/legacy/purge",
                    headers=node_auth_headers(server),
                    timeout=_PURGE_TIMEOUT,
                ),
                timeout=HARD_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as e:
            raise LegacyPurgeError("hard timeout") from e
        except httpx.HTTPError as e:
            raise LegacyPurgeError(str(e) or type(e).__name__) from e

        if response.status_code != 200:
            raise LegacyPurgeError(f"HTTP {response.status_code}")

    async def _store_rows(self, rows: list[dict]) -> None:
        """Одна транзакция на весь импорт: либо сутки ноды легли целиком, либо
        не легло ничего и purge не будет вызван."""
        if not rows:
            return
        async with async_session() as db:
            for start in range(0, len(rows), INSERT_CHUNK_ROWS):
                stmt = pg_insert(ServerTraffic).values(rows[start:start + INSERT_CHUNK_ROWS])
                # Живые данные за этот день легаси не затирает.
                await db.execute(stmt.on_conflict_do_nothing(
                    index_elements=["server_id", "period_type", "scope", "bucket", "scope_key"],
                ))
            await db.commit()

    # ── состояние переноса ─────────────────────────────────────────────────

    async def _save_state(self, server_id: int, **fields) -> None:
        async with async_session() as db:
            await db.execute(
                update(TrafficImportState)
                .where(TrafficImportState.server_id == server_id)
                .values(**fields)
            )
            await db.commit()

    async def _mark_failed(self, server: Server, error: str) -> None:
        async with async_session() as db:
            state = await db.get(TrafficImportState, server.id)
            if state is None:
                return
            state.attempts = (state.attempts or 0) + 1
            state.status = ImportStatus.FAILED.value
            state.last_error = error[:500]
            state.next_attempt_at = _utcnow() + _backoff(state.attempts)
            attempts = state.attempts
            await db.commit()
        logger.warning(f"Legacy traffic import from {server.name} failed (attempt {attempts}): {error}")


_importer: Optional[TrafficImporter] = None


def get_traffic_importer() -> TrafficImporter:
    global _importer
    if _importer is None:
        _importer = TrafficImporter()
    return _importer


async def start_traffic_import() -> None:
    await get_traffic_importer().start()


async def stop_traffic_import() -> None:
    await get_traffic_importer().stop()
