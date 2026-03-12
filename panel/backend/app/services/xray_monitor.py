"""Background service for monitoring Xray connections via xray-core proxy checks."""

import asyncio
import json
import logging
import os
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import httpx
from sqlalchemy import select, delete, func as sa_func

from app.database import async_session
from app.models import (
    XrayMonitorSettings, XrayMonitorServer, XrayMonitorCheck, AlertSettings,
)

logger = logging.getLogger(__name__)

XRAY_BIN = "/usr/local/bin/xray"
SOCKS_PORT_BASE = 10001
CONFIG_DIR = Path("/app/data/xray-monitor")
CONFIG_PATH = CONFIG_DIR / "config.json"
CHECK_HISTORY_RETENTION_HOURS = 24


def _build_outbound(server: XrayMonitorServer) -> dict:
    """Build xray-core outbound JSON from a parsed server record."""
    config = json.loads(server.config_json) if server.config_json else {}
    protocol = server.protocol
    tag = f"out-{server.id}"

    if protocol == "vless":
        security = config.get("security", "none")
        stream = _build_stream_settings(config)
        outbound = {
            "tag": tag,
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": server.address,
                    "port": server.port,
                    "users": [{"id": config.get("id", ""), "encryption": "none"}],
                }]
            },
            "streamSettings": stream,
        }
        return outbound

    if protocol == "vmess":
        stream = _build_stream_settings(config)
        outbound = {
            "tag": tag,
            "protocol": "vmess",
            "settings": {
                "vnext": [{
                    "address": server.address,
                    "port": server.port,
                    "users": [{
                        "id": config.get("id", ""),
                        "alterId": int(config.get("alterId", 0)),
                        "security": config.get("security", "auto"),
                    }],
                }]
            },
            "streamSettings": stream,
        }
        return outbound

    if protocol == "trojan":
        stream = _build_stream_settings(config)
        outbound = {
            "tag": tag,
            "protocol": "trojan",
            "settings": {
                "servers": [{
                    "address": server.address,
                    "port": server.port,
                    "password": config.get("password", ""),
                }]
            },
            "streamSettings": stream,
        }
        return outbound

    if protocol == "shadowsocks":
        outbound = {
            "tag": tag,
            "protocol": "shadowsocks",
            "settings": {
                "servers": [{
                    "address": server.address,
                    "port": server.port,
                    "method": config.get("method", "aes-256-gcm"),
                    "password": config.get("password", ""),
                }]
            },
        }
        return outbound

    return {"tag": tag, "protocol": "freedom", "settings": {}}


def _build_stream_settings(config: dict) -> dict:
    """Build streamSettings from parsed key params."""
    stream: dict = {}
    net = config.get("net", config.get("type", "tcp"))
    security = config.get("security", config.get("tls", ""))

    stream["network"] = net

    if net == "ws":
        stream["wsSettings"] = {
            "path": config.get("path", "/"),
            "headers": {"Host": config.get("host", "")} if config.get("host") else {},
        }
    elif net == "grpc":
        stream["grpcSettings"] = {"serviceName": config.get("serviceName", config.get("path", ""))}
    elif net == "h2" or net == "http":
        stream["httpSettings"] = {
            "path": config.get("path", "/"),
            "host": [config.get("host", "")] if config.get("host") else [],
        }
    elif net == "tcp" and config.get("headerType") == "http":
        stream["tcpSettings"] = {
            "header": {
                "type": "http",
                "request": {"path": [config.get("path", "/")]},
            }
        }
    elif net == "splithttp":
        stream["splithttpSettings"] = {"path": config.get("path", "/"), "host": config.get("host", "")}
    elif net == "xhttp":
        stream["xhttpSettings"] = {"path": config.get("path", "/"), "host": config.get("host", "")}

    if security in ("tls", "reality"):
        tls_obj: dict = {}
        sni = config.get("sni", config.get("host", ""))
        if sni:
            tls_obj["serverName"] = sni
        fp = config.get("fp", "")
        if fp:
            tls_obj["fingerprint"] = fp
        alpn = config.get("alpn", "")
        if alpn:
            tls_obj["alpn"] = alpn.split(",") if isinstance(alpn, str) else alpn
        tls_obj["allowInsecure"] = config.get("allowInsecure", "1") == "1"

        if security == "reality":
            tls_obj["publicKey"] = config.get("pbk", "")
            tls_obj["shortId"] = config.get("sid", "")
            tls_obj["spiderX"] = config.get("spx", "")
            stream["security"] = "reality"
            stream["realitySettings"] = tls_obj
        else:
            stream["security"] = "tls"
            stream["tlsSettings"] = tls_obj
    else:
        stream["security"] = "none"

    return stream


