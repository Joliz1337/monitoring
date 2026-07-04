"""Anti-DDoS manager (panel side).

Two background loops:
  - whitelist push: hourly, sends {all node IPs + panel IP + user CIDRs} to every
    node's antiddos_allow set so the node has a fresh list on disk before an attack.
  - status poll: reads each node's emergency state into Server.antiddos_*, and
    fires a Telegram alert when a node auto-enters emergency mode.

Also exposes on-demand actions used by the API: install watchdog, toggle
emergency per-node / on all nodes, toggle the watchdog, push whitelist now.
"""

import asyncio
import ipaddress
import json
import logging
import re
import socket
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models import Server, AntiDdosSettings, AlertSettings, AlertHistory
from app.services.http_client import get_node_client, get_node_apply_client, get_external_client, node_auth_headers

logger = logging.getLogger(__name__)

GITHUB_CONFIGS_BASE = "https://raw.githubusercontent.com/Joliz1337/monitoring/main/configs"
WATCHDOG_SH_URL = f"{GITHUB_CONFIGS_BASE}/ddos-watchdog.sh"
WATCHDOG_SERVICE_URL = f"{GITHUB_CONFIGS_BASE}/ddos-watchdog.service"

FILES_CACHE_TTL = 300  # seconds


class AntiDdosManager:
    DB_CONCURRENCY = 10
    HTTP_CONCURRENCY = 50
    INSTALL_RETRY_INTERVAL = 600  # don't retry a failing auto-install more often

    def __init__(self):
        self._running = False
        self._whitelist_task: Optional[asyncio.Task] = None
        self._status_task: Optional[asyncio.Task] = None
        self._db_sem = asyncio.Semaphore(self.DB_CONCURRENCY)
        self._http_sem = asyncio.Semaphore(self.HTTP_CONCURRENCY)
        self._files_cache: Optional[tuple[float, str, str]] = None
        self._install_cooldown: dict[int, float] = {}

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self):
        if self._running:
            return
        self._running = True
        self._whitelist_task = asyncio.create_task(self._whitelist_loop())
        self._status_task = asyncio.create_task(self._status_loop())
        logger.info("AntiDdosManager started")

    async def stop(self):
        self._running = False
        for task in (self._whitelist_task, self._status_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        logger.info("AntiDdosManager stopped")

    # ── settings ───────────────────────────────────────────────────────────

    async def get_or_create_settings(self, db) -> AntiDdosSettings:
        row = (await db.execute(select(AntiDdosSettings).limit(1))).scalar_one_or_none()
        if row is None:
            row = AntiDdosSettings()
            db.add(row)
            await db.commit()
            await db.refresh(row)
        return row

    # ── whitelist assembly ─────────────────────────────────────────────────

    @staticmethod
    def _resolve_panel_ip() -> Optional[str]:
        domain = get_settings().domain
        if not domain:
            return None
        try:
            return socket.gethostbyname(domain)
        except (socket.gaierror, OSError):
            return None

    @staticmethod
    def _host_to_ip(host: str) -> Optional[str]:
        if not host:
            return None
        try:
            ipaddress.ip_address(host)
            return host
        except ValueError:
            pass
        try:
            return socket.gethostbyname(host)
        except (socket.gaierror, OSError):
            return None

    async def build_whitelist(self, db) -> list[str]:
        """Auto part (all node IPs + panel IP) + manual part (user CIDRs)."""
        ips: set[str] = set()

        servers = (await db.execute(select(Server).where(Server.is_active == True))).scalars().all()  # noqa: E712
        for srv in servers:
            ip = self._host_to_ip(urlparse(srv.url).hostname or "")
            if ip:
                ips.add(ip)

        panel_ip = self._resolve_panel_ip()
        if panel_ip:
            ips.add(panel_ip)

        settings = await self.get_or_create_settings(db)
        if settings.user_cidrs:
            try:
                for cidr in json.loads(settings.user_cidrs):
                    cidr = str(cidr).strip()
                    if self._valid_ip_cidr(cidr):
                        ips.add(cidr)
            except (ValueError, TypeError):
                pass

        return sorted(ips)

    @staticmethod
    def _valid_ip_cidr(value: str) -> bool:
        try:
            if "/" in value:
                ipaddress.ip_network(value, strict=False)
            else:
                ipaddress.ip_address(value)
            return True
        except ValueError:
            return False

    # ── node calls ─────────────────────────────────────────────────────────

    async def sync_whitelist_to_node(self, server: Server, ips: list[str], timeout: float = 30.0) -> tuple[bool, str]:
        try:
            client = get_node_client(server)
            resp = await client.post(
                f"{server.url}/api/antiddos/whitelist/sync",
                headers=node_auth_headers(server),
                json={"ips": ips},
                timeout=timeout,
            )
            if resp.status_code == 200:
                return True, "synced"
            if resp.status_code == 404:
                return True, "node has no antiddos endpoint (old version)"
            return False, f"HTTP {resp.status_code}"
        except httpx.TimeoutException:
            return False, "timeout"
        except Exception as e:
            return False, str(e)

    async def get_node_status(self, server: Server, timeout: float = 15.0) -> Optional[dict]:
        try:
            client = get_node_client(server)
            resp = await client.get(
                f"{server.url}/api/antiddos/status",
                headers=node_auth_headers(server),
                timeout=timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:
            return None

    async def set_node_emergency(self, server: Server, enabled: bool, timeout: float = 45.0) -> tuple[bool, str, Optional[dict]]:
        try:
            client = get_node_apply_client(server)
            resp = await client.post(
                f"{server.url}/api/antiddos/emergency",
                headers=node_auth_headers(server),
                json={"enabled": enabled},
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                return bool(data.get("success")), data.get("message", ""), data.get("status")
            return False, f"HTTP {resp.status_code}", None
        except httpx.TimeoutException:
            return False, "timeout", None
        except Exception as e:
            return False, str(e), None

    async def set_node_watchdog(self, server: Server, enabled: bool, timeout: float = 20.0) -> tuple[bool, str]:
        try:
            client = get_node_client(server)
            resp = await client.post(
                f"{server.url}/api/antiddos/watchdog",
                headers=node_auth_headers(server),
                json={"enabled": enabled},
                timeout=timeout,
            )
            if resp.status_code == 200:
                return bool(resp.json().get("success")), "ok"
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)

    async def _fetch_watchdog_files(self) -> Optional[tuple[str, str]]:
        loop = asyncio.get_event_loop()
        now = loop.time()
        if self._files_cache and (now - self._files_cache[0]) < FILES_CACHE_TTL:
            return self._files_cache[1], self._files_cache[2]
        try:
            client = get_external_client()
            sh_resp, svc_resp = await asyncio.gather(
                client.get(WATCHDOG_SH_URL, timeout=20.0),
                client.get(WATCHDOG_SERVICE_URL, timeout=20.0),
            )
            if sh_resp.status_code != 200 or svc_resp.status_code != 200:
                return None
            self._files_cache = (now, sh_resp.text, svc_resp.text)
            return sh_resp.text, svc_resp.text
        except Exception as e:
            logger.error(f"Failed to fetch watchdog files: {e}")
            return None

    async def _expected_watchdog_version(self) -> Optional[str]:
        files = await self._fetch_watchdog_files()
        if not files:
            return None
        match = re.search(r'WATCHDOG_VERSION="?([0-9][0-9.]*)"?', files[0])
        return match.group(1) if match else None

    async def install_to_node(self, server: Server, timeout: float = 60.0) -> tuple[bool, str]:
        files = await self._fetch_watchdog_files()
        if not files:
            return False, "could not fetch watchdog files from GitHub"
        script_content, service_content = files
        try:
            async with async_session() as db:
                settings = await self.get_or_create_settings(db)
                watchdog_default = settings.watchdog_default_enabled
            client = get_node_apply_client(server)
            resp = await client.post(
                f"{server.url}/api/antiddos/install",
                headers=node_auth_headers(server),
                json={
                    "script_content": script_content,
                    "service_content": service_content,
                    "watchdog_enabled": watchdog_default,
                },
                timeout=timeout,
            )
            if resp.status_code == 200:
                return bool(resp.json().get("success")), "installed"
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)

    # ── fan-out actions ────────────────────────────────────────────────────

    async def run_bg(self, coro):
        """Await a fan-out coroutine as a fire-and-forget background task,
        swallowing errors so a dropped client or a bad node can't crash it."""
        try:
            await coro
        except Exception as e:
            logger.error(f"background antiddos task failed: {e}")

    async def _active_servers(self) -> list[Server]:
        async with self._db_sem:
            async with async_session() as db:
                return list(
                    (await db.execute(select(Server).where(Server.is_active == True))).scalars().all()  # noqa: E712
                )

    async def push_whitelist_all(self) -> dict:
        async with self._db_sem:
            async with async_session() as db:
                ips = await self.build_whitelist(db)
                servers = list(
                    (await db.execute(select(Server).where(Server.is_active == True))).scalars().all()  # noqa: E712
                )

        async def _one(srv: Server):
            async with self._http_sem:
                ok, msg = await self.sync_whitelist_to_node(srv, ips)
                return {"server_id": srv.id, "success": ok, "message": msg}

        results = await asyncio.gather(*[_one(s) for s in servers], return_exceptions=True)
        ok_count = sum(1 for r in results if isinstance(r, dict) and r.get("success"))

        async with async_session() as db:
            settings = await self.get_or_create_settings(db)
            settings.last_push_at = datetime.now(timezone.utc)
            settings.last_push_status = "ok" if ok_count == len(servers) else "partial"
            settings.last_push_count = len(ips)
            await db.commit()

        return {"whitelist_size": len(ips), "nodes": len(servers), "ok": ok_count}

    async def set_emergency_all(self, enabled: bool) -> dict:
        servers = await self._active_servers()

        async def _one(srv: Server):
            async with self._http_sem:
                ok, msg, status = await self.set_node_emergency(srv, enabled)
                if ok and status:
                    await self._store_status(srv.id, status)
                return {"server_id": srv.id, "success": ok, "message": msg}

        results = await asyncio.gather(*[_one(s) for s in servers], return_exceptions=True)
        ok_count = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
        return {"nodes": len(servers), "ok": ok_count}

    async def set_watchdog_all(self, enabled: bool) -> dict:
        servers = await self._active_servers()

        async def _one(srv: Server):
            async with self._http_sem:
                ok, _ = await self.set_node_watchdog(srv, enabled)
                return ok

        results = await asyncio.gather(*[_one(s) for s in servers], return_exceptions=True)
        ok_count = sum(1 for r in results if r is True)
        return {"nodes": len(servers), "ok": ok_count}

    async def apply_master_state(self, enabled: bool):
        """The master switch controls ONLY fleet-wide auto-detection (watchdog).
        Manual emergency mode is a separate control and is never touched here, so
        turning auto-detection off never clears an emergency an admin pinned."""
        try:
            await self.set_watchdog_all(enabled)
        except Exception as e:
            logger.error(f"apply_master_state({enabled}) failed: {e}")

    async def install_all(self) -> dict:
        servers = await self._active_servers()

        async def _one(srv: Server):
            async with self._http_sem:
                ok, msg = await self.install_to_node(srv)
                return {"server_id": srv.id, "success": ok, "message": msg}

        results = await asyncio.gather(*[_one(s) for s in servers], return_exceptions=True)
        ok_count = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
        return {"nodes": len(servers), "ok": ok_count}

    # ── status persistence + alerting ──────────────────────────────────────

    async def _store_status(self, server_id: int, status: dict, alert: bool = False):
        mode_on = status.get("mode") == "on"
        source = status.get("source", "none")
        reason = (status.get("reason") or "")[:200]
        watchdog_on = status.get("watchdog", "on") == "on"
        since_epoch = int(status.get("since", 0) or 0)
        since_dt = datetime.fromtimestamp(since_epoch, tz=timezone.utc) if since_epoch else None

        async with async_session() as db:
            srv = (await db.execute(select(Server).where(Server.id == server_id))).scalar_one_or_none()
            if srv is None:
                return
            was_on = bool(srv.antiddos_emergency_mode)
            srv.antiddos_emergency_mode = mode_on
            srv.antiddos_source = source
            srv.antiddos_reason = reason
            srv.antiddos_watchdog = watchdog_on
            srv.antiddos_since = since_dt
            srv.antiddos_last_sync_at = datetime.now(timezone.utc)
            server_name = srv.name
            await db.commit()

        # alert only on an off→on transition that the node enabled itself
        if alert and mode_on and not was_on and source == "auto":
            await self._send_alert(server_id, server_name, reason)

    async def _send_alert(self, server_id: int, server_name: str, reason: str):
        try:
            async with async_session() as db:
                alert_settings = (await db.execute(select(AlertSettings).limit(1))).scalar_one_or_none()
            token = getattr(alert_settings, "telegram_bot_token", None)
            chat_id = getattr(alert_settings, "telegram_chat_id", None)
            notified = False
            if token and chat_id:
                text = (
                    f"\U0001f6e1️ <b>Anti-DDoS: аварийный режим</b>\n\n"
                    f"Нода <b>{server_name}</b> автоматически включила защиту.\n"
                    f"Причина: {reason or 'signal detected'}"
                )
                from app.services.telegram_bot import get_telegram_bot_service
                notified = await get_telegram_bot_service().send_message(token, chat_id, text)

            async with async_session() as db:
                db.add(AlertHistory(
                    server_id=server_id,
                    server_name=server_name,
                    alert_type="antiddos_emergency",
                    severity="warning",
                    message=f"Auto emergency mode: {reason}",
                    details=json.dumps({"reason": reason}, ensure_ascii=False),
                    notified=notified,
                ))
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to send antiddos alert: {e}")

    async def _maybe_auto_install(self, server: Server, status: dict, expected_version: Optional[str]) -> dict:
        """Zero-touch rollout: if a node has the new agent (/api/antiddos exists,
        so status came back) but the watchdog isn't installed — or it's an older
        version — the panel installs/updates it itself. No admin action, no per-node
        clicking. A per-node cooldown keeps a failing install from retrying every poll.
        """
        installed = status.get("installed", False)
        version = status.get("version") or ""
        needs = (not installed) or (expected_version and version and version != expected_version)
        if not needs:
            return status

        loop = asyncio.get_event_loop()
        now = loop.time()
        if now - self._install_cooldown.get(server.id, 0.0) < self.INSTALL_RETRY_INTERVAL:
            return status
        self._install_cooldown[server.id] = now

        ok, msg = await self.install_to_node(server)
        if not ok:
            logger.warning(f"Auto-install of watchdog on {server.name} failed: {msg}")
            return status

        logger.info(f"Auto-installed watchdog on {server.name}")
        # give the freshly-installed node its whitelist immediately
        try:
            async with async_session() as db:
                ips = await self.build_whitelist(db)
            await self.sync_whitelist_to_node(server, ips)
        except Exception as e:
            logger.warning(f"Post-install whitelist push to {server.name} failed: {e}")

        fresh = await self.get_node_status(server)
        return fresh if fresh is not None else status

    async def poll_status_all(self):
        servers = await self._active_servers()
        expected_version = await self._expected_watchdog_version()

        async def _one(srv: Server):
            async with self._http_sem:
                status = await self.get_node_status(srv)
                if status is None:
                    return  # unreachable, or old agent without /api/antiddos
                status = await self._maybe_auto_install(srv, status, expected_version)
                await self._store_status(srv.id, status, alert=True)

        await asyncio.gather(*[_one(s) for s in servers], return_exceptions=True)

    # ── loops ──────────────────────────────────────────────────────────────

    async def _whitelist_loop(self):
        await asyncio.sleep(30)
        while self._running:
            interval = 3600
            try:
                async with async_session() as db:
                    settings = await self.get_or_create_settings(db)
                    enabled = settings.enabled
                    interval = max(300, settings.whitelist_push_interval or 3600)
                if enabled:
                    await self.push_whitelist_all()
            except Exception as e:
                logger.error(f"Whitelist push loop error: {e}")
            await asyncio.sleep(interval)

    async def _status_loop(self):
        await asyncio.sleep(20)
        while self._running:
            interval = 60
            try:
                async with async_session() as db:
                    settings = await self.get_or_create_settings(db)
                    enabled = settings.enabled
                    interval = max(15, settings.status_poll_interval or 60)
                if enabled:
                    await self.poll_status_all()
            except Exception as e:
                logger.error(f"Status poll loop error: {e}")
            await asyncio.sleep(interval)


_manager: Optional[AntiDdosManager] = None


def get_antiddos_manager() -> AntiDdosManager:
    global _manager
    if _manager is None:
        _manager = AntiDdosManager()
    return _manager


async def start_antiddos_manager():
    await get_antiddos_manager().start()


async def stop_antiddos_manager():
    await get_antiddos_manager().stop()
