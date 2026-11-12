"""
Cloud.ru Advanced: ширина канала публичных IP по расписанию или по нагрузке нод.

Аккаунт — постоянный ключ AK/SK и Project ID. Под автоматику добавляются
точечно адрес с выделенной полосой или общая полоса целиком (её делят до 20
адресов), у каждой записи своё правило: расписание (часы суток по Москве →
Мбит/с) или динамика (пик скорости привязанных нод за окно + запас). Планировщик
раз в минуту берёт записи, которые в текущем часу ещё не проверены, и меняет
размер полосы (PUT /v1/{project_id}/bandwidths/{id}) только если он отличается
от нужного. По истории смен размера считаются средняя ширина и оплата за период
по тарифу Advanced. Подпись запросов и разбор ответов Cloud.ru — из модуля Advanced IP-поиска.
"""

import asyncio
import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

import httpx
from sqlalchemy import Integer, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import decrypt_secret, encrypt_secret
from app.database import async_session
from app.services.ip_search import cloudru_adv
from app.services.metrics_history import load_collect_interval
from app.services.server_ip_owners import load_ip_owners

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

# Москва без перехода на летнее время с 2014 года; zoneinfo в образе может не быть
MOSCOW_TZ = timezone(timedelta(hours=3))
HOURS_PER_DAY = 24
MIN_SCHEDULE_RULES = 2

# Шаг размера полосы по справочнику Cloud.ru Advanced (eip_apiBandwidth_0003)
FINE_STEP_LIMIT_MBPS = 300
MEDIUM_STEP_LIMIT_MBPS = 1000
MEDIUM_STEP_MBPS = 50
COARSE_STEP_MBPS = 500
# Потолок полосы EIP в консоли Cloud.ru Advanced
MAX_SIZE_MBPS = 2000
DEDICATED_MIN_MBPS = 1
# Минимум общей полосы по справочнику (eip_apisharedbandwidth_0001)
SHARED_MIN_MBPS = 5
SIZE_RULE_HINT = (
    "До 300 Мбит/с — любое значение, от 300 до 1000 — кратно 50, больше 1000 — кратно 500, "
    f"максимум {MAX_SIZE_MBPS}"
)
SHARED_SIZE_RULE_HINT = f"Общая полоса — не меньше {SHARED_MIN_MBPS} Мбит/с. {SIZE_RULE_HINT}"

DYNAMIC_NEEDS_NODE = "Для динамики привяжите адрес к ноде — полоса считается по её скорости"
DYNAMIC_NEEDS_SHARED_NODES = "Для динамики привяжите общую полосу к нодам — она считается по их суммарной скорости"

TICK_SECONDS = 60
RETRY_DELAY = timedelta(minutes=5)
# Антифлуд Cloud.ru срабатывал после 6-7 быстрых запросов подряд
API_GAP_SECONDS = 1.5
ERROR_HISTORY_RETENTION = timedelta(days=30)
# Смены размера — основа средней полосы и оплаты: держим больше года, чтобы сравнивать месяцы
CHANGE_HISTORY_RETENTION = timedelta(days=400)
EXTERNAL_CHANGE_NOTE = "Размер поменяли вне панели (в консоли Cloud.ru или через API)"

# Тариф Cloud.ru Advanced, приложение 7.ADV.32 версии 260619, руб. без НДС.
# Первые Мбит/с полосы дешевле: у выделенной до 5, у общей до 4
BANDWIDTH_PRICE_PER_MBPS_HOUR = 0.022
LOW_TIER_PRICE_PER_MBPS_HOUR = 0.017
DEDICATED_LOW_TIER_MBPS = 5
SHARED_LOW_TIER_MBPS = 4
EIP_PRICE_PER_HOUR = 0.17
VAT_RATE = 0.22
FORECAST_HOURS = 30 * HOURS_PER_DAY
SECONDS_PER_HOUR = 3600

REQUEST_TIMEOUT = 20.0
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 2.0
# Антифлуд — отказ временный: Huawei, на котором построен Advanced, советует повтор через 2–5 с.
# Лимит этот раздел почти не тратит (запрос-два в час на запись) — его выбирают соседи по аккаунту.
# Паузы 3 + 6 с: сохранение правила (до трёх запросов) укладывается в 30 с таймаута страницы
RATE_LIMIT_RETRY_DELAY = 3.0

# Покрытие окна метриками считается по 10-минутным корзинам — не зависит от интервала сбора
COVERAGE_BUCKET_MINUTES = 10
MIN_DECREASE_COVERAGE = 0.8
BYTES_PER_SEC_TO_MBPS = 8 / 1_000_000
# Скорости нод общей полосы складываются по бакетам; в бакете должен оказаться
# хотя бы один снапшот каждой ноды, поэтому он не короче двух интервалов сбора
PEAK_BUCKET_MIN_SECONDS = 60

SECRET_MASK = "****"

_RATE_LIMIT_CODES = {"VPC.0514", "SYS.0429", "APIGW.0308"}
_AUTH_CODES = {"VPC.0008", "APIGW.0301"}


class Mode(str, Enum):
    OFF = "off"
    SCHEDULE = "schedule"
    DYNAMIC = "dynamic"


class ShareType(str, Enum):
    DEDICATED = "PER"
    SHARED = "WHOLE"


class UsagePeriod(str, Enum):
    DAY = "day"
    WEEK = "week"
    LAST_30_DAYS = "30d"
    THIS_MONTH = "month"
    PREVIOUS_MONTH = "prev_month"


