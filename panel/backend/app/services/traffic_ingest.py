"""Учёт трафика серверов: сырые счётчики ноды → часовые и суточные бакеты в PostgreSQL.

Нода отдаёт только кумулятивные значения (интерфейсы, суммарный трафик, счётчики цепочек
iptables по портам), всю историю и арифметику ведёт панель. Арифметика собрана в чистых
функциях уровня модуля — детект сброса счётчика, раскладка дельты по бакетам и проверка
правдоподобности скорости считаются без обращения к БД и проверяются тестами напрямую.

`TrafficIngest` только оркестрирует: держит в памяти последние значения счётчиков и
аккумулятор бакетов, раз в минуту сбрасывая и то и другое в PostgreSQL одной транзакцией.
"""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterator, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.database import async_session
from app.models import Server, ServerTraffic, ServerTrafficCounter

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 60
UPSERT_BATCH_SIZE = 500

# Дельта старше этого окна не раскладывается целиком: панель могла простоять неделями,
# и заливать историю за всё это время по одному наблюдению бессмысленно.
MAX_BACKFILL_DAYS = 35

# Момент загрузки хоста, вычисленный из uptime, дрожит на секунды от округления и
# рассинхрона часов — сбросом считаем только сдвиг заметно больше этого дрожания.
BOOT_DRIFT_TOLERANCE_SECONDS = 120

SPEED_TOLERANCE = 1.25
MBIT_IN_BYTES_PER_SEC = 125_000
UNKNOWN_LINK_BYTES_PER_SEC = 100_000 * MBIT_IN_BYTES_PER_SEC  # 100 Гбит/с

# Скорость, отданная алертеру, протухает: молчащая нода не должна выглядеть как нода
# со стабильным трафиком.
SPEED_TTL_SECONDS = 120

SOURCE_LIVE = "live"
TOTAL_SCOPE_KEY = ""

# Виртуальные интерфейсы дублируют трафик физических; список повторяет
# VIRTUAL_IFACE_PREFIXES ноды, иначе сумма по интерфейсам разойдётся с total.
VIRTUAL_IFACE_PREFIXES = (
    'veth', 'docker', 'br-', 'virbr', 'flannel', 'cni', 'cali',
    'wg', 'tun', 'tap', 'warp', 'gre', 'sit', 'ip6tnl',
)

_HOUR = timedelta(hours=1)
_MICROSECOND = timedelta(microseconds=1)
_US_IN_SECOND = 1_000_000


class Scope(str, Enum):
    TOTAL = "total"
    IFACE = "iface"
    PORT = "port"


class Period(str, Enum):
    HOUR = "hour"
    DAY = "day"


class ResetKind(str, Enum):
    NONE = "none"
    BOOT_ID = "boot_id"
    BOOT_TIME = "boot_time"
    NEGATIVE = "negative"


@dataclass
class CounterState:
    """Последнее кумулятивное значение счётчика и обстоятельства, при которых оно снято."""
    rx_value: int
    tx_value: int
    observed_at: datetime
    boot_id: Optional[str] = None
    boot_at: Optional[datetime] = None


@dataclass(frozen=True)
class ObservedSpeeds:
    rx_bytes_per_sec: float = 0.0
    tx_bytes_per_sec: float = 0.0


@dataclass
class BucketDelta:
    rx_bytes: int = 0
    tx_bytes: int = 0
    covered_seconds: float = 0.0


CounterKey = tuple[int, str, str]
BucketKey = tuple[int, str, str, str, datetime]


def floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def floor_day(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def epoch_to_utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)


def as_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def boot_moment(observed_at: datetime, uptime_seconds) -> Optional[datetime]:
    """Момент загрузки хоста. Работает на нодах любых версий: uptime в контракте давно."""
    if uptime_seconds is None:
        return None
    try:
        uptime = float(uptime_seconds)
    except (TypeError, ValueError):
        return None
    if uptime < 0:
        return None
    return observed_at - timedelta(seconds=uptime)


