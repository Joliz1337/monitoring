"""Yandex Cloud: остаток по REST, средний дневной расход по gRPC-отчёту о потреблении.

Вход — авторизованный ключ сервисного аккаунта с ролью billing.accounts.viewer
на платёжном аккаунте (или ещё не истёкший OAuth-токен), обмен на IAM-токен —
в yc_token_manager.

Готового Python-SDK биллинга в зависимостях панели нет, а тянуть его ради одного
метода — лишний вес, поэтому запрос отчёта потребления сериализуется в protobuf
вручную по схеме consumption_core_service.proto. Унарный gRPC-вызов идёт
обычным HTTP/2-запросом через тот же httpx-клиент, что и REST: grpcio не умеет
SOCKS5, а у проекта может быть задан прокси.

Отчёт посуточный: время в границах периода Yandex отбрасывает и включает обе
даты целиком, поэтому окно — последние закрытые сутки UTC, а среднее считается
по дням, за которые в отчёте есть списания. Отдаёт отчёт Yandex не чаще раза
в минуту с одного IP — очередь и кэш в UsageReportScheduler.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional
from urllib.parse import unquote

import httpx

from app.services.cloud_billing.base import (
    CloudAuthError,
    CloudBillingError,
    CloudProvider,
    CloudSnapshot,
    describe_request_error,
)
from app.services.yc_token_manager import YCTokenError, get_yc_token_manager

logger = logging.getLogger(__name__)

YC_BILLING_BASE = "https://billing.api.cloud.yandex.net/billing/v1"
YC_USAGE_URL = (
    "https://billing.api.cloud.yandex.net"
    "/yandex.cloud.billing.usage_records.v1.ConsumptionCoreService"
    "/GetBillingAccountUsageReport"
)
CONSUMPTION_WINDOW_DAYS = 3
BALANCE_TIMEOUT = 15.0
CONSUMPTION_TIMEOUT = 15.0

# Лимит ConsumptionCoreService — запрос в минуту с IP; запас на то, что окно
# лимита у Yandex отсчитывается не от наших часов
REPORT_RATE_INTERVAL = 65.0
# Кнопка «Обновить» в интерфейсе сдаётся через 30 с — дольше отчёт синхронизация
# не ждёт, он дожидается своей очереди в фоне
REPORT_WAIT_LIMIT = 20.0
# Закрытые сутки Yandex ещё досчитывает задним числом, поэтому кэш живёт
# несколько часов, а не до смены даты
REPORT_CACHE_TTL = 6 * 3600.0
SECONDS_PER_DAY = 86400

# Коды google.rpc.Code по порядку: индекс = числовой grpc-status
GRPC_STATUS_NAMES = (
    "OK", "CANCELLED", "UNKNOWN", "INVALID_ARGUMENT", "DEADLINE_EXCEEDED",
    "NOT_FOUND", "ALREADY_EXISTS", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED",
    "FAILED_PRECONDITION", "ABORTED", "OUT_OF_RANGE", "UNIMPLEMENTED",
    "INTERNAL", "UNAVAILABLE", "DATA_LOSS", "UNAUTHENTICATED",
)
# Сообщение gRPC: флаг сжатия (1 байт) + длина (4 байта, big-endian) + protobuf
GRPC_FRAME_HEADER_SIZE = 5


# ── Protobuf: ручная сериализация ─────────────────────────────────
#
# UsageReportRequest (consumption_core_service.proto):
#   field 1  = billing_account_id (string)
#   field 2  = start_date         (google.protobuf.Timestamp)
#   field 3  = end_date           (google.protobuf.Timestamp)
#   field 10 = aggregation_period (TimeGrouping enum, DAY=1)
#
# BillingAccountUsageReportResponse (consumption_core_service.proto):
#   field 4  = expense         (StringDecimal) — итог за весь период
#   field 5  = entities_data   (repeated BillingAccountUsageReportEntityData)
#
# BillingAccountUsageReportEntityData (consumption_core.proto):
#   field 5  = periodic        (repeated UsageReportPeriodicData)
#
# UsageReportPeriodicData (consumption_core.proto):
#   field 3  = expense         (StringDecimal)
#   field 4  = timestamp       (google.protobuf.Timestamp, начало суток)
#
# StringDecimal (common_types.proto):
#   field 1  = value (string)
#
# google.protobuf.Timestamp:
#   field 1  = seconds (int64)

WIRE_VARINT, WIRE_FIXED64, WIRE_LEN, WIRE_FIXED32 = 0, 1, 2, 5

REPORT_EXPENSE = 4
REPORT_ENTITIES = 5
ENTITY_PERIODIC = 5
PERIOD_EXPENSE = 3
PERIOD_TIMESTAMP = 4


def _varint(value: int) -> bytes:
    buf = bytearray()
    while value > 0x7F:
        buf.append(0x80 | (value & 0x7F))
        value >>= 7
    buf.append(value & 0x7F)
    return bytes(buf)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _pb_string(field: int, value: str) -> bytes:
    raw = value.encode("utf-8")
    return _varint(field << 3 | WIRE_LEN) + _varint(len(raw)) + raw


def _pb_submessage(field: int, inner: bytes) -> bytes:
    return _varint(field << 3 | WIRE_LEN) + _varint(len(inner)) + inner


def _pb_varint_field(field: int, value: int) -> bytes:
    return _varint(field << 3 | WIRE_VARINT) + _varint(value)


def _pb_timestamp(field: int, dt: datetime) -> bytes:
    inner = _pb_varint_field(1, int(dt.timestamp()))  # Timestamp.seconds = field 1
    return _pb_submessage(field, inner)


def _build_usage_request(account_id: str, start: datetime, end: datetime) -> bytes:
    msg = _pb_string(1, account_id)
    msg += _pb_timestamp(2, start)
    msg += _pb_timestamp(3, end)
    msg += _pb_varint_field(10, 1)  # TimeGrouping.DAY = 1
    return msg


def _pb_fields(data: bytes) -> Iterator[tuple[int, int | bytes]]:
    """Поля сообщения по порядку: varint — числом, length-delimited — байтами.
    fixed32/fixed64 отчёту не нужны и пропускаются."""
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field, wire_type = tag >> 3, tag & 7
        if wire_type == WIRE_VARINT:
            value, pos = _read_varint(data, pos)
            yield field, value
        elif wire_type == WIRE_LEN:
            length, pos = _read_varint(data, pos)
            yield field, data[pos:pos + length]
            pos += length
        elif wire_type == WIRE_FIXED64:
            pos += 8
        elif wire_type == WIRE_FIXED32:
            pos += 4
        else:
            raise CloudBillingError("Malformed usage report")


def _pb_field(data: bytes, number: int) -> int | bytes | None:
    return next((value for field, value in _pb_fields(data) if field == number), None)


def _pb_messages(data: bytes, number: int) -> Iterator[bytes]:
    for field, value in _pb_fields(data):
        if field == number and isinstance(value, bytes):
            yield value


def _pb_decimal(message: int | bytes | None) -> Optional[float]:
    """StringDecimal → число; None, если поля нет или в нём не число."""
    raw = _pb_field(message, 1) if isinstance(message, bytes) else None
    if not isinstance(raw, bytes):
        return None
    try:
        return float(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _daily_expenses(report: bytes) -> dict[int, float]:
    """Списания по суткам UTC из entities_data[].periodic[]; ключ — номер суток от эпохи."""
    by_day: dict[int, float] = {}
    for entity in _pb_messages(report, REPORT_ENTITIES):
        for period in _pb_messages(entity, ENTITY_PERIODIC):
            expense = _pb_decimal(_pb_field(period, PERIOD_EXPENSE))
            timestamp = _pb_field(period, PERIOD_TIMESTAMP)
            if expense is None or not isinstance(timestamp, bytes):
                continue
            seconds = _pb_field(timestamp, 1)
            day = (seconds if isinstance(seconds, int) else 0) // SECONDS_PER_DAY
            by_day[day] = by_day.get(day, 0.0) + abs(expense)
    return by_day


def _average_daily_expense(report: bytes) -> Optional[float]:
    """Средний расход за сутки со списаниями.

    Делим на число таких суток, а не на всё окно: у аккаунта, созданного вчера,
    деление на три занизило бы расход втрое. Сутки без списаний (до создания
    аккаунта или покрытые грантом) в среднее не входят."""
    spent = [value for value in _daily_expenses(report).values() if value > 0]
    if spent:
        return round(sum(spent) / len(spent), 4)

    # Разбивки по дням в ответе нет — итог периода делится на всё окно
    total = _pb_decimal(_pb_field(report, REPORT_EXPENSE))
    if total:
        return round(abs(total) / CONSUMPTION_WINDOW_DAYS, 4)
    return None


def _report_window(now: datetime) -> tuple[datetime, datetime]:
    """Последние закрытые сутки UTC, обе границы включительно: время Yandex
    отбрасывает, а текущие сутки ещё не досчитаны."""
    day = now.astimezone(timezone.utc).date()
    today = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return today - timedelta(days=CONSUMPTION_WINDOW_DAYS), today - timedelta(days=1)


def _grpc_frame(message: bytes) -> bytes:
    return b"\x00" + len(message).to_bytes(4, "big") + message


def _grpc_unframe(body: bytes) -> bytes:
    if len(body) < GRPC_FRAME_HEADER_SIZE:
        raise CloudBillingError("Empty gRPC response")
    if body[0]:
        raise CloudBillingError("Compressed gRPC response is not supported")
    length = int.from_bytes(body[1:GRPC_FRAME_HEADER_SIZE], "big")
    message = body[GRPC_FRAME_HEADER_SIZE:GRPC_FRAME_HEADER_SIZE + length]
    if len(message) < length:
        raise CloudBillingError("Truncated gRPC response")
    return message


def _grpc_status_error(status: str, message: str) -> CloudBillingError:
    code = int(status) if status.isdigit() else -1
    name = GRPC_STATUS_NAMES[code] if 0 <= code < len(GRPC_STATUS_NAMES) else status
    return CloudBillingError(f"gRPC {name}: {unquote(message)}")


async def _grpc_unary_call(
    client: httpx.AsyncClient, url: str, iam_token: str, message: bytes
) -> bytes:
    try:
        resp = await client.post(
            url,
            content=_grpc_frame(message),
            headers={
                "content-type": "application/grpc",
                "te": "trailers",
                "authorization": f"Bearer {iam_token}",
            },
            timeout=CONSUMPTION_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise CloudBillingError(describe_request_error(e)) from e

    if resp.http_version != "HTTP/2":
        raise CloudBillingError(f"gRPC needs HTTP/2, got {resp.http_version}")
    if resp.status_code != 200:
        raise CloudBillingError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    # Ошибку сервер присылает ответом без тела, grpc-status — в заголовках.
    # Трейлеры успешного ответа httpx не отдаёт, поэтому успех — по наличию тела
    status = resp.headers.get("grpc-status")
    if status is not None and status != "0":
        raise _grpc_status_error(status, resp.headers.get("grpc-message", ""))
    return _grpc_unframe(resp.content)


@dataclass(slots=True)
class _CachedCost:
    window_end: datetime
    fetched_at: float
    daily_cost: Optional[float]


class UsageReportScheduler:
    """Отчёт о потреблении под лимит Yandex — запрос в минуту с IP.

    Запросы через одну точку выхода (общий клиент или клиент своего прокси —
    у каждого свой IP) идут по очереди с интервалом. Синхронизация ждёт отчёт
    не дольше wait_limit: дальше он дожидается очереди в фоне и попадает в кэш
    к следующей синхронизации, а у проекта пока остаётся прежний расход."""

    def __init__(
        self,
        interval: float = REPORT_RATE_INTERVAL,
        wait_limit: float = REPORT_WAIT_LIMIT,
        cache_ttl: float = REPORT_CACHE_TTL,
    ) -> None:
        self._interval = interval
        self._wait_limit = wait_limit
        self._cache_ttl = cache_ttl
        self._next_slot: dict[httpx.AsyncClient, float] = {}
        self._cache: dict[str, _CachedCost] = {}
        self._pending: dict[str, asyncio.Task] = {}

    async def daily_cost(
        self, client: httpx.AsyncClient, iam_token: str, account_id: str, now: datetime
    ) -> tuple[Optional[float], Optional[str]]:
        """Средний расход в сутки и предупреждение. Ошибка не фатальна — баланс
        уже получен, без расхода теряется только прогноз."""
        start, end = _report_window(now)
        cached = self._cache.get(account_id)
        if (
            cached is not None
            and cached.window_end == end
            and time.monotonic() - cached.fetched_at < self._cache_ttl
        ):
            return cached.daily_cost, None

        task = self._pending.get(account_id)
        if task is None:
            task = asyncio.create_task(self._fetch(client, iam_token, account_id, start, end))
            self._pending[account_id] = task
            task.add_done_callback(lambda _: self._pending.pop(account_id, None))

        try:
            return await asyncio.wait_for(asyncio.shield(task), self._wait_limit)
        except asyncio.TimeoutError:
            return None, None

    async def _fetch(
        self,
        client: httpx.AsyncClient,
        iam_token: str,
        account_id: str,
        start: datetime,
        end: datetime,
    ) -> tuple[Optional[float], Optional[str]]:
        await self._wait_turn(client)
        request = _build_usage_request(account_id, start, end)
        try:
            response = await _grpc_unary_call(client, YC_USAGE_URL, iam_token, request)
            daily_cost = _average_daily_expense(response)
        except CloudBillingError as e:
            logger.warning("YC consumption API failed for %s: %s", account_id, e)
            return None, str(e)

        self._cache[account_id] = _CachedCost(end, time.monotonic(), daily_cost)
        return daily_cost, None

    async def _wait_turn(self, client: httpx.AsyncClient) -> None:
        # Слот бронируется до ожидания: следующий запрос через тот же выход
        # встанет за этим, даже если придёт, пока этот ещё спит
        now = time.monotonic()
        slot = max(now, self._next_slot.get(client, now))
        self._next_slot[client] = slot + self._interval
        if slot > now:
            await asyncio.sleep(slot - now)


class YandexCloudProvider(CloudProvider):
    id = "yandex_cloud"
    default_currency = "RUB"
    requires_account_id = True

    def __init__(self, reports: Optional[UsageReportScheduler] = None) -> None:
        self._reports = reports or UsageReportScheduler()

    async def fetch(
        self,
        client: httpx.AsyncClient,
        credential: str,
        account_id: Optional[str],
        login: Optional[str] = None,
    ) -> CloudSnapshot:
        if not account_id:
            raise CloudBillingError("Billing account ID is required")

        iam_token = await self._iam_token(client, credential)
        balance, currency = await self._fetch_balance(client, iam_token, account_id)
        daily_cost, warning = await self._reports.daily_cost(
            client, iam_token, account_id, datetime.now(timezone.utc)
        )

        return CloudSnapshot(
            balance=balance,
            currency=currency,
            daily_cost=daily_cost,
            warning=warning,
        )

    async def _iam_token(self, client: httpx.AsyncClient, credential: str) -> str:
        try:
            return await get_yc_token_manager().get_iam_token(client, credential)
        except YCTokenError as e:
            raise CloudAuthError(str(e)) from e
        except httpx.HTTPError as e:
            raise CloudBillingError(f"IAM token request failed: {describe_request_error(e)}") from e

    async def _fetch_balance(
        self, client: httpx.AsyncClient, iam_token: str, account_id: str
    ) -> tuple[float, str]:
        url = f"{YC_BILLING_BASE}/billingAccounts/{account_id}"
        try:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {iam_token}"},
                timeout=BALANCE_TIMEOUT,
            )
        except Exception as e:
            logger.warning("YC balance request failed for %s: %s", account_id, e)
            raise CloudBillingError(describe_request_error(e)) from e

        if resp.status_code == 401:
            raise CloudAuthError("Auth failed: invalid or expired IAM token")
        if resp.status_code == 403:
            raise CloudAuthError("Forbidden: need billing.accounts.viewer role on the billing account")
        if resp.status_code == 404:
            raise CloudBillingError(f"Billing account {account_id} not found")
        if resp.status_code != 200:
            raise CloudBillingError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        return float(data.get("balance", "0")), data.get("currency") or self.default_currency
