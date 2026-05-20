import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

import grpc

from app.services.http_client import get_external_client

logger = logging.getLogger(__name__)

YC_BILLING_BASE = "https://billing.api.cloud.yandex.net/billing/v1"
YC_GRPC_HOST = "billing.api.cloud.yandex.net:443"
YC_USAGE_METHOD = (
    "/yandex.cloud.billing.usage_records.v1.ConsumptionCoreService"
    "/GetBillingAccountUsageReport"
)

_grpc_pool = ThreadPoolExecutor(max_workers=2)


# ── REST: баланс ──────────────────────────────────────────────────


async def fetch_yc_balance(
    iam_token: str,
    billing_account_id: str,
) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """(balance, currency, error) — None/None/msg при ошибке."""
    url = f"{YC_BILLING_BASE}/billingAccounts/{billing_account_id}"
    headers = {"Authorization": f"Bearer {iam_token}"}

    try:
        client = get_external_client()
        resp = await client.get(url, headers=headers, timeout=15.0)

        if resp.status_code == 401:
            return None, None, "Auth failed: invalid or expired IAM token"
        if resp.status_code == 403:
            return None, None, "Forbidden: need billing.accounts.viewer role"
        if resp.status_code == 404:
            return None, None, f"Billing account {billing_account_id} not found"
        if resp.status_code != 200:
            return None, None, f"HTTP {resp.status_code}: {resp.text[:200]}"

        data = resp.json()
        balance = float(data.get("balance", "0"))
        currency = data.get("currency", "RUB")
        return balance, currency, None

    except Exception as e:
        logger.error(f"YC billing API error for {billing_account_id}: {e}")
        return None, None, str(e)


# ── Protobuf: ручная сериализация ─────────────────────────────────
#
# UsageReportRequest (consumption_core_service.proto):
#   field 1  = billing_account_id (string)
#   field 2  = start_date         (google.protobuf.Timestamp)
#   field 3  = end_date           (google.protobuf.Timestamp)
#   field 10 = aggregation_period (TimeGrouping enum, DAY=1)
#
# BillingAccountUsageReportResponse (consumption_core_service.proto):
#   field 1  = currency        (Currency enum)
#   field 2  = cost            (StringDecimal)
#   field 3  = credit_details  (CreditDetails)
#   field 4  = expense         (StringDecimal)
#
# StringDecimal (common_types.proto):
#   field 1  = value (string)
#
# google.protobuf.Timestamp:
#   field 1  = seconds (int64)


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
    """Encode string field (wire type 2)."""
    raw = value.encode("utf-8")
    return _varint(field << 3 | 2) + _varint(len(raw)) + raw


def _pb_submessage(field: int, inner: bytes) -> bytes:
    """Encode submessage field (wire type 2)."""
    return _varint(field << 3 | 2) + _varint(len(inner)) + inner


def _pb_varint_field(field: int, value: int) -> bytes:
    """Encode varint field (wire type 0)."""
    return _varint(field << 3) + _varint(value)


def _pb_timestamp(field: int, dt: datetime) -> bytes:
    """Encode google.protobuf.Timestamp as submessage."""
    secs = int(dt.timestamp())
    inner = _pb_varint_field(1, secs)  # Timestamp.seconds = field 1
    return _pb_submessage(field, inner)


def _build_usage_request(account_id: str, start: datetime, end: datetime) -> bytes:
    """UsageReportRequest: account_id(1) + start_date(2) + end_date(3) + aggregation_period(10)=DAY(1)."""
    msg = _pb_string(1, account_id)
    msg += _pb_timestamp(2, start)
    msg += _pb_timestamp(3, end)
    msg += _pb_varint_field(10, 1)  # TimeGrouping.DAY = 1
    return msg


def _extract_expense(data: bytes) -> Optional[str]:
    """Извлечь expense (field 4) → StringDecimal.value (field 1) из response."""
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        fn = tag >> 3
        wt = tag & 7

        if wt == 0:
            _, pos = _read_varint(data, pos)
        elif wt == 2:
            length, pos = _read_varint(data, pos)
            payload = data[pos:pos + length]
            if fn == 4:
                val = _extract_string_value(payload)
                if val is not None:
                    try:
                        float(val)
                        return val
                    except ValueError:
                        pass
            pos += length
        elif wt == 1:
            pos += 8
        elif wt == 5:
            pos += 4
        else:
            break
    return None


def _extract_string_value(data: bytes) -> Optional[str]:
    """Извлечь field 1 (string) из StringDecimal submessage."""
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        fn = tag >> 3
        wt = tag & 7

        if wt == 0:
            _, pos = _read_varint(data, pos)
        elif wt == 2:
            length, pos = _read_varint(data, pos)
            if fn == 1:
                return data[pos:pos + length].decode("utf-8")
            pos += length
        elif wt == 1:
            pos += 8
        elif wt == 5:
            pos += 4
        else:
            break
    return None


# ── gRPC: потребление за последние N дней ─────────────────────────


def _sync_fetch_consumption(
    iam_token: str,
    account_id: str,
    start_seconds: int,
    end_seconds: int,
) -> Optional[str]:
    """Синхронный gRPC вызов → строка expense."""
    creds = grpc.composite_channel_credentials(
        grpc.ssl_channel_credentials(),
        grpc.access_token_call_credentials(iam_token),
    )
    channel = grpc.secure_channel(YC_GRPC_HOST, creds)
    try:
        method = channel.unary_unary(
            YC_USAGE_METHOD,
            request_serializer=lambda x: x,
            response_deserializer=lambda x: x,
        )
        start_dt = datetime.fromtimestamp(start_seconds, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(end_seconds, tz=timezone.utc)
        request = _build_usage_request(account_id, start_dt, end_dt)
        response: bytes = method(request, timeout=15)
        return _extract_expense(response)
    finally:
        channel.close()


async def fetch_yc_daily_cost(
    iam_token: str,
    billing_account_id: str,
    days: int = 3,
) -> tuple[Optional[float], Optional[str]]:
    """Средний дневной расход за последние N дней через gRPC.

    Returns: (daily_cost, error)
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    loop = asyncio.get_event_loop()
    try:
        expense_str = await loop.run_in_executor(
            _grpc_pool,
            _sync_fetch_consumption,
            iam_token,
            billing_account_id,
            int(start.timestamp()),
            int(now.timestamp()),
        )
        if expense_str is None:
            return None, "No expense data in response"

        total = abs(float(expense_str))
        if total <= 0:
            return None, None

        daily = round(total / days, 4)
        return daily, None

    except grpc.RpcError as e:
        msg = f"gRPC {e.code()}: {e.details()}"
        logger.warning(f"YC consumption API failed for {billing_account_id}: {msg}")
        return None, msg
    except Exception as e:
        logger.error(f"YC consumption error for {billing_account_id}: {e}")
        return None, str(e)


# ── Расчёты ───────────────────────────────────────────────────────


def compute_yc_days_left(
    balance: float,
    threshold: float,
    daily_cost: Optional[float],
) -> Optional[float]:
    """days_left = (balance - threshold) / daily_cost."""
    if daily_cost is None or daily_cost <= 0:
        return None

    usable = balance - threshold
    if usable <= 0:
        return 0.0

    return round(usable / daily_cost, 1)
