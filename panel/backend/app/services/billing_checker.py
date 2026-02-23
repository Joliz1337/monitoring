import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from sqlalchemy import select

from app.database import async_session
from app.models import BillingServer, BillingSettings, AlertSettings

logger = logging.getLogger(__name__)


class BillingChecker:
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._check_interval = 3600

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Billing checker started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Billing checker stopped")

    async def _loop(self):
        await asyncio.sleep(30)
        while self._running:
            try:
                await self._check()
            except Exception as e:
                logger.error(f"Billing check error: {e}")
            await asyncio.sleep(self._check_interval)

    async def _check(self):
        async with async_session() as db:
            settings_row = (await db.execute(select(BillingSettings).limit(1))).scalar_one_or_none()
            if not settings_row or not settings_row.enabled:
                return

            self._check_interval = max(60, (settings_row.check_interval_minutes or 60)) * 60

            try:
                notify_days = json.loads(settings_row.notify_days) if settings_row.notify_days else [1, 3, 7]
            except (json.JSONDecodeError, TypeError):
                notify_days = [1, 3, 7]
            notify_days = sorted(notify_days, reverse=True)

            alert_row = (await db.execute(select(AlertSettings).limit(1))).scalar_one_or_none()
            if not alert_row or not alert_row.telegram_bot_token or not alert_row.telegram_chat_id:
                return

            servers = (await db.execute(select(BillingServer))).scalars().all()
            now = datetime.now(timezone.utc)

            for srv in servers:
                await self._check_server(db, srv, now, notify_days, alert_row)

    async def _check_server(
        self,
        db,
        srv: BillingServer,
        now: datetime,
        notify_days: list[int],
        alert: AlertSettings,
    ):
        if srv.billing_type == "resource" and srv.monthly_cost and srv.monthly_cost > 0:
            if srv.balance_updated_at and srv.account_balance is not None:
                updated = srv.balance_updated_at
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                elapsed = (now - updated).total_seconds() / 86400
                daily_cost = srv.monthly_cost / 30
                consumed = elapsed * daily_cost
                new_balance = max(0, srv.account_balance - consumed)
                srv.account_balance = new_balance
                srv.balance_updated_at = now
                if new_balance > 0:
                    days_remaining = new_balance / daily_cost
                    srv.paid_until = now + timedelta(days=days_remaining)
                else:
                    srv.paid_until = now

        if not srv.paid_until:
            return

        paid_until = srv.paid_until
        if paid_until.tzinfo is None:
            paid_until = paid_until.replace(tzinfo=timezone.utc)

        days_left = (paid_until - now).total_seconds() / 86400

        try:
            already_notified = json.loads(srv.last_notified_days) if srv.last_notified_days else []
        except (json.JSONDecodeError, TypeError):
            already_notified = []

        for threshold in notify_days:
            if days_left <= threshold and threshold not in already_notified:
                sent = await self._send_notification(alert, srv, days_left, threshold)
                if sent:
                    already_notified.append(threshold)
                    srv.last_notified_days = json.dumps(already_notified)

        await db.commit()

    async def _send_notification(
        self,
        alert: AlertSettings,
        srv: BillingServer,
        days_left: float,
        threshold: int,
    ) -> bool:
        if days_left <= 0:
            emoji = "\U0001f534"
            status = "EXPIRED"
        elif days_left <= 1:
            emoji = "\U0001f534"
            status = f"{days_left:.1f}d left"
        elif days_left <= 3:
            emoji = "\U0001f7e1"
            status = f"{days_left:.1f}d left"
        else:
            emoji = "\U0001f7e0"
            status = f"{days_left:.1f}d left"

        billing_label = "monthly" if srv.billing_type == "monthly" else "resource"
        lines = [
            f"{emoji} <b>Billing Alert</b>",
            f"",
            f"\U0001f4bb <b>{srv.name}</b> ({billing_label})",
            f"\u23f0 {status}",
        ]

        if srv.billing_type == "resource" and srv.account_balance is not None:
            lines.append(f"\U0001f4b0 Balance: {srv.account_balance:.2f} {srv.currency or 'USD'}")
            if srv.monthly_cost:
                lines.append(f"\U0001f4b8 Cost: {srv.monthly_cost:.2f}/mo")

        if srv.paid_until:
            pu = srv.paid_until
            if pu.tzinfo is None:
                pu = pu.replace(tzinfo=timezone.utc)
            lines.append(f"\U0001f4c5 Expires: {pu.strftime('%Y-%m-%d %H:%M')} UTC")

        text = "\n".join(lines)

        try:
            url = f"https://api.telegram.org/bot{alert.telegram_bot_token}/sendMessage"
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={
                    "chat_id": alert.telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        logger.info(f"Billing alert sent for '{srv.name}' (threshold={threshold}d)")
                        return True
                    logger.warning(f"Telegram API error {resp.status} for billing alert")
                    return False
        except Exception as e:
            logger.error(f"Failed to send billing alert: {e}")
            return False


_instance: Optional[BillingChecker] = None


def get_billing_checker() -> BillingChecker:
    global _instance
    if _instance is None:
        _instance = BillingChecker()
    return _instance


async def start_billing_checker():
    checker = get_billing_checker()
    await checker.start()


async def stop_billing_checker():
    checker = get_billing_checker()
    await checker.stop()
