"""Облачные биллинг-провайдеры: реестр и единая синхронизация баланса.

Роутер и фоновый чекер зовут только `sync_cloud_balance` — вся арифметика срока
оплаты живёт здесь, поэтому новый провайдер добавляется одним классом.
"""
import logging
from datetime import datetime, timedelta

from app.services.cloud_billing.base import (
    CloudAuthError,
    CloudBillingError,
    CloudProvider,
    CloudSnapshot,
    compute_days_left,
)
from app.services.cloud_billing.selectel import SelectelProvider
from app.services.cloud_billing.yandex import YandexCloudProvider

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, CloudProvider] = {
    p.id: p for p in (YandexCloudProvider(), SelectelProvider())
}

__all__ = [
    "CloudAuthError",
    "CloudBillingError",
    "CloudProvider",
    "CloudSnapshot",
    "PROVIDERS",
    "compute_days_left",
    "get_provider",
    "sync_cloud_balance",
]


def get_provider(provider_id: str | None) -> CloudProvider:
    provider = PROVIDERS.get(provider_id or "")
    if provider is None:
        raise CloudBillingError(f"Unknown cloud provider: {provider_id}")
    return provider


async def sync_cloud_balance(server, now: datetime) -> CloudSnapshot:
    """Обновить у сервера баланс, расход и срок оплаты. Коммит — на вызывающем."""
    provider = get_provider(server.cloud_provider)

    if not server.cloud_credential:
        raise CloudBillingError("API token is required")
    if provider.requires_account_id and not server.cloud_account_id:
        raise CloudBillingError("Billing account ID is required")

    try:
        snapshot = await provider.fetch(server.cloud_credential, server.cloud_account_id)
    except CloudBillingError as e:
        server.cloud_last_error = str(e)[:500]
        logger.warning("Cloud sync failed for '%s' (%s): %s", server.name, provider.id, e)
        raise

    _apply_snapshot(server, snapshot, now)
    return snapshot


def _apply_snapshot(server, snapshot: CloudSnapshot, now: datetime) -> None:
    server.account_balance = snapshot.balance
    server.balance_updated_at = now
    server.currency = snapshot.currency
    server.cloud_last_sync_at = now
    server.cloud_last_error = snapshot.warning[:500] if snapshot.warning else None

    daily_cost = snapshot.daily_cost
    if daily_cost is None:
        daily_cost = _daily_cost_from_forecast(snapshot)
    if daily_cost is not None:
        server.cloud_daily_cost = daily_cost

    threshold = server.cloud_balance_threshold or 0
    days_left = compute_days_left(snapshot.balance, threshold, server.cloud_daily_cost)
    if days_left is None:
        server.paid_until = None
        return

    server.paid_until = now + timedelta(days=days_left)
    server.monthly_cost = server.cloud_daily_cost * 30


def _daily_cost_from_forecast(snapshot: CloudSnapshot) -> float | None:
    """Провайдер отдал готовый прогноз вместо расхода (Selectel) — приводим к
    дневному расходу, чтобы порог остатка и калькулятор пополнения работали одинаково."""
    if not snapshot.days_left or snapshot.days_left <= 0 or snapshot.balance <= 0:
        return None
    return round(snapshot.balance / snapshot.days_left, 4)