def _generate_xray_config(servers: list[XrayMonitorServer]) -> dict:
    """Generate a full xray-core config with socks inbounds and per-server outbounds."""
    inbounds = []
    outbounds = []
    rules = []

    for srv in servers:
        if not srv.socks_port:
            continue
        in_tag = f"in-{srv.id}"
        out_tag = f"out-{srv.id}"

        inbounds.append({
            "tag": in_tag,
            "port": srv.socks_port,
            "listen": "127.0.0.1",
            "protocol": "socks",
            "settings": {"udp": False},
        })

        outbounds.append(_build_outbound(srv))

        rules.append({
            "type": "field",
            "inboundTag": [in_tag],
            "outboundTag": out_tag,
        })

    if not outbounds:
        outbounds.append({"tag": "direct", "protocol": "freedom", "settings": {}})

    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {"rules": rules} if rules else {},
    }


class XrayMonitorService:
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._xray_proc: Optional[asyncio.subprocess.Process] = None
        self._config_dirty = True
        self._last_check: Optional[datetime] = None
        self._time_since_check = 0

    async def start(self):
        if self._running:
            return
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Xray monitor service started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._stop_xray()
        logger.info("Xray monitor service stopped")

    def mark_config_dirty(self):
        self._config_dirty = True

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "xray_running": self._xray_proc is not None and self._xray_proc.returncode is None,
            "last_check": self._last_check.isoformat() if self._last_check else None,
        }

    async def _loop(self):
        self._time_since_check = 0
        first_run = True

        while self._running:
            try:
                settings = await self._load_settings()
                interval = settings.check_interval if settings else 30

                if self._config_dirty:
                    await self._reload_xray()
                    self._config_dirty = False

                should_check = settings and settings.enabled and (
                    first_run or self._time_since_check >= interval
                )

                if should_check:
                    await self._check_all(settings)
                    self._time_since_check = 0
                    first_run = False

                await asyncio.sleep(1)
                self._time_since_check += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Xray monitor loop error: {e}")
                await asyncio.sleep(10)
                self._time_since_check += 10

    async def _load_settings(self) -> Optional[XrayMonitorSettings]:
        async with async_session() as db:
            result = await db.execute(select(XrayMonitorSettings).limit(1))
            return result.scalar_one_or_none()

    async def _reload_xray(self):
        """Regenerate config, assign ports, (re)start xray-core."""
        async with async_session() as db:
            result = await db.execute(
                select(XrayMonitorServer).where(XrayMonitorServer.enabled == True)  # noqa: E712
            )
            servers = list(result.scalars().all())

        port = SOCKS_PORT_BASE
        for srv in servers:
            srv.socks_port = port
            port += 1

        async with async_session() as db:
            for srv in servers:
                await db.execute(
                    XrayMonitorServer.__table__.update()
                    .where(XrayMonitorServer.id == srv.id)
                    .values(socks_port=srv.socks_port)
                )
            await db.commit()

        config = _generate_xray_config(servers)
        CONFIG_PATH.write_text(json.dumps(config, indent=2))
        logger.info(f"Xray monitor config generated: {len(servers)} servers")

        await self._stop_xray()
        if servers:
            await self._start_xray()

    async def _start_xray(self):
        if not Path(XRAY_BIN).exists():
            logger.warning(f"xray binary not found at {XRAY_BIN}")
            return
        try:
            self._xray_proc = await asyncio.create_subprocess_exec(
                XRAY_BIN, "run", "-c", str(CONFIG_PATH),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.sleep(1)
            if self._xray_proc.returncode is not None:
                stderr = await self._xray_proc.stderr.read()
                logger.error(f"xray-core failed to start: {stderr.decode()[:500]}")
                self._xray_proc = None
            else:
                logger.info(f"xray-core started (pid={self._xray_proc.pid})")
        except Exception as e:
            logger.error(f"Failed to start xray-core: {e}")
            self._xray_proc = None

    async def _stop_xray(self):
        if self._xray_proc and self._xray_proc.returncode is None:
            try:
                self._xray_proc.terminate()
                try:
                    await asyncio.wait_for(self._xray_proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._xray_proc.kill()
                    await self._xray_proc.wait()
                logger.info("xray-core stopped")
            except Exception as e:
                logger.warning(f"Error stopping xray-core: {e}")
        self._xray_proc = None

    async def _check_all(self, settings: XrayMonitorSettings):
        self._last_check = datetime.now(timezone.utc).replace(tzinfo=None)

        async with async_session() as db:
            result = await db.execute(
                select(XrayMonitorServer).where(
                    XrayMonitorServer.enabled == True,  # noqa: E712
                    XrayMonitorServer.socks_port.isnot(None),
                )
            )
            servers = list(result.scalars().all())

        if not servers:
            return

        xray_alive = self._xray_proc and self._xray_proc.returncode is None
        if not xray_alive:
            logger.warning("xray-core not running, restarting...")
            await self._reload_xray()
            self._config_dirty = False
            await asyncio.sleep(2)

        tasks = [self._check_server(srv, settings) for srv in servers]
        await asyncio.gather(*tasks, return_exceptions=True)

        await self._cleanup_old_checks()

    async def _check_server(self, srv: XrayMonitorServer, settings: XrayMonitorSettings):
        """Check one server through its SOCKS port."""
        proxy_url = f"socks5://127.0.0.1:{srv.socks_port}"
        ping_ms: Optional[float] = None
        error_msg: Optional[str] = None
        check_ok = False

        for target in ("http://google.com", "http://one.one.one.one"):
            try:
                start = time.monotonic()
                transport = httpx.AsyncHTTPTransport(proxy=proxy_url)
                async with httpx.AsyncClient(transport=transport, timeout=10, follow_redirects=True) as client:
                    resp = await client.get(target)
                elapsed = (time.monotonic() - start) * 1000
                if resp.status_code < 500:
                    ping_ms = round(elapsed, 1)
                    check_ok = True
                    break
            except Exception as e:
                error_msg = f"{target}: {type(e).__name__}"
                continue

        was_offline = srv.status == "offline"
        was_online = srv.status == "online"
        fail_threshold = settings.fail_threshold or 2

        async with async_session() as db:
            if check_ok:
                await db.execute(
                    XrayMonitorServer.__table__.update()
                    .where(XrayMonitorServer.id == srv.id)
                    .values(
                        status="online",
                        last_ping_ms=ping_ms,
                        last_check=self._last_check,
                        fail_count=0,
                    )
                )
                db.add(XrayMonitorCheck(
                    server_id=srv.id, status="ok", ping_ms=ping_ms,
                ))
            else:
                new_fail = (srv.fail_count or 0) + 1
                new_status = "offline" if new_fail >= fail_threshold else srv.status
                await db.execute(
                    XrayMonitorServer.__table__.update()
                    .where(XrayMonitorServer.id == srv.id)
                    .values(
                        status=new_status,
                        last_check=self._last_check,
                        fail_count=new_fail,
                        last_ping_ms=None,
                    )
                )
                db.add(XrayMonitorCheck(
                    server_id=srv.id, status="fail", error=error_msg,
                ))
            await db.commit()

        if check_ok and was_offline and settings.notify_recovery:
            await self._send_notification(
                settings, "info",
                f"✅ <b>Xray Monitor</b>\n\nСервер восстановлен:\n<b>{srv.name}</b> ({srv.address}:{srv.port})\nPing: {ping_ms} ms",
            )

        if check_ok and ping_ms and settings.notify_latency:
            threshold = settings.latency_threshold_ms or 500
            if ping_ms > threshold:
                await self._send_notification(
                    settings, "warning",
                    f"🟡 <b>Xray Monitor — High Latency</b>\n\n<b>{srv.name}</b> ({srv.address}:{srv.port})\nPing: {ping_ms} ms (порог: {threshold} ms)",
                )

        if not check_ok:
            new_fail = (srv.fail_count or 0) + 1
            if new_fail == fail_threshold and settings.notify_down:
                await self._send_notification(
                    settings, "critical",
                    f"🔴 <b>Xray Monitor — Server DOWN</b>\n\n<b>{srv.name}</b> ({srv.address}:{srv.port})\nОшибка: {error_msg}",
                )

    async def _send_notification(self, settings: XrayMonitorSettings, severity: str, message: str):
        bot_token = settings.telegram_bot_token
        chat_id = settings.telegram_chat_id

        if not settings.use_custom_bot or not bot_token or not chat_id:
            async with async_session() as db:
                result = await db.execute(select(AlertSettings).limit(1))
                alert_settings = result.scalar_one_or_none()
            if alert_settings and alert_settings.telegram_bot_token and alert_settings.telegram_chat_id:
                bot_token = alert_settings.telegram_bot_token
                chat_id = alert_settings.telegram_chat_id

        if not bot_token or not chat_id:
            return

        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                }) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"Telegram send failed ({resp.status}): {body[:200]}")
        except Exception as e:
            logger.error(f"Telegram send error: {e}")

    async def _cleanup_old_checks(self):
        """Remove check history older than retention period."""
        try:
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None)
            from datetime import timedelta
            cutoff -= timedelta(hours=CHECK_HISTORY_RETENTION_HOURS)
            async with async_session() as db:
                await db.execute(
                    delete(XrayMonitorCheck).where(XrayMonitorCheck.timestamp < cutoff)
                )
                await db.commit()
        except Exception as e:
            logger.debug(f"Cleanup old checks error: {e}")


_service: Optional[XrayMonitorService] = None


def get_xray_monitor_service() -> XrayMonitorService:
    global _service
    if _service is None:
        _service = XrayMonitorService()
    return _service


async def start_xray_monitor():
    svc = get_xray_monitor_service()
    await svc.start()


async def stop_xray_monitor():
    svc = get_xray_monitor_service()
    await svc.stop()
