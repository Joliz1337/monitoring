"""Облачные биллинг-провайдеры: реестр и единая синхронизация баланса.

Роутер и фоновый чекер зовут только `sync_cloud_balance` — вся арифметика срока
оплаты живёт здесь, поэтому новый провайдер добавляется одним классом.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from app.services.cloud_billing.base import (
    CloudAuthError,
    CloudBillingError,
    CloudProvider,
    CloudSnapshot,
    compute_days_left,
)
from app.services.cloud_billing.cloud_ru import CloudRuProvider
from app.services.cloud_billing.selectel import SelectelProvider
from app.services.cloud_billing.timeweb import TimewebProvider
from app.services.cloud_billing.vk_cloud import VkCloudProvider
from app.services.cloud_billing.yandex import YandexCloudProvider
from app.services.http_client import get_external_client, sanitize_proxy

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, CloudProvider] = {
    p.id: p
    for p in (
        YandexCloudProvider(),
        SelectelProvider(),
        TimewebProvider(),
        VkCloudProvider(),
        CloudRuProvider(),
    )
}

# История баланса для провайдеров без текущего расхода в API: окно то же, что у окна
# потребления Yandex Cloud, — оценка реагирует на нагрузку за те же 3 дня
HISTORY_WINDOW_DAYS = 3
# Ручные «Обновить» чаще, чем раз в 15 минут, двигают последнюю точку,
# а не плодят новые — история остаётся примерно почасовой
HISTORY_MIN_GAP = timedelta(minutes=15)
# Пока покрытых списаниями интервалов меньше 6 часов, снижение баланса —
# шум одного-двух списаний; до этого срок считается по тарифу из API
HISTORY_MIN_SPAN = timedelta(hours=6)

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
    if provider.requires_login and not server.cloud_login:
        raise CloudBillingError("Login is required")

    client = get_external_client(server.cloud_proxy)
    try:
        snapshot = await provider.fetch(
            client, server.cloud_credential, server.cloud_account_id, server.cloud_login
        )
    except CloudBillingError as error:
        # Мёртвый прокси выглядит как недоступность провайдера — без пометки
        # «All connection attempts failed» читалось бы как сбой облака
        if server.cloud_proxy:
            error = type(error)(f"{error} (via proxy {sanitize_proxy(server.cloud_proxy)})")
        server.cloud_last_error = str(error)[:500]
        logger.warning("Cloud sync failed for '%s' (%s): %s", server.name, provider.id, error)
        raise error

    _apply_snapshot(server, snapshot, now, provider)
    return snapshot


def _apply_snapshot(
    server, snapshot: CloudSnapshot, now: datetime, provider: CloudProvider | None = None
) -> None:
    server.account_balance = snapshot.balance
    server.balance_updated_at = now
    # Провайдеры пишут код валюты по-разному (RUB у Yandex, rub у Selectel),
    # а сводка группирует суммы по нему — без единого регистра рубли разъехались бы
    server.currency = snapshot.currency.upper()
    server.cloud_last_sync_at = now
    server.cloud_last_error = snapshot.warning[:500] if snapshot.warning else None

    daily_cost = snapshot.daily_cost
    if provider is not None and provider.uses_balance_history:
        history_cost = _balance_history_daily_cost(server, snapshot.balance, now)
        if history_cost is not None:
            daily_cost = history_cost
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


def _balance_history_daily_cost(server, balance: float, now: datetime) -> float | None:
    """Фактический дневной расход по собственным снимкам баланса.

    Точка на каждую синхронизацию, окно HISTORY_WINDOW_DAYS. Интервалы, где
    баланс вырос (пополнение), выбрасываются целиком — сумма пополнения из API
    не видна, и расход внутри такого интервала восстановить нельзя.

    Рост не больше падения прямо перед ним — не пополнение, а возврат: VK Cloud
    может на час убрать с баланса тысячи и вернуть их. Такой интервал гасит
    это падение, иначе оно целиком осталось бы в расходе."""
    cutoff = now - timedelta(days=HISTORY_WINDOW_DAYS)
    points = [p for p in _load_history(server.cloud_balance_history) if p[0] >= cutoff]

    if points and now - points[-1][0] < HISTORY_MIN_GAP:
        points[-1] = (now, balance)
    else:
        points.append((now, balance))
    server.cloud_balance_history = json.dumps(
        [[ts.isoformat(), bal] for ts, bal in points]
    )

    spent = 0.0
    spent_seconds = 0.0
    returnable = 0.0
    for (prev_ts, prev_balance), (ts, cur_balance) in zip(points, points[1:]):
        delta = prev_balance - cur_balance
        if delta < 0 and -delta > returnable:
            returnable = 0.0
            continue
        spent += delta
        spent_seconds += (ts - prev_ts).total_seconds()
        returnable = delta if delta >= 0 else returnable + delta

    if spent <= 0 or spent_seconds < HISTORY_MIN_SPAN.total_seconds():
        return None
    return round(spent / (spent_seconds / 86400), 4)


def _load_history(raw) -> list[tuple[datetime, float]]:
    try:
        rows = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(rows, list):
        return []

    points: list[tuple[datetime, float]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        try:
            ts = datetime.fromisoformat(row[0])
            value = float(row[1])
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        points.append((ts, value))
    points.sort(key=lambda p: p[0])
    return points


def _daily_cost_from_forecast(snapshot: CloudSnapshot) -> float | None:
    """Провайдер отдал готовый прогноз вместо расхода (Selectel) — приводим к
    дневному расходу, чтобы порог остатка и калькулятор пополнения работали одинаково."""
    if not snapshot.days_left or snapshot.days_left <= 0 or snapshot.balance <= 0:
        return None
    return round(snapshot.balance / snapshot.days_left, 4)