class CloudBwError(Exception):
    """Отказ, текст которого показывается пользователю как есть.
    retryable — сбой временный, планировщик повторит через RETRY_DELAY, а не через час."""

    def __init__(self, message: str, retryable: bool = False, status: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class CloudBwNotFound(CloudBwError):
    pass


class CloudBwRateLimited(CloudBwError):
    pass


# ═══════════════════════════════════════════════════════════════════════
# DB TABLES
# ═══════════════════════════════════════════════════════════════════════

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS cloud_bw_accounts (
    id SERIAL PRIMARY KEY,
    label VARCHAR(255) NOT NULL DEFAULT '',
    key_id VARCHAR(255) NOT NULL,
    key_secret TEXT NOT NULL,
    project_id VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cloud_bw_ips (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES cloud_bw_accounts(id) ON DELETE CASCADE,
    -- У общей полосы адреса нет: eip_id NULL, адреса — в members
    eip_id VARCHAR(64) UNIQUE,
    ip_address VARCHAR(50) NOT NULL,
    bandwidth_id VARCHAR(64) NOT NULL,
    shared BOOLEAN NOT NULL DEFAULT false,
    bandwidth_name VARCHAR(255),
    members TEXT NOT NULL DEFAULT '[]',
    -- Выделенная полоса — одна нода в server_id, общая — список в server_ids
    server_id INTEGER REFERENCES servers(id) ON DELETE SET NULL,
    server_ids INTEGER[] NOT NULL DEFAULT '{}',
    mode VARCHAR(10) NOT NULL DEFAULT 'off',
    schedule TEXT NOT NULL DEFAULT '[]',
    dyn_window_hours INTEGER NOT NULL DEFAULT 3,
    dyn_reserve_percent INTEGER NOT NULL DEFAULT 20,
    dyn_interval_hours INTEGER NOT NULL DEFAULT 1,
    dyn_min_mbps INTEGER NOT NULL DEFAULT 100,
    dyn_max_mbps INTEGER NOT NULL DEFAULT 2000,
    current_mbps INTEGER,
    charge_mode VARCHAR(20),
    share_type VARCHAR(10),
    last_check_at TIMESTAMPTZ,
    last_change_at TIMESTAMPTZ,
    last_peak_mbps REAL,
    last_note TEXT,
    last_error TEXT,
    retry_at TIMESTAMPTZ,
    evaluated_hour BIGINT,
    dyn_next_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cloud_bw_history (
    id BIGSERIAL PRIMARY KEY,
    ip_id INTEGER NOT NULL REFERENCES cloud_bw_ips(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    old_mbps INTEGER,
    new_mbps INTEGER,
    note TEXT,
    error TEXT
);

ALTER TABLE cloud_bw_ips ALTER COLUMN dyn_max_mbps SET DEFAULT 2000;
ALTER TABLE cloud_bw_ips ALTER COLUMN eip_id DROP NOT NULL;
ALTER TABLE cloud_bw_ips ADD COLUMN IF NOT EXISTS shared BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cloud_bw_ips ADD COLUMN IF NOT EXISTS bandwidth_name VARCHAR(255);
ALTER TABLE cloud_bw_ips ADD COLUMN IF NOT EXISTS members TEXT NOT NULL DEFAULT '[]';
ALTER TABLE cloud_bw_ips ADD COLUMN IF NOT EXISTS server_ids INTEGER[] NOT NULL DEFAULT '{}';

CREATE UNIQUE INDEX IF NOT EXISTS uq_cloud_bw_shared_bandwidth ON cloud_bw_ips(bandwidth_id) WHERE shared;
CREATE INDEX IF NOT EXISTS idx_cloud_bw_history_ip ON cloud_bw_history(ip_id, id);
CREATE INDEX IF NOT EXISTS idx_cloud_bw_history_created ON cloud_bw_history(created_at)
"""

_tables_ready = False
_tables_lock: Optional[asyncio.Lock] = None


async def init_tables(engine) -> None:
    global _tables_ready
    async with engine.begin() as conn:
        for statement in TABLES_SQL.split(";"):
            if statement.strip():
                await conn.execute(text(statement))
    _tables_ready = True
    logger.info("Cloud.ru bandwidth tables initialized")


async def _ensure_tables() -> None:
    """Страховка на случай, если старт роутера не создал таблицы (сбой базы в момент старта)."""
    global _tables_lock
    if _tables_ready:
        return
    if _tables_lock is None:
        _tables_lock = asyncio.Lock()
    async with _tables_lock:
        if not _tables_ready:
            from app.database import engine
            await init_tables(engine)


# ═══════════════════════════════════════════════════════════════════════
# BANDWIDTH SIZE RULES
# ═══════════════════════════════════════════════════════════════════════

def size_step(mbps: int) -> int:
    if mbps <= FINE_STEP_LIMIT_MBPS:
        return 1
    if mbps <= MEDIUM_STEP_LIMIT_MBPS:
        return MEDIUM_STEP_MBPS
    return COARSE_STEP_MBPS


def is_valid_size(mbps: int, shared: bool) -> bool:
    min_mbps = SHARED_MIN_MBPS if shared else DEDICATED_MIN_MBPS
    return min_mbps <= mbps <= MAX_SIZE_MBPS and mbps % size_step(mbps) == 0


def round_up_to_valid_size(mbps: float) -> int:
    value = max(1, math.ceil(mbps))
    step = size_step(value)
    return min(math.ceil(value / step) * step, MAX_SIZE_MBPS)


def _require_valid_size(mbps: int, what: str, shared: bool) -> None:
    if not is_valid_size(mbps, shared):
        hint = SHARED_SIZE_RULE_HINT if shared else SIZE_RULE_HINT
        raise CloudBwError(f"{what}: {mbps} Мбит/с — Cloud.ru не примет такой размер. {hint}")


# ═══════════════════════════════════════════════════════════════════════
# SCHEDULE / DYNAMIC RULES
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ScheduleRule:
    start_hour: int
    # Час окончания не входит в интервал; меньше начала — интервал через полночь
    end_hour: int
    mbps: int

    def hours(self) -> list[int]:
        if self.end_hour > self.start_hour:
            return list(range(self.start_hour, self.end_hour))
        return list(range(self.start_hour, HOURS_PER_DAY)) + list(range(0, self.end_hour))

    def label(self) -> str:
        return f"{self.start_hour:02d}:00–{self.end_hour % HOURS_PER_DAY:02d}:00"


@dataclass(frozen=True)
class DynamicSettings:
    window_hours: int
    reserve_percent: int
    interval_hours: int
    min_mbps: int
    max_mbps: int


@dataclass(frozen=True)
class Decision:
    target_mbps: Optional[int]
    note: str
    peak_mbps: Optional[float] = None
    # Плановый пересчёт динамики выполнен — следующий через interval_hours
    regular_done: bool = False


def _format_hour_ranges(hours: list[int]) -> str:
    """[0, 1, 5, 23] → «23:00–02:00, 05:00–06:00»: часы подряд склеиваются, в том числе через полночь."""
    ranges: list[list[int]] = []
    for hour in hours:
        if ranges and ranges[-1][1] == hour:
            ranges[-1][1] = hour + 1
        else:
            ranges.append([hour, hour + 1])
    if len(ranges) > 1 and ranges[0][0] == 0 and ranges[-1][1] == HOURS_PER_DAY:
        ranges[0][0] = ranges.pop()[0]
    return ", ".join(f"{start:02d}:00–{end % HOURS_PER_DAY:02d}:00" for start, end in ranges)


def parse_schedule(raw: list[dict]) -> list[ScheduleRule]:
    rules = [ScheduleRule(int(r["start_hour"]), int(r["end_hour"]), int(r["mbps"])) for r in raw]
    return sorted(rules, key=lambda rule: rule.start_hour)


def validate_schedule(rules: list[ScheduleRule], shared: bool) -> None:
    if len(rules) < MIN_SCHEDULE_RULES:
        raise CloudBwError(f"В расписании нужно минимум {MIN_SCHEDULE_RULES} интервала")

    owners: list[Optional[ScheduleRule]] = [None] * HOURS_PER_DAY
    for rule in rules:
        if rule.start_hour == rule.end_hour:
            raise CloudBwError(f"Интервал {rule.label()} пустой — начало совпадает с концом")
        _require_valid_size(rule.mbps, f"Интервал {rule.label()}", shared)
        for hour in rule.hours():
            if owners[hour] is not None:
                raise CloudBwError(f"Интервалы {owners[hour].label()} и {rule.label()} пересекаются")
            owners[hour] = rule

    missing = [hour for hour, owner in enumerate(owners) if owner is None]
    if missing:
        raise CloudBwError(f"Расписание должно покрывать все 24 часа. Не покрыто: {_format_hour_ranges(missing)}")


def validate_dynamic(settings: DynamicSettings, shared: bool) -> None:
    _require_valid_size(settings.min_mbps, "Минимум", shared)
    _require_valid_size(settings.max_mbps, "Максимум", shared)
    if settings.min_mbps > settings.max_mbps:
        raise CloudBwError("Минимум больше максимума")


def decide_schedule(rules: list[ScheduleRule], now: datetime) -> Decision:
    hour = now.astimezone(MOSCOW_TZ).hour
    rule = next(rule for rule in rules if hour in rule.hours())
    return Decision(rule.mbps, f"Расписание {rule.label()} → {rule.mbps} Мбит/с")


def _dynamic_basis(settings: DynamicSettings, peak_mbps: float, node_count: int,
                   wanted: float, rounded: int, target: int) -> str:
    """Расчёт целиком, с причиной, если итог отличается от «пик + запас»: без неё
    1293 + 20% → 2000 выглядит ошибкой, хотя выше 1000 Cloud.ru принимает только шаг 500."""
    peak = f"Пик {peak_mbps:.0f} Мбит/с" + (f" (сумма {node_count} нод)" if node_count > 1 else "")
    text = (f"{peak} за {settings.window_hours} ч + {settings.reserve_percent}% "
            f"= {wanted:.0f} → {target} Мбит/с")
    if target > rounded:
        return f"{text} (минимум правила)"
    if target < rounded:
        return f"{text} (максимум правила)"
    if target > math.ceil(wanted):
        return f"{text} (ближайший размер, который примет Cloud.ru: тут шаг {size_step(target)} Мбит/с)"
    return text


def decide_dynamic(
    settings: DynamicSettings, current_mbps: int, node_count: int,
    peak_mbps: Optional[float], coverage: float, regular_due: bool,
) -> Decision:
    """Поднимать — в любой час, как только пик с запасом выше текущей полосы:
    упёршийся в потолок канал режет трафик. Снижать — только в плановый пересчёт
    и при метриках почти за всё окно, иначе дыра в данных выглядит как спад.
    coverage у нескольких нод — по самой неполной: без её метрик сумма занижена."""
    if peak_mbps is None:
        nodes = "нод" if node_count > 1 else "ноды"
        return Decision(None, f"Нет метрик {nodes} за {settings.window_hours} ч — размер не меняю")

    wanted = peak_mbps * (1 + settings.reserve_percent / 100)
    rounded = round_up_to_valid_size(wanted)
    target = min(max(rounded, settings.min_mbps), settings.max_mbps)
    basis = _dynamic_basis(settings, peak_mbps, node_count, wanted, rounded, target)

    if target > current_mbps:
        return Decision(target, basis, peak_mbps, regular_due)
    if target == current_mbps:
        return Decision(None, basis, peak_mbps, regular_due)
    if not regular_due:
        return Decision(None, f"{basis}; снижу в плановый пересчёт", peak_mbps)
    if coverage < MIN_DECREASE_COVERAGE:
        whose = "у одной из нод метрики есть" if node_count > 1 else "метрики есть"
        return Decision(None, f"{basis}, но {whose} только за {coverage:.0%} окна — не снижаю", peak_mbps, True)
    return Decision(target, basis, peak_mbps, True)


def _hour_key(moment: datetime) -> int:
    return int(moment.timestamp() // 3600)


def _hour_start(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


# ═══════════════════════════════════════════════════════════════════════
# USAGE AND COST
# ═══════════════════════════════════════════════════════════════════════

_ROLLING_PERIODS = {
    UsagePeriod.DAY: timedelta(days=1),
    UsagePeriod.WEEK: timedelta(days=7),
    UsagePeriod.LAST_30_DAYS: timedelta(days=30),
}


@dataclass(frozen=True)
class SizeChange:
    at: datetime
    old_mbps: Optional[int]
    new_mbps: int


@dataclass(frozen=True)
class SizeSegment:
    mbps: int
    start: datetime
    end: datetime

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / SECONDS_PER_HOUR


@dataclass(frozen=True)
class Usage:
    hours: float
    mbps_hours: float
    min_mbps: Optional[int]
    max_mbps: Optional[int]
    covered_from: Optional[datetime]
    # Рубли без НДС
    bandwidth_cost: float
    eip_cost: float

    @property
    def avg_mbps(self) -> Optional[float]:
        return self.mbps_hours / self.hours if self.hours else None

    @property
    def cost(self) -> float:
        return self.bandwidth_cost + self.eip_cost


def _month_start(moment: datetime) -> datetime:
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def usage_period_bounds(period: UsagePeriod, now: datetime) -> tuple[datetime, datetime]:
    """Календарный месяц — по московскому времени, как и часы расписания"""
    if period in _ROLLING_PERIODS:
        return now - _ROLLING_PERIODS[period], now
    this_month = _month_start(now.astimezone(MOSCOW_TZ))
    if period == UsagePeriod.THIS_MONTH:
        return this_month, now
    return _month_start(this_month - timedelta(days=1)), this_month


def size_segments(changes: list[SizeChange], current_mbps: Optional[int],
                  start: datetime, end: datetime) -> list[SizeSegment]:
    """Отрезки постоянной ширины внутри [start, end). changes — по времени: последняя смена
    до start и все смены внутри. Размер на start — итог последней смены до него, иначе
    исходный размер первой смены внутри, иначе текущий: значит, полосу за период не меняли.
    Пока размер неизвестен, отрезка нет — эта часть периода считается не покрытой."""
    if start >= end:
        return []
    before = [change for change in changes if change.at <= start]
    inside = [change for change in changes if start < change.at < end]
    if before:
        size = before[-1].new_mbps
    elif inside:
        size = inside[0].old_mbps
    else:
        size = current_mbps

    segments: list[SizeSegment] = []
    cursor = start
    for change in inside:
        if size is not None and change.at > cursor:
            segments.append(SizeSegment(size, cursor, change.at))
        cursor, size = change.at, change.new_mbps
    if size is not None:
        segments.append(SizeSegment(size, cursor, end))
    return segments


def bandwidth_price_per_hour(mbps: int, shared: bool) -> float:
    low_tier = min(mbps, SHARED_LOW_TIER_MBPS if shared else DEDICATED_LOW_TIER_MBPS)
    return low_tier * LOW_TIER_PRICE_PER_MBPS_HOUR + (mbps - low_tier) * BANDWIDTH_PRICE_PER_MBPS_HOUR


def summarize_usage(segments: list[SizeSegment], shared: bool, eip_count: int) -> Usage:
    hours = sum(segment.hours for segment in segments)
    return Usage(
        hours=hours,
        mbps_hours=sum(segment.mbps * segment.hours for segment in segments),
        min_mbps=min((segment.mbps for segment in segments), default=None),
        max_mbps=max((segment.mbps for segment in segments), default=None),
        covered_from=segments[0].start if segments else None,
        bandwidth_cost=sum(bandwidth_price_per_hour(segment.mbps, shared) * segment.hours for segment in segments),
        eip_cost=eip_count * hours * EIP_PRICE_PER_HOUR,
    )


def _with_vat(rubles: float) -> float:
    return round(rubles * (1 + VAT_RATE), 2)


# ═══════════════════════════════════════════════════════════════════════
# CLOUD.RU API
# ═══════════════════════════════════════════════════════════════════════

_http: Optional[httpx.AsyncClient] = None


def _http_client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers={"Accept": "application/json"})
    return _http


def _api_error(resp: httpx.Response) -> CloudBwError:
    code, _neutron_type, message = cloudru_adv._parse_error(resp)
    tag = code or f"HTTP {resp.status_code}"
    status = resp.status_code
    # Код важнее статуса: антифлуд Cloud.ru приходит и со статусом 403
    if code in _RATE_LIMIT_CODES or status == 429:
        # Шлюз пишет, какая политика сработала (user/ip/api), лимит и окно — по ним видно, кто выбрал лимит
        detail = f" Ответ Cloud.ru: {message}" if message else ""
        return CloudBwRateLimited(
            f"Cloud.ru временно ограничил частоту запросов ({tag}).{detail}",
            retryable=True, status=status,
        )
    if code in _AUTH_CODES or status == 401:
        return CloudBwError(f"Ключ не принят — проверьте AK, SK и Project ID ({tag})", status=status)
    if status == 403:
        return CloudBwError(
            f"Нет прав ({tag}): ключу нужны vpc:publicIps:list/get и vpc:bandwidths:get/update. {message}",
            status=status,
        )
    if status == 404:
        return CloudBwError(f"Не найдено в Cloud.ru ({tag})", status=status)
    return CloudBwError(f"Cloud.ru отказал ({tag}): {message}", retryable=status >= 500, status=status)


def _retry_delay(error: CloudBwError, attempt: int) -> float:
    base = RATE_LIMIT_RETRY_DELAY if isinstance(error, CloudBwRateLimited) else RETRY_BASE_DELAY
    return base * 2 ** (attempt - 1)


class CloudBwClient:
    """VPC API Cloud.ru Advanced одного аккаунта: публичные IP и их полосы."""

    def __init__(self, http: httpx.AsyncClient, key_id: str, key_secret: str, project_id: str):
        self._http = http
        self._key_id = key_id
        self._key_secret = key_secret
        self._project_id = project_id
        self._base = f"{cloudru_adv.CLOUD_ADV_VPC_URL}/v1/{project_id}"

    async def list_publicips(self) -> list[dict]:
        return (await self._request("GET", "/publicips")).get("publicips") or []

    async def get_publicip(self, eip_id: str) -> dict:
        return (await self._request("GET", f"/publicips/{eip_id}"))["publicip"]

    async def get_bandwidth(self, bandwidth_id: str) -> dict:
        return (await self._request("GET", f"/bandwidths/{bandwidth_id}"))["bandwidth"]

    async def set_bandwidth_size(self, bandwidth_id: str, mbps: int) -> dict:
        # В теле только size: способ оплаты и имя полосы не трогаем
        body = {"bandwidth": {"size": mbps}}
        return (await self._request("PUT", f"/bandwidths/{bandwidth_id}", body))["bandwidth"]

    async def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """Повтор при сетевом сбое, 5xx и антифлуде. Повторять PUT безопасно: он задаёт размер, а не прибавляет."""
        url = self._base + path
        payload = json.dumps(body).encode("utf-8") if body is not None else b""
        content_type = "application/json" if body is not None else None
        last_error = CloudBwError("Cloud.ru не ответил", retryable=True)

        for attempt in range(RETRY_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_retry_delay(last_error, attempt))
            # Подпись каждый раз заново: в неё входит время запроса
            headers = cloudru_adv._sign_request(
                self._key_id, self._key_secret, method, url, payload, content_type, self._project_id,
            )
            try:
                resp = await self._http.request(method, url, content=payload or None, headers=headers)
            except httpx.HTTPError as e:
                last_error = CloudBwError(f"Нет связи с Cloud.ru: {str(e) or type(e).__name__}", retryable=True)
                logger.warning("cloud_bw request failed: %s %s attempt=%d error=%s", method, path, attempt + 1, last_error)
                continue
            if resp.status_code >= 400:
                last_error = _api_error(resp)
                if not last_error.retryable:
                    raise last_error
                logger.warning("cloud_bw request failed: %s %s attempt=%d error=%s", method, path, attempt + 1, last_error)
                continue
            try:
                return resp.json()
            except ValueError:
                raise CloudBwError(f"Cloud.ru вернул не JSON на {method} {path}", retryable=True)

        raise last_error


def _cloud_client(key_id: str, stored_secret: str, project_id: str) -> CloudBwClient:
    secret = decrypt_secret(stored_secret)
    if not secret:
        raise CloudBwError("SK аккаунта не расшифровывается — введите его заново")
    return CloudBwClient(_http_client(), key_id, secret, project_id)


def _ensure_eligible(bandwidth: dict, shared: bool) -> None:
    is_shared = bandwidth.get("share_type") == ShareType.SHARED.value
    if is_shared and not shared:
        # Выделенная полоса освобождается, когда адрес переводят в общую — менять надо общую
        raise CloudBwError(
            "Адрес в общей полосе — её делят несколько адресов. Добавьте общую полосу целиком: "
            "она есть в списке адресов проекта"
        )
    if shared and not is_shared:
        raise CloudBwError("Полоса больше не общая — удалите её из списка и добавьте адрес отдельно")
    if bandwidth.get("charge_mode") != "bandwidth":
        raise CloudBwError(
            "Оплата по трафику — от ширины канала цена не зависит. "
            "Переключите адрес на оплату по полосе в консоли Cloud.ru"
        )
    status = bandwidth.get("status") or "NORMAL"
    if status != "NORMAL":
        raise CloudBwError(f"Полоса в статусе {status} — Cloud.ru её не меняет (обычно из-за долга на балансе)")


def _serves_eip(bandwidth: dict, eip_id: str) -> bool:
    if "publicip_info" not in bandwidth:
        return True
    return any(info.get("publicip_id") == eip_id for info in bandwidth["publicip_info"] or [])


async def _current_bandwidth(client: CloudBwClient, row) -> dict:
    if row["shared"]:
        return await _shared_bandwidth(client, row["bandwidth_id"])
    return await _dedicated_bandwidth(client, row["eip_id"], row["bandwidth_id"])


async def _shared_bandwidth(client: CloudBwClient, bandwidth_id: str) -> dict:
    try:
        return await client.get_bandwidth(bandwidth_id)
    except CloudBwError as e:
        if e.status == 404:
            raise CloudBwError("Общей полосы больше нет в проекте Cloud.ru — удалите её из списка")
        raise


async def _dedicated_bandwidth(client: CloudBwClient, eip_id: str, bandwidth_id: str) -> dict:
    """Полоса адреса. Если сохранённая полоса пропала или обслуживает уже другой
    адрес, ID берётся заново из самого адреса — менять чужую полосу нельзя."""
    try:
        bandwidth = await client.get_bandwidth(bandwidth_id)
        if _serves_eip(bandwidth, eip_id):
            return bandwidth
    except CloudBwError as e:
        if e.status != 404:
            raise

    try:
        publicip = await client.get_publicip(eip_id)
    except CloudBwError as e:
        if e.status == 404:
            raise CloudBwError("Адреса больше нет в проекте Cloud.ru — удалите его из списка")
        raise
    return await client.get_bandwidth(publicip["bandwidth_id"])


# ═══════════════════════════════════════════════════════════════════════
# SERIALIZATION
# ═══════════════════════════════════════════════════════════════════════

def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _account_public(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "label": row["label"],
        "key_id": row["key_id"],
        "key_secret_masked": SECRET_MASK,
        "project_id": row["project_id"],
        "ip_count": row["ip_count"],
        "created_at": _iso(row["created_at"]),
    }


def _dynamic_from_row(row) -> DynamicSettings:
    return DynamicSettings(
        window_hours=row["dyn_window_hours"],
        reserve_percent=row["dyn_reserve_percent"],
        interval_hours=row["dyn_interval_hours"],
        min_mbps=row["dyn_min_mbps"],
        max_mbps=row["dyn_max_mbps"],
    )


def _row_server_ids(row) -> list[int]:
    """Ноды, по чьей скорости считается динамика: у общей полосы их несколько"""
    if row["shared"]:
        return list(row["live_server_ids"])
    return [row["server_id"]] if row["server_id"] is not None else []


def _row_subject(row) -> str:
    if row["shared"]:
        return f"shared {row['bandwidth_name'] or row['bandwidth_id']}"
    return row["ip_address"]


def _bandwidth_members(bandwidth: dict) -> list[dict[str, str]]:
    return [
        {"eip_id": info.get("publicip_id") or "", "ip_address": info.get("publicip_address") or ""}
        for info in bandwidth.get("publicip_info") or []
    ]


def _ip_public(row) -> dict[str, Any]:
    dynamic = _dynamic_from_row(row)
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "account_label": row["account_label"],
        "shared": row["shared"],
        "eip_id": row["eip_id"],
        "ip_address": row["ip_address"],
        "bandwidth_id": row["bandwidth_id"],
        "bandwidth_name": row["bandwidth_name"],
        "members": json.loads(row["members"]),
        "server_id": row["server_id"],
        "server_name": row["server_name"],
        "server_ids": list(row["live_server_ids"]),
        "mode": row["mode"],
        "schedule": json.loads(row["schedule"]),
        "dynamic": {
            "window_hours": dynamic.window_hours,
            "reserve_percent": dynamic.reserve_percent,
            "interval_hours": dynamic.interval_hours,
            "min_mbps": dynamic.min_mbps,
            "max_mbps": dynamic.max_mbps,
        },
        "current_mbps": row["current_mbps"],
        "charge_mode": row["charge_mode"],
        "share_type": row["share_type"],
        "last_check_at": _iso(row["last_check_at"]),
        "last_change_at": _iso(row["last_change_at"]),
        "last_peak_mbps": row["last_peak_mbps"],
        "last_note": row["last_note"],
        "last_error": row["last_error"],
        "next_decrease_at": _iso(row["dyn_next_at"]) if row["mode"] == Mode.DYNAMIC.value else None,
    }


ACCOUNT_SELECT = """
SELECT a.id, a.label, a.key_id, a.key_secret, a.project_id, a.created_at,
       (SELECT COUNT(*) FROM cloud_bw_ips i WHERE i.account_id = a.id) AS ip_count
FROM cloud_bw_accounts a
"""

# server_ids общей полосы — без нод, удалённых из панели уже после сохранения правила
LIVE_SERVER_IDS = "ARRAY(SELECT sv.id FROM servers sv WHERE sv.id = ANY(i.server_ids) ORDER BY sv.id) AS live_server_ids"

IP_SELECT = f"""
SELECT i.*, a.label AS account_label, s.name AS server_name, {LIVE_SERVER_IDS}
FROM cloud_bw_ips i
JOIN cloud_bw_accounts a ON a.id = i.account_id
LEFT JOIN servers s ON s.id = i.server_id
"""

def _server_ids_param():
    return bindparam("server_ids", type_=ARRAY(Integer))


# Для size_segments: последняя смена размера до начала периода и все смены внутри
SIZE_CHANGES_SQL = text("""
(SELECT DISTINCT ON (ip_id) id, ip_id, created_at, old_mbps, new_mbps FROM cloud_bw_history
 WHERE new_mbps IS NOT NULL AND created_at <= :start
 ORDER BY ip_id, created_at DESC, id DESC)
UNION ALL
(SELECT id, ip_id, created_at, old_mbps, new_mbps FROM cloud_bw_history
 WHERE new_mbps IS NOT NULL AND created_at > :start AND created_at < :end)
ORDER BY ip_id, created_at, id
""")


async def _load_size_changes(db: AsyncSession, start: datetime, end: datetime) -> dict[int, list[SizeChange]]:
    changes: dict[int, list[SizeChange]] = {}
    for row in (await db.execute(SIZE_CHANGES_SQL, {"start": start, "end": end})).mappings():
        changes.setdefault(row["ip_id"], []).append(SizeChange(row["created_at"], row["old_mbps"], row["new_mbps"]))
    return changes


def _eip_count(row) -> int:
    return len(json.loads(row["members"])) if row["shared"] else 1


def _usage_public(row, usage: Usage) -> dict[str, Any]:
    avg_mbps = usage.avg_mbps
    return {
        "ip_id": row["id"],
        "shared": row["shared"],
        "ip_address": row["ip_address"],
        "bandwidth_name": row["bandwidth_name"],
        "account_label": row["account_label"],
        "current_mbps": row["current_mbps"],
        "covered_from": _iso(usage.covered_from),
        "hours": round(usage.hours, 2),
        "avg_mbps": round(avg_mbps, 1) if avg_mbps is not None else None,
        "min_mbps": usage.min_mbps,
        "max_mbps": usage.max_mbps,
        "mbps_hours": round(usage.mbps_hours),
        "eip_count": _eip_count(row),
        "bandwidth_rub": _with_vat(usage.bandwidth_cost),
        "eip_rub": _with_vat(usage.eip_cost),
        "total_rub": _with_vat(usage.cost),
    }


def _usage_total(usages: list[Usage]) -> dict[str, Any]:
    covered = [usage for usage in usages if usage.hours]
    cost = sum(usage.cost for usage in covered)
    # Прогноз — по нынешнему часовому расходу каждой записи, даже если её добавили посреди периода
    hourly_cost = sum(usage.cost / usage.hours for usage in covered)
    return {
        "avg_mbps": round(sum(usage.avg_mbps for usage in covered), 1),
        "mbps_hours": round(sum(usage.mbps_hours for usage in covered)),
        "bandwidth_rub": _with_vat(sum(usage.bandwidth_cost for usage in covered)),
        "eip_rub": _with_vat(sum(usage.eip_cost for usage in covered)),
        "total_rub": _with_vat(cost),
        "total_rub_without_vat": round(cost, 2),
        "forecast_30d_rub": _with_vat(hourly_cost * FORECAST_HOURS),
    }


TARIFF_PUBLIC = {
    "bandwidth_per_mbps_hour": BANDWIDTH_PRICE_PER_MBPS_HOUR,
    "low_tier_per_mbps_hour": LOW_TIER_PRICE_PER_MBPS_HOUR,
    "dedicated_low_tier_mbps": DEDICATED_LOW_TIER_MBPS,
    "shared_low_tier_mbps": SHARED_LOW_TIER_MBPS,
    "eip_per_hour": EIP_PRICE_PER_HOUR,
    "vat_percent": round(VAT_RATE * 100),
}


# ═══════════════════════════════════════════════════════════════════════
# NODE METRICS
# ═══════════════════════════════════════════════════════════════════════

# Пик — секундный максимум скорости за окно опроса. Лимит полосы у каждого
# направления свой, поэтому вход и выход нод складываются по бакетам отдельно
# и берётся большее. Максимум ноды за бакет идёт в сумму целиком, даже если пики
# разных нод внутри бакета не совпали: сумма выходит не ниже настоящей. У одной
# ноды это ровно её пик. NULL в *_max — нода без оконной статистики
NODES_PEAK_SQL = text("""
WITH per_node AS (
    SELECT server_id,
           date_bin(make_interval(secs => :bucket), timestamp, TIMESTAMPTZ 'epoch') AS bucket,
           MAX(COALESCE(net_rx_bytes_per_sec_max, net_rx_bytes_per_sec)) AS rx,
           MAX(COALESCE(net_tx_bytes_per_sec_max, net_tx_bytes_per_sec)) AS tx
    FROM metrics_snapshots
    WHERE server_id = ANY(:server_ids) AND timestamp >= :since
    GROUP BY server_id, bucket
)
SELECT MAX(GREATEST(rx, tx)) FROM (
    SELECT SUM(rx) AS rx, SUM(tx) AS tx FROM per_node GROUP BY bucket
) AS totals
""").bindparams(_server_ids_param())

NODES_COVERAGE_SQL = text(f"""
SELECT server_id,
       COUNT(DISTINCT date_bin(INTERVAL '{COVERAGE_BUCKET_MINUTES} minutes', timestamp,
                               TIMESTAMPTZ '2000-01-01')) AS buckets
FROM metrics_snapshots
WHERE server_id = ANY(:server_ids) AND timestamp >= :since
GROUP BY server_id
""").bindparams(_server_ids_param())


async def _nodes_peak(db: AsyncSession, server_ids: list[int], now: datetime,
                      window_hours: int) -> tuple[Optional[float], float]:
    """(пик в Мбит/с или None без данных, доля окна, покрытая метриками у самой неполной ноды)"""
    params = {"server_ids": server_ids, "since": now - timedelta(hours=window_hours)}
    bucket_seconds = max(PEAK_BUCKET_MIN_SECONDS, 2 * await load_collect_interval(db))
    peak = (await db.execute(NODES_PEAK_SQL, {**params, "bucket": bucket_seconds})).scalar()
    if peak is None:
        return None, 0.0
    buckets = dict((await db.execute(NODES_COVERAGE_SQL, params)).all())
    expected_buckets = window_hours * 60 / COVERAGE_BUCKET_MINUTES
    coverage = min(buckets.get(server_id, 0) for server_id in server_ids) / expected_buckets
    return peak * BYTES_PER_SEC_TO_MBPS, min(1.0, coverage)


# ═══════════════════════════════════════════════════════════════════════
# EVALUATION
# ═══════════════════════════════════════════════════════════════════════

_ip_locks: dict[int, asyncio.Lock] = {}


async def evaluate_ip(ip_id: int, force_regular: bool = False) -> None:
    """Сверить полосу адреса с правилом и при расхождении поменять.
    force_regular — сохранение правила или ручной запуск: динамика пересчитывается
    как плановая, то есть может и снизить полосу."""
    async with _ip_locks.setdefault(ip_id, asyncio.Lock()):
        await _evaluate_locked(ip_id, force_regular)


async def _evaluate_locked(ip_id: int, force_regular: bool) -> None:
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        row = (await db.execute(text(f"""
            SELECT i.*, a.key_id, a.key_secret, a.project_id, {LIVE_SERVER_IDS}
            FROM cloud_bw_ips i JOIN cloud_bw_accounts a ON a.id = i.account_id
            WHERE i.id = :id
        """), {"id": ip_id})).mappings().one_or_none()
        if row is None or row["mode"] == Mode.OFF.value:
            return
        server_ids = _row_server_ids(row)
        peak_mbps, coverage = None, 0.0
        if row["mode"] == Mode.DYNAMIC.value and server_ids:
            peak_mbps, coverage = await _nodes_peak(db, server_ids, now, row["dyn_window_hours"])

    try:
        client = _cloud_client(row["key_id"], row["key_secret"], row["project_id"])
        bandwidth = await _current_bandwidth(client, row)
        _ensure_eligible(bandwidth, row["shared"])
        current = int(bandwidth["size"])
        decision = _decide(row, current, now, server_ids, peak_mbps, coverage, force_regular)

        changed_from = None
        if decision.target_mbps is not None and decision.target_mbps != current:
            updated = await client.set_bandwidth_size(bandwidth["id"], decision.target_mbps)
            if int(updated.get("size") or 0) != decision.target_mbps:
                raise CloudBwError(
                    f"Cloud.ru принял запрос, но вернул {updated.get('size')} Мбит/с вместо {decision.target_mbps}",
                    retryable=True,
                )
            changed_from, current = current, decision.target_mbps
    except CloudBwError as e:
        await _record_failure(row, now, e)
        return

    await _record_success(row, now, bandwidth, current, decision, changed_from)


def _decide(row, current: int, now: datetime, server_ids: list[int], peak_mbps: Optional[float],
            coverage: float, force_regular: bool) -> Decision:
    if row["mode"] == Mode.SCHEDULE.value:
        return decide_schedule(parse_schedule(json.loads(row["schedule"])), now)
    if not server_ids:
        raise CloudBwError(DYNAMIC_NEEDS_SHARED_NODES if row["shared"] else DYNAMIC_NEEDS_NODE)
    regular_due = force_regular or row["dyn_next_at"] is None or now >= row["dyn_next_at"]
    return decide_dynamic(_dynamic_from_row(row), current, len(server_ids), peak_mbps, coverage, regular_due)


async def _record_success(row, now: datetime, bandwidth: dict, current: int,
                          decision: Decision, changed_from: Optional[int]) -> None:
    next_regular = row["dyn_next_at"]
    if decision.regular_done:
        next_regular = _hour_start(now) + timedelta(hours=row["dyn_interval_hours"])
    # bandwidth — размер до нашей смены. Расхождение с записанным значит, что полосу меняли
    # мимо панели: без записи в истории средняя и оплата считались бы по старому размеру
    observed = int(bandwidth["size"])
    changed_outside = row["current_mbps"] is not None and observed != row["current_mbps"]

    async with async_session() as db:
        if changed_outside:
            await db.execute(text("""
                INSERT INTO cloud_bw_history (ip_id, old_mbps, new_mbps, note)
                VALUES (:ip_id, :old, :new, :note)
            """), {"ip_id": row["id"], "old": row["current_mbps"], "new": observed, "note": EXTERNAL_CHANGE_NOTE})
        await db.execute(text("""
            UPDATE cloud_bw_ips SET
                bandwidth_id = :bandwidth_id, current_mbps = :current, charge_mode = :charge_mode,
                share_type = :share_type, bandwidth_name = :bandwidth_name, members = :members,
                last_check_at = :now, last_peak_mbps = :peak,
                last_note = :note, last_error = NULL, retry_at = NULL, evaluated_hour = :hour,
                dyn_next_at = :next_regular,
                last_change_at = CASE WHEN :changed THEN :now ELSE last_change_at END
            WHERE id = :id
        """), {
            "id": row["id"], "bandwidth_id": bandwidth["id"], "current": current,
            "charge_mode": bandwidth.get("charge_mode"), "share_type": bandwidth.get("share_type"),
            "bandwidth_name": bandwidth.get("name"), "members": json.dumps(_bandwidth_members(bandwidth)),
            "now": now, "peak": decision.peak_mbps, "note": decision.note, "hour": _hour_key(now),
            "next_regular": next_regular, "changed": changed_from is not None,
        })
        if changed_from is not None:
            await db.execute(text("""
                INSERT INTO cloud_bw_history (ip_id, old_mbps, new_mbps, note)
                VALUES (:ip_id, :old, :new, :note)
            """), {"ip_id": row["id"], "old": changed_from, "new": current, "note": decision.note})
        await db.commit()

    if changed_outside:
        logger.info("cloud_bw size changed outside panel: target=%s %s -> %s Mbit/s",
                    _row_subject(row), row["current_mbps"], observed)
    if changed_from is not None:
        logger.info("cloud_bw size changed: target=%s %s -> %s Mbit/s (%s)",
                    _row_subject(row), changed_from, current, decision.note)


async def _record_failure(row, now: datetime, error: CloudBwError) -> None:
    message = str(error)
    async with async_session() as db:
        await db.execute(text("""
            UPDATE cloud_bw_ips SET
                last_check_at = :now, last_error = :error, retry_at = :retry_at,
                evaluated_hour = COALESCE(:hour, evaluated_hour)
            WHERE id = :id
        """), {
            "id": row["id"], "now": now, "error": message,
            # Временный сбой — повтор через RETRY_DELAY, постоянный — в следующем часу
            "retry_at": now + RETRY_DELAY if error.retryable else None,
            "hour": None if error.retryable else _hour_key(now),
        })
        # Повтор той же ошибки каждые 5 минут историю не засоряет
        if message != row["last_error"]:
            await db.execute(text("""
                INSERT INTO cloud_bw_history (ip_id, old_mbps, error) VALUES (:ip_id, :old, :error)
            """), {"ip_id": row["id"], "old": row["current_mbps"], "error": message})
        await db.commit()

    logger.warning("cloud_bw evaluation failed: target=%s retryable=%s error=%s",
                   _row_subject(row), error.retryable, message)


# ═══════════════════════════════════════════════════════════════════════
# SCHEDULER
# ═══════════════════════════════════════════════════════════════════════

_scheduler_task: Optional[asyncio.Task] = None
_last_prune_hour: Optional[int] = None


async def start_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        return
    _scheduler_task = asyncio.create_task(_scheduler_loop(), name="cloud-bw-scheduler")
    logger.info("Cloud.ru bandwidth scheduler started")


async def stop_scheduler() -> None:
    global _scheduler_task, _http
    if _scheduler_task:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
        _scheduler_task = None
    if _http is not None:
        await _http.aclose()
        _http = None


async def _scheduler_loop() -> None:
    while True:
        try:
            await _tick()
        except Exception:
            logger.exception("Cloud.ru bandwidth scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)


async def _tick() -> None:
    global _last_prune_hour
    await _ensure_tables()
    now = datetime.now(timezone.utc)
    hour = _hour_key(now)

    async with async_session() as db:
        due_ids = (await db.execute(text("""
            SELECT id FROM cloud_bw_ips
            WHERE mode <> :off
              AND (evaluated_hour IS NULL OR evaluated_hour <> :hour)
              AND (retry_at IS NULL OR retry_at <= :now)
            ORDER BY id
        """), {"off": Mode.OFF.value, "hour": hour, "now": now})).scalars().all()

        if _last_prune_hour != hour:
            await db.execute(text("""
                DELETE FROM cloud_bw_history
                WHERE created_at < :change_cutoff OR (new_mbps IS NULL AND created_at < :error_cutoff)
            """), {"change_cutoff": now - CHANGE_HISTORY_RETENTION, "error_cutoff": now - ERROR_HISTORY_RETENTION})
            await db.commit()
            _last_prune_hour = hour

    for index, ip_id in enumerate(due_ids):
        if index:
            await asyncio.sleep(API_GAP_SECONDS)
        try:
            await evaluate_ip(ip_id)
        except Exception:
            logger.exception("cloud_bw evaluation crashed: ip_id=%s", ip_id)


# ═══════════════════════════════════════════════════════════════════════
# SERVICE (API)
# ═══════════════════════════════════════════════════════════════════════

class CloudBwService:

    # ── ACCOUNTS ──

    @staticmethod
    async def list_accounts(db: AsyncSession) -> list[dict[str, Any]]:
        await _ensure_tables()
        rows = (await db.execute(text(ACCOUNT_SELECT + " ORDER BY a.id"))).mappings().all()
        return [_account_public(row) for row in rows]

    @staticmethod
    async def _load_account(db: AsyncSession, account_id: int):
        await _ensure_tables()
        row = (await db.execute(text(ACCOUNT_SELECT + " WHERE a.id = :id"), {"id": account_id})).mappings().one_or_none()
        if row is None:
            raise CloudBwNotFound("Аккаунт не найден")
        return row

    @staticmethod
    async def create_account(db: AsyncSession, label: str, key_id: str, key_secret: str, project_id: str) -> dict[str, Any]:
        await _ensure_tables()
        # Ключ проверяется до записи: аккаунт с нерабочим ключом в списке не нужен
        await CloudBwClient(_http_client(), key_id, key_secret, project_id).list_publicips()
        account_id = (await db.execute(text("""
            INSERT INTO cloud_bw_accounts (label, key_id, key_secret, project_id)
            VALUES (:label, :key_id, :key_secret, :project_id) RETURNING id
        """), {
            "label": label, "key_id": key_id, "key_secret": encrypt_secret(key_secret), "project_id": project_id,
        })).scalar_one()
        await db.commit()
        logger.info("cloud_bw account created: account_id=%s project_id=%s", account_id, project_id)
        return _account_public(await CloudBwService._load_account(db, account_id))

    @staticmethod
    async def update_account(db: AsyncSession, account_id: int, changes: dict[str, str]) -> dict[str, Any]:
        row = await CloudBwService._load_account(db, account_id)
        key_id = changes.get("key_id") or row["key_id"]
        project_id = changes.get("project_id") or row["project_id"]
        key_secret = changes.get("key_secret") or decrypt_secret(row["key_secret"])
        if not key_secret:
            raise CloudBwError("SK аккаунта не расшифровывается — введите его заново")
        await db.commit()

        if {"key_id", "key_secret", "project_id"} & {k for k, v in changes.items() if v}:
            await CloudBwClient(_http_client(), key_id, key_secret, project_id).list_publicips()

        await db.execute(text("""
            UPDATE cloud_bw_accounts SET label = :label, key_id = :key_id, key_secret = :key_secret,
                project_id = :project_id
            WHERE id = :id
        """), {
            "id": account_id, "label": changes.get("label", row["label"]), "key_id": key_id,
            "key_secret": encrypt_secret(key_secret), "project_id": project_id,
        })
        await db.commit()
        return _account_public(await CloudBwService._load_account(db, account_id))

    @staticmethod
    async def delete_account(db: AsyncSession, account_id: int) -> None:
        await _ensure_tables()
        result = await db.execute(text("DELETE FROM cloud_bw_accounts WHERE id = :id"), {"id": account_id})
        await db.commit()
        if not result.rowcount:
            raise CloudBwNotFound("Аккаунт не найден")
        logger.info("cloud_bw account deleted: account_id=%s", account_id)

    # ── CLOUD EIPS ──

    @staticmethod
    async def list_cloud_eips(db: AsyncSession, account_id: int) -> list[dict[str, Any]]:
        """Публичные IP проекта: какие уже под автоматикой и к какой ноде адрес относится.
        Адреса общей полосы страница собирает в одну строку по bandwidth_id."""
        account = await CloudBwService._load_account(db, account_id)
        added_rows = (await db.execute(text("SELECT eip_id, bandwidth_id, shared FROM cloud_bw_ips"))).all()
        added_eips = {row.eip_id for row in added_rows if not row.shared}
        added_shared = {row.bandwidth_id for row in added_rows if row.shared}
        owners = await load_ip_owners(db)
        await db.commit()

        client = _cloud_client(account["key_id"], account["key_secret"], account["project_id"])
        result = []
        for eip in await client.list_publicips():
            ip_address = eip.get("public_ip_address") or ""
            owner = owners.get(ip_address)
            shared = eip.get("bandwidth_share_type") == ShareType.SHARED.value
            result.append({
                "eip_id": eip["id"],
                "ip_address": ip_address,
                "bandwidth_id": eip.get("bandwidth_id"),
                "bandwidth_name": eip.get("bandwidth_name"),
                "bandwidth_mbps": eip.get("bandwidth_size"),
                "share_type": eip.get("bandwidth_share_type"),
                "status": eip.get("status"),
                "attached": bool(eip.get("port_id")),
                "added": eip.get("bandwidth_id") in added_shared if shared else eip["id"] in added_eips,
                "suggested_server_id": owner.id if owner else None,
                "suggested_server_name": owner.name if owner else None,
            })
        return sorted(result, key=lambda item: item["ip_address"])

    # ── IPS ──

    @staticmethod
    async def list_ips(db: AsyncSession) -> list[dict[str, Any]]:
        await _ensure_tables()
        rows = (await db.execute(text(IP_SELECT + " ORDER BY i.id"))).mappings().all()
        return [_ip_public(row) for row in rows]

    @staticmethod
    async def get_ip(db: AsyncSession, ip_id: int) -> dict[str, Any]:
        await _ensure_tables()
        row = (await db.execute(text(IP_SELECT + " WHERE i.id = :id"), {"id": ip_id})).mappings().one_or_none()
        if row is None:
            raise CloudBwNotFound("Адрес не найден")
        return _ip_public(row)

    @staticmethod
    async def _ensure_servers_exist(db: AsyncSession, server_ids: list[int]) -> None:
        if not server_ids:
            return
        found = (await db.execute(
            text("SELECT COUNT(*) FROM servers WHERE id = ANY(:server_ids)").bindparams(_server_ids_param()),
            {"server_ids": server_ids},
        )).scalar()
        if found < len(set(server_ids)):
            raise CloudBwError("Нода не найдена")

    @staticmethod
    async def add_ip(db: AsyncSession, account_id: int, eip_id: str, server_id: Optional[int]) -> dict[str, Any]:
        account = await CloudBwService._load_account(db, account_id)
        await CloudBwService._ensure_servers_exist(db, [server_id] if server_id is not None else [])
        await db.commit()

        client = _cloud_client(account["key_id"], account["key_secret"], account["project_id"])
        publicip = await client.get_publicip(eip_id)
        bandwidth = await client.get_bandwidth(publicip["bandwidth_id"])
        _ensure_eligible(bandwidth, shared=False)

        ip_id = (await db.execute(text("""
            INSERT INTO cloud_bw_ips (account_id, eip_id, ip_address, bandwidth_id, server_id,
                                      current_mbps, charge_mode, share_type, last_check_at)
            VALUES (:account_id, :eip_id, :ip_address, :bandwidth_id, :server_id,
                    :current, :charge_mode, :share_type, now())
            ON CONFLICT (eip_id) DO NOTHING
            RETURNING id
        """), {
            "account_id": account_id, "eip_id": eip_id, "ip_address": publicip.get("public_ip_address") or "",
            "bandwidth_id": bandwidth["id"], "server_id": server_id, "current": bandwidth.get("size"),
            "charge_mode": bandwidth.get("charge_mode"), "share_type": bandwidth.get("share_type"),
        })).scalar()
        await db.commit()
        if ip_id is None:
            raise CloudBwError("Этот адрес уже добавлен")
        logger.info("cloud_bw ip added: ip=%s account_id=%s server_id=%s",
                    publicip.get("public_ip_address"), account_id, server_id)
        return await CloudBwService.get_ip(db, ip_id)

    @staticmethod
    async def add_shared(db: AsyncSession, account_id: int, bandwidth_id: str, server_ids: list[int]) -> dict[str, Any]:
        account = await CloudBwService._load_account(db, account_id)
        server_ids = sorted(set(server_ids))
        await CloudBwService._ensure_servers_exist(db, server_ids)
        await db.commit()

        client = _cloud_client(account["key_id"], account["key_secret"], account["project_id"])
        bandwidth = await _shared_bandwidth(client, bandwidth_id)
        _ensure_eligible(bandwidth, shared=True)

        ip_id = (await db.execute(text("""
            INSERT INTO cloud_bw_ips (account_id, ip_address, bandwidth_id, shared, bandwidth_name, members,
                                      server_ids, current_mbps, charge_mode, share_type, last_check_at)
            VALUES (:account_id, '', :bandwidth_id, true, :bandwidth_name, :members,
                    :server_ids, :current, :charge_mode, :share_type, now())
            ON CONFLICT (bandwidth_id) WHERE shared DO NOTHING
            RETURNING id
        """).bindparams(_server_ids_param()), {
            "account_id": account_id, "bandwidth_id": bandwidth["id"], "bandwidth_name": bandwidth.get("name"),
            "members": json.dumps(_bandwidth_members(bandwidth)), "server_ids": server_ids,
            "current": bandwidth.get("size"), "charge_mode": bandwidth.get("charge_mode"),
            "share_type": bandwidth.get("share_type"),
        })).scalar()
        await db.commit()
        if ip_id is None:
            raise CloudBwError("Эта общая полоса уже добавлена")
        logger.info("cloud_bw shared bandwidth added: bandwidth_id=%s account_id=%s server_ids=%s",
                    bandwidth["id"], account_id, server_ids)
        return await CloudBwService.get_ip(db, ip_id)

    @staticmethod
    async def update_ip(db: AsyncSession, ip_id: int, config: dict[str, Any]) -> None:
        """Сохранить правило записи. Проверка ждёт evaluate_ip — её вызывает роутер после commit.
        Выделенной полосе — одна нода в server_id, общей — список server_ids."""
        shared = (await CloudBwService.get_ip(db, ip_id))["shared"]
        mode = Mode(config["mode"])
        if shared:
            server_id, server_ids = None, sorted(set(config.get("server_ids") or []))
            bound_nodes = server_ids
        else:
            server_id, server_ids = config.get("server_id"), []
            bound_nodes = [server_id] if server_id is not None else []
        await CloudBwService._ensure_servers_exist(db, bound_nodes)

        rules = parse_schedule(config.get("schedule") or [])
        dynamic = DynamicSettings(
            window_hours=config["dyn_window_hours"],
            reserve_percent=config["dyn_reserve_percent"],
            interval_hours=config["dyn_interval_hours"],
            min_mbps=config["dyn_min_mbps"],
            max_mbps=config["dyn_max_mbps"],
        )
        if mode == Mode.SCHEDULE:
            validate_schedule(rules, shared)
        if mode == Mode.DYNAMIC:
            if not bound_nodes:
                raise CloudBwError(DYNAMIC_NEEDS_SHARED_NODES if shared else DYNAMIC_NEEDS_NODE)
            validate_dynamic(dynamic, shared)

        await db.execute(text("""
            UPDATE cloud_bw_ips SET
                server_id = :server_id, server_ids = :server_ids, mode = :mode, schedule = :schedule,
                dyn_window_hours = :window, dyn_reserve_percent = :reserve, dyn_interval_hours = :interval,
                dyn_min_mbps = :min_mbps, dyn_max_mbps = :max_mbps,
                evaluated_hour = NULL, retry_at = NULL, dyn_next_at = NULL, last_error = NULL,
                last_note = COALESCE(:off_note, last_note)
            WHERE id = :id
        """).bindparams(_server_ids_param()), {
            "id": ip_id, "server_id": server_id, "server_ids": server_ids, "mode": mode.value,
            "off_note": "Автоматика выключена" if mode == Mode.OFF else None,
            "schedule": json.dumps([asdict(rule) for rule in rules]),
            "window": dynamic.window_hours, "reserve": dynamic.reserve_percent,
            "interval": dynamic.interval_hours, "min_mbps": dynamic.min_mbps, "max_mbps": dynamic.max_mbps,
        })
        await db.commit()
        logger.info("cloud_bw rule saved: ip_id=%s mode=%s nodes=%s", ip_id, mode.value, bound_nodes)

    @staticmethod
    async def delete_ip(db: AsyncSession, ip_id: int) -> None:
        await _ensure_tables()
        result = await db.execute(text("DELETE FROM cloud_bw_ips WHERE id = :id"), {"id": ip_id})
        await db.commit()
        if not result.rowcount:
            raise CloudBwNotFound("Адрес не найден")
        _ip_locks.pop(ip_id, None)
        logger.info("cloud_bw ip deleted: ip_id=%s", ip_id)

    @staticmethod
    async def get_history(db: AsyncSession, ip_id: int, limit: int) -> list[dict[str, Any]]:
        await _ensure_tables()
        rows = (await db.execute(text("""
            SELECT id, created_at, old_mbps, new_mbps, note, error FROM cloud_bw_history
            WHERE ip_id = :ip_id ORDER BY id DESC LIMIT :limit
        """), {"ip_id": ip_id, "limit": limit})).mappings().all()
        return [{**row, "created_at": _iso(row["created_at"])} for row in rows]

    @staticmethod
    async def get_usage(db: AsyncSession, period: UsagePeriod) -> dict[str, Any]:
        """Средняя ширина и оплата каждой записи за период — по истории смен размера.
        Считается только время, пока запись в разделе: до добавления размер панели неизвестен."""
        await _ensure_tables()
        now = datetime.now(timezone.utc)
        start, end = usage_period_bounds(period, now)
        rows = (await db.execute(text(IP_SELECT + " ORDER BY i.id"))).mappings().all()
        changes = await _load_size_changes(db, start, end)

        items, usages = [], []
        for row in rows:
            segments = size_segments(changes.get(row["id"], []), row["current_mbps"],
                                     max(start, row["created_at"]), end)
            usage = summarize_usage(segments, row["shared"], _eip_count(row))
            usages.append(usage)
            items.append(_usage_public(row, usage))

        return {
            "period": period.value,
            "start": _iso(start),
            "end": _iso(end),
            "hours": round((end - start).total_seconds() / SECONDS_PER_HOUR, 2),
            "items": items,
            "total": _usage_total(usages),
            "tariff": TARIFF_PUBLIC,
        }
