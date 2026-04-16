import asyncio
import ipaddress
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import TorrentBlockerSettings, RemnawaveSettings, Server
from app.services.http_client import get_node_client, node_auth_headers
from app.services.remnawave_api import RemnawaveAPI, RemnawaveAPIError

logger = logging.getLogger(__name__)

PAGE_SIZE = 500


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
    def _extract_ips(records: list[dict]) -> list[str]:
        seen: set[str] = set()
        valid_ips: list[str] = []
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
            if ip_str not in seen:
                seen.add(ip_str)
                valid_ips.append(ip_str)
        return valid_ips

    async def _send_to_nodes(
        self, ips: list[str], ban_seconds: int, excluded_ids: set[int]
    ) -> dict[int, dict]:
        async with async_session() as db:
            result = await db.execute(
                select(Server).where(Server.is_active == True)  # noqa: E712
            )
            servers = result.scalars().all()

        targets = [s for s in servers if s.id not in excluded_ids]
        if not targets:
            logger.warning("No target nodes for torrent blocker bans")
            return {}

        async def _send_one(server: Server) -> dict:
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

                ips = self._extract_ips(records)
                if not ips:
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
                node_results = await self._send_to_nodes(ips, ban_seconds, excluded_ids)

                successful = sum(1 for r in node_results.values() if r.get("success"))
                failed = len(node_results) - successful

                if successful > 0:
                    try:
                        await api.truncate_torrent_blocker_reports()
                    except RemnawaveAPIError as e:
                        logger.error(f"Failed to truncate reports: {e}")

                if failed:
                    for sid, r in node_results.items():
                        if not r.get("success"):
                            logger.warning(f"Torrent blocker: node {sid} failed: {r.get('error')}")

                msg = f"Banned {len(ips)} IPs on {successful}/{successful + failed} nodes"
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
