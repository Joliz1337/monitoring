"""Yandex Cloud: остаток по REST, средний дневной расход по gRPC.

Готового Python-SDK биллинга в зависимостях панели нет, а тянуть его ради одного
метода — лишний вес, поэтому запрос отчёта потребления сериализуется в protobuf
вручную по схеме consumption_core_service.proto.
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

import grpc

from app.services.cloud_billing.base import (
    CloudAuthError,
    CloudBillingError,
    CloudProvider,
    CloudSnapshot,
)
from app.services.http_client import get_external_client
from app.services.yc_token_manager import YCTokenError, get_yc_token_manager

logger = logging.getLogger(__name__)

YC_BILLING_BASE = "https://billing.api.cloud.yandex.net/billing/v1"
YC_GRPC_HOST = "billing.api.cloud.yandex.net:443"
YC_USAGE_METHOD = (
    "/yandex.cloud.billing.usage_records.v1.ConsumptionCoreService"
    "/GetBillingAccountUsageReport"
)
CONSUMPTION_WINDOW_DAYS = 3

_grpc_pool = ThreadPoolExecutor(max_workers=2)


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
    raw = value.encode("utf-8")
    return _varint(field << 3 | 2) + _varint(len(raw)) + raw


def _pb_submessage(field: int, inner: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(inner)) + inner


def _pb_varint_field(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _pb_timestamp(field: int, dt: datetime) -> bytes:
    inner = _pb_varint_field(1, int(dt.timestamp()))  # Timestamp.seconds = field 1
    return _pb_submessage(field, inner)


def _build_usage_request(account_id: str, start: datetime, end: datetime) -> bytes:
    msg = _pb_string(1, account_id)
    msg += _pb_timestamp(2, start)
    msg += _pb_timestamp(3, end)
    msg += _pb_varint_field(10, 1)  # TimeGrouping.DAY = 1
    return msg


def _extract_expense(data: bytes) -> Optional[str]:
    """expense (field 4) → StringDecimal.value (field 1)."""
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
    """field 1 (string) из StringDecimal submessage."""
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


def _sync_fetch_consumption(
    iam_token: str,
    account_id: str,
    start_seconds: int,
    end_seconds: int,
) -> Optional[str]:
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


class YandexCloudProvider(CloudProvider):
    id = "yandex_cloud"
    default_currency = "RUB"
    requires_account_id = True

    async def fetch(self, credential: str, account_id: Optional[str]) -> CloudSnapshot:
        if not account_id:
            raise CloudBillingError("Billing account ID is required")

        iam_token = await self._iam_token(credential)
        balance, currency = await self._fetch_balance(iam_token, account_id)
        daily_cost, warning = await self._fetch_daily_cost(iam_token, account_id)

        return CloudSnapshot(
            balance=balance,
            currency=currency,
            daily_cost=daily_cost,
            warning=warning,
        )

    async def _iam_token(self, oauth_token: str) -> str:
        try:
            return await get_yc_token_manager().get_iam_token(oauth_token)
        except YCTokenError as e:
            raise CloudAuthError(str(e)) from e

    async def _fetch_balance(self, iam_token: str, account_id: str) -> tuple[float, str]:
        url = f"{YC_BILLING_BASE}/billingAccounts/{account_id}"
        try:
            resp = await get_external_client().get(
                url,
                headers={"Authorization": f"Bearer {iam_token}"},
                timeout=15.0,
            )
        except Exception as e:
            logger.warning("YC balance request failed for %s: %s", account_id, e)
            raise CloudBillingError(str(e)) from e

        if resp.status_code == 401:
            raise CloudAuthError("Auth failed: invalid or expired IAM token")
        if resp.status_code == 403:
            raise CloudAuthError("Forbidden: need billing.accounts.viewer role")
        if resp.status_code == 404:
            raise CloudBillingError(f"Billing account {account_id} not found")
        if resp.status_code != 200:
            raise CloudBillingError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        return float(data.get("balance", "0")), data.get("currency") or self.default_currency

    async def _fetch_daily_cost(
        self, iam_token: str, account_id: str
    ) -> tuple[Optional[float], Optional[str]]:
        """Средний расход в сутки за окно потребления; ошибка здесь не фатальна —
        баланс уже получен, без расхода теряется только прогноз."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=CONSUMPTION_WINDOW_DAYS)

        try:
            expense_str = await asyncio.get_event_loop().run_in_executor(
                _grpc_pool,
                _sync_fetch_consumption,
                iam_token,
                account_id,
                int(start.timestamp()),
                int(now.timestamp()),
            )
        except grpc.RpcError as e:
            msg = f"gRPC {e.code()}: {e.details()}"
            logger.warning("YC consumption API failed for %s: %s", account_id, msg)
            return None, msg
        except Exception as e:
            logger.error("YC consumption error for %s: %s", account_id, e)
            return None, str(e)

        if expense_str is None:
            return None, "No expense data in response"

        total = abs(float(expense_str))
        if total <= 0:
            return None, None

        return round(total / CONSUMPTION_WINDOW_DAYS, 4), None