def is_virtual_interface(iface: dict) -> bool:
    if iface.get("is_virtual"):
        return True
    return str(iface.get("name") or "").startswith(VIRTUAL_IFACE_PREFIXES)


def total_counters(interfaces: list[dict], network_total: Optional[dict]) -> Optional[tuple[int, int]]:
    """Суммарный счётчик по невиртуальным интерфейсам.

    Готовое поле network.total берётся только если список интерфейсов пуст: у ноды оно
    считается по тому же фильтру, но расхождение total с суммой частей ломало бы графики.
    """
    if interfaces:
        return (
            sum(int(iface.get("rx_bytes") or 0) for iface in interfaces),
            sum(int(iface.get("tx_bytes") or 0) for iface in interfaces),
        )
    if network_total:
        return int(network_total.get("rx_bytes") or 0), int(network_total.get("tx_bytes") or 0)
    return None


def link_speed_mbps(interfaces: list[dict]) -> Optional[int]:
    """Пропускная способность всех линков разом — потолок для суммарного счётчика."""
    total = 0
    for iface in interfaces:
        speed = iface.get("speed_mbps")
        if isinstance(speed, (int, float)) and speed > 0:
            total += int(speed)
    return total or None


def detect_reset(
    prev: Optional[CounterState],
    boot_id: Optional[str],
    boot_at: Optional[datetime],
    current_rx: int,
    current_tx: int,
) -> ResetKind:
    """Обнулился ли счётчик между наблюдениями. Первый сработавший признак выигрывает."""
    if prev is None:
        return ResetKind.NONE
    if boot_id and prev.boot_id and boot_id != prev.boot_id:
        return ResetKind.BOOT_ID
    if boot_at and prev.boot_at:
        drift = abs((boot_at - prev.boot_at).total_seconds())
        if drift > BOOT_DRIFT_TOLERANCE_SECONDS:
            return ResetKind.BOOT_TIME
    if current_rx < prev.rx_value or current_tx < prev.tx_value:
        return ResetKind.NEGATIVE
    return ResetKind.NONE


def clamp_backfill_start(start: datetime, now: datetime) -> datetime:
    earliest = now - timedelta(days=MAX_BACKFILL_DAYS)
    return start if start > earliest else earliest


