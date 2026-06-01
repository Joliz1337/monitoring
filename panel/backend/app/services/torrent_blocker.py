import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select

from app.database import async_session
from app.models import (
    RemnawaveSettings,
    RemnawaveUserCache,
    Server,
    TorrentBlockerBan,
    TorrentBlockerSettings,
)
from app.services.http_client import get_external_client, get_node_client, node_auth_headers
from app.services.remnawave_api import RemnawaveAPI, RemnawaveAPIError

logger = logging.getLogger(__name__)

PAGE_SIZE = 500
BAN_RETENTION_DAYS = 35
SEND_CONCURRENCY = 30  # макс параллельных POST к нодам — без лимита 100+ одновременных запросов забивают keepalive-пул и роняют поток метрик
WEBHOOK_CONCURRENCY = 20
# Нода считается живой, если сборщик метрик видел её не дольше этого окна назад.
# Сборщик опрашивает ноды каждые ~10с (макс 300с), 300с — заведомо живой запас.
LIVE_THRESHOLD_SECONDS = 300


@dataclass
class BanTarget:
    """IP-кандидат на бан вместе с данными пользователя для вебхук-уведомления."""
    ip: str
    user_uuid: Optional[str] = None
    username: Optional[str] = None
    node_name: Optional[str] = None
    node_country: Optional[str] = None
    telegram_id: Optional[int] = None
    short_uuid: Optional[str] = None


