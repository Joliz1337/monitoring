"""Доступ к легаси-хранилищу трафика ноды (SQLite) строго на чтение.

История трафика ведётся панелью в PostgreSQL, старая БД остаётся на ноде только
для одноразового экспорта дневных итогов и последующего удаления по команде
панели. Сама нода не удаляет её ни при каких обстоятельствах — иначе стал бы
важен порядок обновления: обновившаяся первой нода стёрла бы историю, которую
панель ещё не успела забрать.
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from app.config import get_settings

logger = logging.getLogger(__name__)

MAX_EXPORT_DAYS = 400
MAX_EXPORT_ROWS = 50_000
SQLITE_TIMEOUT_SEC = 10

DATE_FORMAT = "%Y-%m-%d"
HOUR_FORMAT = "%Y-%m-%d %H:00"
MONTH_FORMAT = "%Y-%m"

TOMBSTONE_NAME = "legacy_purged.json"
STATE_NAME = "traffic_state.json"
DB_SIDECAR_SUFFIXES = ("-wal", "-shm")

# Колонка бакета → таблица легаси-БД. Оба имени подставляются прямо в SQL,
# поэтому берутся только отсюда и никогда из параметров запроса.
BUCKET_TABLES = {
    "hour": "hourly_traffic",
    "date": "daily_traffic",
    "month": "monthly_traffic",
}

# SUM + GROUP BY обязательны: в старом коллекторе UPSERT не срабатывал никогда
# (NULL в UNIQUE-ключе не конфликтует), и на каждый цикл опроса в daily_traffic
# ложилась новая строка вместо инкремента существующей. Построчное чтение отдало
# бы тысячи мусорных точек вместо дневных итогов.
EXPORT_SQL = """
    SELECT date, interface, port, SUM(rx_bytes), SUM(tx_bytes)
    FROM daily_traffic
    WHERE date >= ?
    GROUP BY date, interface, port
    ORDER BY date
    LIMIT ?