def split_delta(
    prev_at: datetime,
    now_at: datetime,
    rx: int,
    tx: int,
) -> list[tuple[datetime, int, int, float]]:
    """Разложить дельту по часовым бакетам пропорционально времени, попавшему в каждый час.

    Последний бакет забирает остаток целочисленного деления: иначе сумма частей не сходится
    с исходной дельтой и история накапливает недостачу. Раскладка по реальной длительности —
    единственное, что не даёт вырасти фальшивому небоскрёбу после провала связи.
    """
    total_us = (now_at - prev_at) // _MICROSECOND
    if total_us <= 0:
        return [(floor_hour(now_at), rx, tx, 0.0)]

    spans: list[tuple[datetime, int]] = []
    cursor = prev_at
    while cursor < now_at:
        bucket = floor_hour(cursor)
        edge = min(bucket + _HOUR, now_at)
        spans.append((bucket, (edge - cursor) // _MICROSECOND))
        cursor = edge

    parts: list[tuple[datetime, int, int, float]] = []
    rx_left, tx_left = rx, tx
    last_index = len(spans) - 1
    for index, (bucket, span_us) in enumerate(spans):
        if index == last_index:
            rx_part, tx_part = rx_left, tx_left
        else:
            rx_part = rx * span_us // total_us
            tx_part = tx * span_us // total_us
        rx_left -= rx_part
        tx_left -= tx_part
        parts.append((bucket, rx_part, tx_part, span_us / _US_IN_SECOND))
    return parts


def rate_is_sane(rx_delta: int, tx_delta: int, seconds: float, speed_mbps: Optional[int] = None) -> bool:
    """Отсечка мусорных дельт: счётчик не может отдать больше, чем физически тянет линк."""
    if rx_delta <= 0 and tx_delta <= 0:
        return True
    if seconds <= 0:
        return False
    if speed_mbps and speed_mbps > 0:
        limit = speed_mbps * MBIT_IN_BYTES_PER_SEC * SPEED_TOLERANCE
    else:
        limit = UNKNOWN_LINK_BYTES_PER_SEC
    return max(rx_delta, tx_delta) / seconds <= limit


def _batched(rows: list[dict], size: int) -> Iterator[list[dict]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


class TrafficIngest:

    def __init__(self):
        self._counters: dict[CounterKey, CounterState] = {}
        self._accum: dict[BucketKey, BucketDelta] = {}
        self._dirty: set[CounterKey] = set()
        self._speeds: dict[int, tuple[float, ObservedSpeeds]] = {}
        self._port_samples: dict[int, float] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._running:
            return
        await self._load_counters()
        self._running = True
        self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush()

    def observe(self, server_id: int, metrics: dict, collected_at: float) -> ObservedSpeeds:
        """Разобрать ответ ноды в дельты трафика. Синхронно, только память."""
        network = metrics.get("network") or {}
        system = metrics.get("system") or {}
        observed_at = epoch_to_utc(collected_at)
        boot_id = system.get("boot_id") or None
        boot_at = boot_moment(observed_at, system.get("uptime_seconds"))

        interfaces = [
            iface for iface in (network.get("interfaces") or [])
            if isinstance(iface, dict) and not is_virtual_interface(iface)
        ]

        speeds = ObservedSpeeds()
        totals = total_counters(interfaces, network.get("total"))
        if totals is not None:
            speeds = self._apply(
                server_id, Scope.TOTAL, TOTAL_SCOPE_KEY, totals[0], totals[1],
                observed_at, boot_id, boot_at,
                speed_mbps=link_speed_mbps(interfaces), credit_reset=True,
            )

        for iface in interfaces:
            name = str(iface.get("name") or "").strip()
            if not name:
                continue
            self._apply(
                server_id, Scope.IFACE, name,
                int(iface.get("rx_bytes") or 0), int(iface.get("tx_bytes") or 0),
                observed_at, boot_id, boot_at,
                speed_mbps=iface.get("speed_mbps"), credit_reset=True,
            )

        self._observe_ports(server_id, network, observed_at, boot_id, boot_at)

        self._speeds[server_id] = (collected_at, speeds)
        return speeds

    def speed_for(self, server_id: int, collected_at: float) -> Optional[ObservedSpeeds]:
        entry = self._speeds.get(server_id)
        if entry is None:
            return None
        measured_at, speeds = entry
        if collected_at - measured_at > SPEED_TTL_SECONDS:
            return None
        return speeds

    async def flush(self) -> None:
        """Записать накопленные бакеты и счётчики одной транзакцией.

        Обмен аккумулятора и снимок счётчиков делаются без await между операциями —
        под asyncio это атомарно, локи не нужны. Счётчик нельзя двигать вперёд отдельно
        от бакетов: падение панели между двумя записями потеряло бы трафик.
        """
        pending, self._accum = self._accum, {}
        dirty, self._dirty = self._dirty, set()
        counter_rows = [self._counter_row(key) for key in dirty if key in self._counters]

        if not pending and not counter_rows:
            return

        bucket_rows = [
            {
                "server_id": server_id,
                "period_type": period,
                "scope": scope,
                "scope_key": scope_key,
                "bucket": bucket,
                "rx_bytes": delta.rx_bytes,
                "tx_bytes": delta.tx_bytes,
                "covered_seconds": round(delta.covered_seconds),
                "source": SOURCE_LIVE,
            }
            for (server_id, period, scope, scope_key, bucket), delta in pending.items()
        ]

        try:
            await self._write(bucket_rows, counter_rows)
        except IntegrityError as e:
            # Один удалённый сервер откатывает общую транзакцию, поэтому выбросить можно
            # только его строки: без requeue вместе с ним терялись бы бакеты всей флотилии,
            # а счётчики в памяти уже продвинуты и восстановить дельту неоткуда.
            dropped = await self._forget_deleted_servers(pending, dirty)
            self._requeue(pending, dirty)
            logger.error(f"Traffic flush rejected, dropped {dropped} orphaned buckets: {e}")
        except SQLAlchemyError as e:
            self._requeue(pending, dirty)
            logger.warning(f"Traffic flush failed, {len(bucket_rows)} buckets requeued: {e}")

    async def _write(self, bucket_rows: list[dict], counter_rows: list[dict]) -> None:
        async with async_session() as db:
            for batch in _batched(bucket_rows, UPSERT_BATCH_SIZE):
                stmt = pg_insert(ServerTraffic).values(batch)
                await db.execute(stmt.on_conflict_do_update(
                    constraint="uq_server_traffic",
                    set_={
                        "rx_bytes": ServerTraffic.rx_bytes + stmt.excluded.rx_bytes,
                        "tx_bytes": ServerTraffic.tx_bytes + stmt.excluded.tx_bytes,
                        "covered_seconds": ServerTraffic.covered_seconds + stmt.excluded.covered_seconds,
                    },
                ))

            for batch in _batched(counter_rows, UPSERT_BATCH_SIZE):
                stmt = pg_insert(ServerTrafficCounter).values(batch)
                await db.execute(stmt.on_conflict_do_update(
                    index_elements=["server_id", "scope", "scope_key"],
                    set_={
                        col: getattr(stmt.excluded, col)
                        for col in ("rx_value", "tx_value", "observed_at", "boot_id", "boot_at")
                    },
                ))

            await db.commit()

    async def _load_counters(self) -> None:
        async with async_session() as db:
            rows = (await db.execute(select(ServerTrafficCounter))).scalars().all()

        for row in rows:
            self._counters[(row.server_id, row.scope, row.scope_key)] = CounterState(
                rx_value=row.rx_value,
                tx_value=row.tx_value,
                observed_at=as_naive_utc(row.observed_at),
                boot_id=row.boot_id,
                boot_at=as_naive_utc(row.boot_at),
            )
        logger.info(f"Traffic ingest restored {len(rows)} counters")

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            try:
                await self.flush()
            except Exception as e:
                logger.error(f"Traffic flush cycle crashed: {e}")

    def _observe_ports(
        self,
        server_id: int,
        network: dict,
        observed_at: datetime,
        boot_id: Optional[str],
        boot_at: Optional[datetime],
    ) -> None:
        if not network.get("ports_available"):
            return
        ports = network.get("ports") or []
        if not ports:
            return

        # Семплер опрашивает iptables реже, чем панель опрашивает ноду: без этой отсечки
        # одни и те же счётчики читались бы по несколько раз и дельта ложилась бы не в своё окно.
        sampled_at = network.get("ports_sampled_at")
        if sampled_at is not None:
            if self._port_samples.get(server_id) == sampled_at:
                return
            self._port_samples[server_id] = sampled_at

        for entry in ports:
            if not isinstance(entry, dict) or entry.get("port") is None:
                continue
            self._apply(
                server_id, Scope.PORT, str(entry["port"]),
                int(entry.get("rx_bytes") or 0), int(entry.get("tx_bytes") or 0),
                observed_at, boot_id, boot_at,
                speed_mbps=None, credit_reset=False,
            )

    def _apply(
        self,
        server_id: int,
        scope: Scope,
        scope_key: str,
        rx: int,
        tx: int,
        observed_at: datetime,
        boot_id: Optional[str],
        boot_at: Optional[datetime],
        speed_mbps: Optional[int],
        credit_reset: bool,
    ) -> ObservedSpeeds:
        key = (server_id, scope.value, scope_key)
        prev = self._counters.get(key)
        if prev is None:
            self._remember(key, rx, tx, observed_at, boot_id, boot_at)
            return ObservedSpeeds()

        reset = detect_reset(prev, boot_id, boot_at, rx, tx)
        if reset is not ResetKind.NONE:
            self._remember(key, rx, tx, observed_at, boot_id, boot_at)
            # Приписать истории весь кумулятив можно только при ТОЧНОМ признаке ребута —
            # смене boot_id. Остальные признаки эвристические, и цена ошибки несимметрична:
            # boot_at считается из часов панели минус аптайм ноды, поэтому шаг часов на любой
            # стороне даёт ложный сброс, а начисление кумулятива на окно [boot_at, now]
            # закинуло бы в историю терабайты (проверка скорости это не ловит — окно
            # растянуто на сутки, и подразумеваемая скорость выходит низкой). Счётчики цепочек
            # iptables вообще обнуляются без ребута и к моменту загрузки не привязаны.
            # Поэтому на эвристике мы просто переустанавливаем базу: теряется один интервал,
            # а не выдумывается трафик, которого не было.
            if not credit_reset or boot_at is None or reset is not ResetKind.BOOT_ID:
                return ObservedSpeeds()
            self._store_delta(server_id, scope, scope_key, boot_at, observed_at, rx, tx, speed_mbps, reset)
            return ObservedSpeeds()

        started_at = prev.observed_at
        rx_delta = rx - prev.rx_value
        tx_delta = tx - prev.tx_value
        self._remember(key, rx, tx, observed_at, boot_id, boot_at)

        seconds = (observed_at - started_at).total_seconds()
        if seconds <= 0:
            return ObservedSpeeds()
        if not self._store_delta(
            server_id, scope, scope_key, started_at, observed_at, rx_delta, tx_delta, speed_mbps, reset
        ):
            return ObservedSpeeds()
        return ObservedSpeeds(rx_delta / seconds, tx_delta / seconds)

    def _store_delta(
        self,
        server_id: int,
        scope: Scope,
        scope_key: str,
        started_at: datetime,
        observed_at: datetime,
        rx_delta: int,
        tx_delta: int,
        speed_mbps: Optional[int],
        reset: ResetKind,
    ) -> bool:
        started_at = clamp_backfill_start(started_at, observed_at)

        if rx_delta <= 0 and tx_delta <= 0:
            # Байт нет, но наблюдение было — записываем только покрытие. Пропусти мы бакет
            # целиком, простаивающий порт был бы неотличим от недоступной ноды: строки нет,
            # и график нарисовал бы разрыв там, где на деле просто тишина.
            self._cover_window(server_id, scope, scope_key, started_at, observed_at)
            return False

        seconds = (observed_at - started_at).total_seconds()
        if not rate_is_sane(rx_delta, tx_delta, seconds, speed_mbps):
            logger.warning(
                f"Implausible traffic delta discarded: server={server_id} "
                f"scope={scope.value}/{scope_key or '-'} rx={rx_delta} tx={tx_delta} "
                f"seconds={seconds:.1f} link={speed_mbps or 'unknown'}Mbps reset={reset.value}"
            )
            return False

        for bucket, rx_part, tx_part, span in split_delta(started_at, observed_at, rx_delta, tx_delta):
            self._add_bucket(server_id, Period.HOUR, scope, scope_key, bucket, rx_part, tx_part, span)
            self._add_bucket(server_id, Period.DAY, scope, scope_key, floor_day(bucket), rx_part, tx_part, span)
        return True

    def _cover_window(
        self,
        server_id: int,
        scope: Scope,
        scope_key: str,
        started_at: datetime,
        observed_at: datetime,
    ) -> None:
        """Отметить окно наблюдённым, не начисляя байт."""
        for bucket, _, _, span in split_delta(started_at, observed_at, 0, 0):
            self._add_bucket(server_id, Period.HOUR, scope, scope_key, bucket, 0, 0, span)
            self._add_bucket(server_id, Period.DAY, scope, scope_key, floor_day(bucket), 0, 0, span)

    def _add_bucket(
        self,
        server_id: int,
        period: Period,
        scope: Scope,
        scope_key: str,
        bucket: datetime,
        rx_bytes: int,
        tx_bytes: int,
        seconds: float,
    ) -> None:
        key = (server_id, period.value, scope.value, scope_key, bucket)
        slot = self._accum.get(key)
        if slot is None:
            self._accum[key] = BucketDelta(rx_bytes, tx_bytes, seconds)
            return
        slot.rx_bytes += rx_bytes
        slot.tx_bytes += tx_bytes
        slot.covered_seconds += seconds

    def _remember(
        self,
        key: CounterKey,
        rx: int,
        tx: int,
        observed_at: datetime,
        boot_id: Optional[str],
        boot_at: Optional[datetime],
    ) -> None:
        state = self._counters.get(key)
        if state is None:
            self._counters[key] = CounterState(rx, tx, observed_at, boot_id, boot_at)
        else:
            state.rx_value = rx
            state.tx_value = tx
            state.observed_at = observed_at
            state.boot_id = boot_id
            state.boot_at = boot_at
        self._dirty.add(key)

    def _counter_row(self, key: CounterKey) -> dict:
        server_id, scope, scope_key = key
        state = self._counters[key]
        return {
            "server_id": server_id,
            "scope": scope,
            "scope_key": scope_key,
            "rx_value": state.rx_value,
            "tx_value": state.tx_value,
            "observed_at": state.observed_at,
            "boot_id": state.boot_id,
            "boot_at": state.boot_at,
        }

    async def _forget_deleted_servers(
        self, pending: dict[BucketKey, BucketDelta], dirty: set[CounterKey]
    ) -> int:
        """Выкинуть из партии строки серверов, которых больше нет в базе.

        Транзакция флаша общая, поэтому нарушение внешнего ключа по одному удалённому
        серверу откатывает бакеты всех остальных. Определяем виновных явным запросом и
        чистим только их — вместе с состоянием в памяти, иначе они ломали бы каждый
        следующий флаш.
        """
        server_ids = {key[0] for key in pending} | {key[0] for key in dirty}
        if not server_ids:
            return 0

        try:
            async with async_session() as db:
                result = await db.execute(select(Server.id).where(Server.id.in_(server_ids)))
                alive = set(result.scalars().all())
        except SQLAlchemyError as e:
            logger.warning(f"Cannot resolve deleted servers, keeping batch: {e}")
            return 0

        gone = server_ids - alive
        if not gone:
            return 0

        dropped = 0
        for key in [k for k in pending if k[0] in gone]:
            del pending[key]
            dropped += 1
        for key in [k for k in dirty if k[0] in gone]:
            dirty.discard(key)
        for store in (self._counters, self._speeds):
            for key in [k for k in store if (k[0] if isinstance(k, tuple) else k) in gone]:
                del store[key]
        return dropped

    def _requeue(self, pending: dict[BucketKey, BucketDelta], dirty: set[CounterKey]) -> None:
        for key, delta in pending.items():
            slot = self._accum.get(key)
            if slot is None:
                self._accum[key] = delta
                continue
            slot.rx_bytes += delta.rx_bytes
            slot.tx_bytes += delta.tx_bytes
            slot.covered_seconds += delta.covered_seconds
        self._dirty |= dirty


_ingest: Optional[TrafficIngest] = None


def get_traffic_ingest() -> TrafficIngest:
    global _ingest
    if _ingest is None:
        _ingest = TrafficIngest()
    return _ingest