class TorrentBlockerService:
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Torrent blocker service started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Torrent blocker service stopped")

    async def _loop(self):
        await asyncio.sleep(30)
        while self._running:
            interval = 300
            try:
                settings = await self._get_settings()
                if settings and settings.enabled:
                    await self._poll_cycle()
                    interval = max(60, settings.poll_interval_minutes * 60)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Torrent blocker cycle error: {e}")
            await asyncio.sleep(interval)

    async def _get_settings(self) -> Optional[TorrentBlockerSettings]:
        async with async_session() as db:
            result = await db.execute(select(TorrentBlockerSettings).limit(1))
            return result.scalar_one_or_none()

    async def _get_remnawave_api(self) -> Optional[RemnawaveAPI]:
        async with async_session() as db:
            result = await db.execute(select(RemnawaveSettings).limit(1))
            rw = result.scalar_one_or_none()
            if not rw or not rw.api_url or not rw.api_token:
                return None
            return RemnawaveAPI(rw.api_url, rw.api_token, rw.cookie_secret)

    async def _fetch_all_reports(self, api: RemnawaveAPI) -> list[dict]:
        all_records: list[dict] = []
        start = 0
        while True:
            page = await api.get_torrent_blocker_reports(start=start, size=PAGE_SIZE)
            records = page.get("records", [])
            all_records.extend(records)
            total = page.get("total", 0)
            if len(all_records) >= total or not records:
                break
            start += PAGE_SIZE
        return all_records

    @staticmethod
    def _extract_ban_targets(records: list[dict]) -> list[BanTarget]:
        seen: set[str] = set()
        targets: list[BanTarget] = []
        for record in records:
            report = record.get("report", {})
            action = report.get("actionReport", {})
            ip_str = action.get("ip", "")
            if not ip_str:
                # Фолбэк: source из xrayReport (формат ip:port)
                xray = report.get("xrayReport", {})
                source = xray.get("source", "")
                if ":" in source:
                    ip_str = source.rsplit(":", 1)[0]
            if not ip_str:
                continue
            try:
                ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if ip_str in seen:
                continue
            seen.add(ip_str)
            user = record.get("user", {})
            node = record.get("node", {})
            targets.append(BanTarget(
                ip=ip_str,
                user_uuid=user.get("uuid") or None,
                username=user.get("username") or None,
                node_name=node.get("name") or None,
                node_country=node.get("countryCode") or None,
            ))
        return targets

    async def _enrich_targets(self, targets: list[BanTarget]):
        """Подтянуть telegram_id и shortUuid пользователей из кэша Remnawave по uuid."""
        uuids = {t.user_uuid for t in targets if t.user_uuid}
        if not uuids:
            return
        async with async_session() as db:
            rows = (await db.execute(
                select(
                    RemnawaveUserCache.uuid,
                    RemnawaveUserCache.telegram_id,
                    RemnawaveUserCache.short_uuid,
                    RemnawaveUserCache.username,
                ).where(RemnawaveUserCache.uuid.in_(uuids))
            )).all()
        by_uuid = {row.uuid: row for row in rows}
        for target in targets:
            row = by_uuid.get(target.user_uuid)
            if not row:
                continue
            target.telegram_id = row.telegram_id
            target.short_uuid = target.short_uuid or row.short_uuid
            target.username = target.username or row.username

    async def _send_webhooks(
        self, targets: list[BanTarget], settings: TorrentBlockerSettings,
        ban_seconds: int, ban_at: datetime,
    ) -> int:
        url = (settings.webhook_url or "").strip()
        if not url:
            return 0

        secret = (settings.webhook_secret or "").encode("utf-8")
        delay_seconds = max(0, settings.webhook_delay_seconds or 0)
        scheduled_at = datetime.now(timezone.utc).isoformat()
        client = get_external_client()
        sem = asyncio.Semaphore(WEBHOOK_CONCURRENCY)

        async def _send_one(target: BanTarget) -> bool:
            payload = {
                "event": "torrent_ban_scheduled",
                "ip": target.ip,
                "user": {
                    "uuid": target.user_uuid,
                    "short_uuid": target.short_uuid,
                    "username": target.username,
                    "telegram_id": target.telegram_id,
                },
                "node": {"name": target.node_name, "country": target.node_country},
                "ban_duration_seconds": ban_seconds,
                "delay_seconds": delay_seconds,
                "ban_at": ban_at.isoformat(),
                "scheduled_at": scheduled_at,
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if secret:
                signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
                headers["X-Signature"] = f"sha256={signature}"
            async with sem:
                try:
                    response = await client.post(url, content=body, headers=headers)
                    if response.status_code < 400:
                        return True
                    logger.warning(f"Torrent blocker webhook for {target.ip}: HTTP {response.status_code}")
                    return False
                except Exception as e:
                    logger.warning(f"Torrent blocker webhook for {target.ip} failed: {e}")
                    return False

        results = await asyncio.gather(*[_send_one(t) for t in targets])
        return sum(1 for ok in results if ok)

    @staticmethod
    def _is_node_live(server: Server, cutoff: datetime) -> bool:
        if not server.last_seen:
            return False
        last_seen = server.last_seen
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        return last_seen >= cutoff

    async def _send_to_nodes(
        self, ips: list[str], ban_seconds: int, excluded_ids: set[int]
    ) -> dict[int, dict]:
        async with async_session() as db:
            result = await db.execute(
                select(Server).where(Server.is_active == True)  # noqa: E712
            )
            servers = result.scalars().all()

        # Слать бан только на живые ноды: мёртвые лишь висят до таймаута 20с и тормозят
        # весь цикл (gather ждёт всех). Живость — по свежести last_seen от сборщика метрик.
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=LIVE_THRESHOLD_SECONDS)
        candidates = [s for s in servers if s.id not in excluded_ids]
        targets = [s for s in candidates if self._is_node_live(s, cutoff)]

        skipped_dead = len(candidates) - len(targets)
        if skipped_dead:
            logger.info(f"Torrent blocker: skipping {skipped_dead} offline node(s)")
        if not targets:
            logger.warning("No live target nodes for torrent blocker bans")
            return {}

        sem = asyncio.Semaphore(SEND_CONCURRENCY)

        async def _send_one(server: Server) -> dict:
            async with sem:
                try:
                    client = get_node_client(server)
                    response = await client.post(
                        f"{server.url}/api/ipset/bulk-add",
                        headers=node_auth_headers(server),
                        json={
                            "ips": ips,
                            "permanent": False,
                            "direction": "in",
                            "timeout": ban_seconds,
                        },
                        timeout=20.0,
                    )
                    if response.status_code == 200:
                        data = response.json()
                        return {"success": True, "added": data.get("added", 0)}
                    return {"success": False, "error": f"HTTP {response.status_code}"}
                except Exception as e:
                    return {"success": False, "error": str(e)}

        tasks = [_send_one(s) for s in targets]
        results_list = await asyncio.gather(*tasks)
        return {s.id: r for s, r in zip(targets, results_list)}

    async def _poll_cycle(self):
        if self._lock.locked():
            return
        async with self._lock:
            settings = await self._get_settings()
            if not settings or not settings.enabled:
                return

            api = await self._get_remnawave_api()
            if not api:
                logger.warning("Torrent blocker: Remnawave API not configured")
                await self._update_status("error", "Remnawave API not configured", 0, 0)
                return

            try:
                records = await self._fetch_all_reports(api)
                if not records:
                    await self._update_status("no_reports", "No reports", 0, 0)
                    await api.close()
                    return

                targets = self._extract_ban_targets(records)
                if not targets:
                    await self._update_status("no_reports", "Reports found but no valid IPs", len(records), 0)
                    await api.close()
                    return

                excluded_ids = set()
                if settings.excluded_server_ids:
                    try:
                        excluded_ids = set(json.loads(settings.excluded_server_ids))
                    except (json.JSONDecodeError, TypeError):
                        pass

                ban_seconds = settings.ban_duration_minutes * 60
                ips = [t.ip for t in targets]

                # Вебхук-предупреждение + грейс-период: уведомляем бота о грядущем бане,
                # ждём задержку, затем баним. Сбой вебхука бан не отменяет (fail-open).
                webhook_sent = None
                if settings.webhook_enabled and (settings.webhook_url or "").strip():
                    delay_seconds = max(0, settings.webhook_delay_seconds or 0)
                    ban_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
                    await self._enrich_targets(targets)
                    webhook_sent = await self._send_webhooks(targets, settings, ban_seconds, ban_at)
                    if delay_seconds > 0:
                        await asyncio.sleep(delay_seconds)

                node_results = await self._send_to_nodes(ips, ban_seconds, excluded_ids)

                successful = sum(1 for r in node_results.values() if r.get("success"))
                failed = len(node_results) - successful

                if successful > 0:
                    await self._record_bans(ips, ban_seconds)
                    try:
                        await api.truncate_torrent_blocker_reports()
                    except RemnawaveAPIError as e:
                        logger.error(f"Failed to truncate reports: {e}")

                if failed:
                    for sid, r in node_results.items():
                        if not r.get("success"):
                            logger.warning(f"Torrent blocker: node {sid} failed: {r.get('error')}")

                msg = f"Banned {len(ips)} IPs on {successful}/{successful + failed} nodes"
                if webhook_sent is not None:
                    msg += f"; webhooks {webhook_sent}/{len(targets)}"
                status = "success" if successful > 0 else "error"
                await self._update_status(status, msg, len(records), len(ips))
                logger.info(f"Torrent blocker: {msg}")

            except RemnawaveAPIError as e:
                logger.error(f"Torrent blocker Remnawave error: {e}")
                await self._update_status("error", str(e.message), 0, 0)
            except Exception as e:
                logger.error(f"Torrent blocker unexpected error: {e}")
                await self._update_status("error", str(e), 0, 0)
            finally:
                await api.close()

    async def _record_bans(self, ips: list[str], ban_seconds: int):
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ban_seconds)
        async with async_session() as db:
            db.add_all([
                TorrentBlockerBan(ip=ip, banned_at=now, expires_at=expires_at)
                for ip in ips
            ])
            cutoff = now - timedelta(days=BAN_RETENTION_DAYS)
            await db.execute(delete(TorrentBlockerBan).where(TorrentBlockerBan.banned_at < cutoff))
            await db.commit()

    async def _update_status(
        self, status: str, message: str, reports_processed: int, ips_banned: int
    ):
        async with async_session() as db:
            result = await db.execute(select(TorrentBlockerSettings).limit(1))
            settings = result.scalar_one_or_none()
            if not settings:
                return
            settings.last_poll_at = datetime.now(timezone.utc)
            settings.last_poll_status = status
            settings.last_poll_message = message
            settings.last_reports_processed = reports_processed
            settings.last_ips_banned = ips_banned
            settings.total_ips_banned = (settings.total_ips_banned or 0) + ips_banned
            settings.total_cycles = (settings.total_cycles or 0) + 1
            await db.commit()

    async def get_status(self) -> dict:
        settings = await self._get_settings()
        if not settings:
            return {
                "running": self._running,
                "enabled": False,
                "last_poll_at": None,
                "last_poll_status": None,
                "last_poll_message": None,
                "last_ips_banned": 0,
                "last_reports_processed": 0,
                "total_ips_banned": 0,
                "total_cycles": 0,
            }
        return {
            "running": self._running,
            "enabled": settings.enabled,
            "last_poll_at": settings.last_poll_at.isoformat() if settings.last_poll_at else None,
            "last_poll_status": settings.last_poll_status,
            "last_poll_message": settings.last_poll_message,
            "last_ips_banned": settings.last_ips_banned or 0,
            "last_reports_processed": settings.last_reports_processed or 0,
            "total_ips_banned": settings.total_ips_banned or 0,
            "total_cycles": settings.total_cycles or 0,
        }

    async def run_now(self):
        asyncio.ensure_future(self._poll_cycle())

    @staticmethod
    async def send_test_webhook(url: str, secret: Optional[str]) -> tuple[bool, str]:
        """Отправить тестовый payload на вебхук. Возвращает (успех, сообщение)."""
        url = (url or "").strip()
        if not url.startswith("https://"):
            return False, "webhook_url must use https://"

        now = datetime.now(timezone.utc)
        payload = {
            "event": "torrent_ban_scheduled",
            "test": True,
            "ip": "203.0.113.10",
            "user": {
                "uuid": "00000000-0000-0000-0000-000000000000",
                "short_uuid": "testshort",
                "username": "test_user",
                "telegram_id": 123456789,
            },
            "node": {"name": "test-node", "country": "NL"},
            "ban_duration_seconds": 1800,
            "delay_seconds": 60,
            "ban_at": (now + timedelta(seconds=60)).isoformat(),
            "scheduled_at": now.isoformat(),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if secret:
            signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-Signature"] = f"sha256={signature}"

        try:
            response = await get_external_client().post(url, content=body, headers=headers)
            if response.status_code < 400:
                return True, f"HTTP {response.status_code}"
            return False, f"HTTP {response.status_code}"
        except Exception as e:
            return False, str(e)


_service: Optional[TorrentBlockerService] = None


def get_torrent_blocker_service() -> TorrentBlockerService:
    global _service
    if _service is None:
        _service = TorrentBlockerService()
    return _service


async def start_torrent_blocker():
    service = get_torrent_blocker_service()
    await service.start()


async def stop_torrent_blocker():
    service = get_torrent_blocker_service()
    await service.stop()