"""

T = TypeVar("T")


class LegacyStoreUnreadableError(Exception):
    """Файл легаси-БД на месте, но прочитать его не удалось."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LegacyStorePurgeError(Exception):
    """Не удалось удалить файлы легаси-хранилища."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _fetch(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    """Отсутствие таблицы — это «данных нет», а не сбой.

    БД разных поколений ноды содержат разный набор таблиц, и витрине проще
    отдать пустой набор, чем разбираться, какая именно версия схемы на диске.
    """
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return []
        raise


class LegacyTrafficStore:
    """Read-only витрина старой SQLite плюс её удаление по команде панели."""

    def __init__(self):
        db_path = Path(get_settings().traffic_db_path)
        self._db_path = db_path
        self._sidecars = [db_path.with_name(db_path.name + suffix) for suffix in DB_SIDECAR_SUFFIXES]
        self._state_path = db_path.parent / STATE_NAME
        self._tombstone_path = db_path.parent / TOMBSTONE_NAME

    def is_available(self) -> bool:
        return self._db_path.exists() and not self._tombstone_path.exists()

    def purged_at(self) -> Optional[str]:
        try:
            marker = json.loads(self._tombstone_path.read_text())
        except (OSError, ValueError):
            return None
        stamp = marker.get("purged_at")
        return stamp if isinstance(stamp, str) else None

    async def export(self, max_days: int) -> dict[str, Any]:
        """Дневные итоги для импорта в панель."""
        if self._tombstone_path.exists():
            return {"available": False, "purged_at": self.purged_at()}
        if not self._db_path.exists():
            return {"available": False, "reason": "no_db"}

        window_days = max(1, min(max_days, MAX_EXPORT_DAYS))
        return await asyncio.to_thread(self._export_sync, window_days)

    async def purge(self) -> dict[str, Any]:
        """Удалить БД и служебные файлы, оставив тумбстоун. Повторный вызов — no-op."""
        return await asyncio.to_thread(self._purge_sync)

    async def hourly(
        self, hours: int, interface: Optional[str] = None, port: Optional[int] = None
    ) -> list[dict]:
        cutoff = (_utc_now() - timedelta(hours=hours)).strftime(HOUR_FORMAT)
        rows = await self._bucket_rows("hour", cutoff, interface, port)
        return [{"hour": r[0], "rx_bytes": r[1], "tx_bytes": r[2]} for r in rows]

    async def daily(
        self, days: int, interface: Optional[str] = None, port: Optional[int] = None
    ) -> list[dict]:
        cutoff = (_utc_now() - timedelta(days=days)).strftime(DATE_FORMAT)
        rows = await self._bucket_rows("date", cutoff, interface, port)
        return [{"date": r[0], "rx_bytes": r[1], "tx_bytes": r[2]} for r in rows]

    async def monthly(
        self, months: int, interface: Optional[str] = None, port: Optional[int] = None
    ) -> list[dict]:
        cutoff = (_utc_now() - timedelta(days=months * 30)).strftime(MONTH_FORMAT)
        rows = await self._bucket_rows("month", cutoff, interface, port)
        return [{"month": r[0], "rx_bytes": r[1], "tx_bytes": r[2]} for r in rows]

    async def totals(self, days: int) -> dict[str, Any]:
        rows = await self._daily_rollup(
            "SELECT SUM(rx_bytes), SUM(tx_bytes) FROM daily_traffic "
            "WHERE date >= ? AND port IS NULL",
            days,
        )
        rx, tx = rows[0] if rows else (0, 0)
        return {"rx_bytes": rx or 0, "tx_bytes": tx or 0, "days": days}

    async def interface_summary(self, days: int) -> list[dict]:
        rows = await self._daily_rollup(
            "SELECT interface, SUM(rx_bytes), SUM(tx_bytes) FROM daily_traffic "
            "WHERE date >= ? AND interface IS NOT NULL AND port IS NULL "
            "GROUP BY interface ORDER BY interface",
            days,
        )
        return [{"interface": r[0], "rx_bytes": r[1], "tx_bytes": r[2]} for r in rows]

    async def port_summary(self, days: int) -> list[dict]:
        rows = await self._daily_rollup(
            "SELECT port, SUM(rx_bytes), SUM(tx_bytes) FROM daily_traffic "
            "WHERE date >= ? AND port IS NOT NULL "
            "GROUP BY port ORDER BY port",
            days,
        )
        return [{"port": r[0], "rx_bytes": r[1], "tx_bytes": r[2]} for r in rows]

    async def _bucket_rows(
        self, bucket: str, cutoff: str, interface: Optional[str], port: Optional[int]
    ) -> list[tuple]:
        table = BUCKET_TABLES[bucket]
        head = f"SELECT {bucket}, SUM(rx_bytes), SUM(tx_bytes) FROM {table} WHERE {bucket} >= ?"
        tail = f" GROUP BY {bucket} ORDER BY {bucket}"

        if interface:
            return await self._view_rows(f"{head} AND interface = ? AND port IS NULL{tail}", (cutoff, interface))
        if port:
            return await self._view_rows(f"{head} AND port = ?{tail}", (cutoff, port))
        return await self._view_rows(f"{head} AND port IS NULL{tail}", (cutoff,))

    async def _daily_rollup(self, sql: str, days: int) -> list[tuple]:
        cutoff = (_utc_now() - timedelta(days=days)).strftime(DATE_FORMAT)
        return await self._view_rows(sql, (cutoff,))

    async def _view_rows(self, sql: str, params: tuple) -> list[tuple]:
        """Чтение для замороженных витрин: любая недоступность БД — пустой набор.

        Старая панель уходит в фолбэк на кэш только по 5xx, поэтому ошибку сюда
        поднимать нельзя — страница трафика покраснела бы вместо пустых графиков.
        """
        if not self.is_available():
            return []
        try:
            return await asyncio.to_thread(self._read, lambda conn: _fetch(conn, sql, params))
        except LegacyStoreUnreadableError as e:
            logger.warning(f"Легаси-БД трафика не читается: {e.reason}")
            return []

    def _read(self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Открыть БД строго на чтение и выполнить запросы одним подключением.

        Резервная попытка с `immutable=1` нужна для БД, оставшейся с включённым
        WAL без файла -shm (жёсткое убийство старого коллектора): read-only
        подключение не имеет права создать -shm и падает, immutable читает
        основной файл мимо WAL — теряется максимум последняя минута записей.
        """
        base_uri = f"file:{self._db_path}?mode=ro"
        failures: list[str] = []

        for uri in (base_uri, f"{base_uri}&immutable=1"):
            try:
                with closing(sqlite3.connect(uri, uri=True, timeout=SQLITE_TIMEOUT_SEC)) as conn:
                    return work(conn)
            except sqlite3.Error as e:
                failures.append(str(e))

        raise LegacyStoreUnreadableError("; ".join(failures))

    def _export_sync(self, max_days: int) -> dict[str, Any]:
        cutoff = (_utc_now() - timedelta(days=max_days)).strftime(DATE_FORMAT)

        def work(conn: sqlite3.Connection) -> tuple[list[tuple], tuple]:
            span = _fetch(conn, "SELECT MIN(date), MAX(date) FROM daily_traffic")
            bounds = span[0] if span else (None, None)
            return _fetch(conn, EXPORT_SQL, (cutoff, MAX_EXPORT_ROWS + 1)), bounds

        rows, (min_date, max_date) = self._read(work)

        truncated = len(rows) > MAX_EXPORT_ROWS
        if truncated:
            rows = rows[:MAX_EXPORT_ROWS]
            logger.warning(f"Экспорт легаси-трафика обрезан до {MAX_EXPORT_ROWS} строк")

        daily = [
            {
                "date": row[0],
                "interface": row[1],
                "port": row[2],
                "rx_bytes": row[3] or 0,
                "tx_bytes": row[4] or 0,
            }
            for row in rows
        ]
        return {
            "available": True,
            "purged_at": None,
            "fingerprint": self._fingerprint(len(daily), min_date, max_date),
            "row_count": len(daily),
            "min_date": min_date,
            "max_date": max_date,
            "truncated": truncated,
            "daily": daily,
        }

    def _fingerprint(self, row_count: int, min_date: Optional[str], max_date: Optional[str]) -> str:
        try:
            stat = self._db_path.stat()
            size, mtime = stat.st_size, int(stat.st_mtime)
        except OSError:
            size, mtime = 0, 0
        raw = f"{size}|{mtime}|{min_date}|{max_date}|{row_count}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _purge_sync(self) -> dict[str, Any]:
        removed: list[str] = []
        # traffic_config.json не трогаем: это конфигурация правил iptables для
        # семплера портов, а не история.
        for path in (self._db_path, *self._sidecars, self._state_path):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as e:
                raise LegacyStorePurgeError(f"{path.name}: {e}")
            removed.append(path.name)

        purged_at = self.purged_at() or _iso(_utc_now())
        try:
            self._tombstone_path.write_text(json.dumps({"purged_at": purged_at}, indent=2))
        except OSError as e:
            raise LegacyStorePurgeError(f"{self._tombstone_path.name}: {e}")

        if removed:
            logger.info(f"Легаси-хранилище трафика удалено: {', '.join(removed)}")
        return {"success": True, "purged_at": purged_at, "removed": removed}


_store: Optional[LegacyTrafficStore] = None


def get_legacy_traffic_store() -> LegacyTrafficStore:
    global _store
    if _store is None:
        _store = LegacyTrafficStore()
    return _store
