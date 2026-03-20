"""Background service for monitoring Xray connections via speedtest through xray-core SOCKS5 proxies."""

import asyncio
import json
import logging
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from sqlalchemy import select, delete

from app.database import async_session
from app.models import (
    XrayMonitorSettings, XrayMonitorSubscription,
    XrayMonitorServer, XrayMonitorCheck, AlertSettings,
)
from app.services.xray_key_parser import is_valid_server, is_ignored_address, fetch_subscription

logger = logging.getLogger(__name__)

XRAY_BIN = "/usr/local/bin/xray"
SPEEDTEST_BIN = shutil.which("speedtest") or "/usr/local/bin/speedtest"
PROXYCHAINS_BIN = shutil.which("proxychains4") or "/usr/bin/proxychains4"
SOCKS_PORT_BASE = 10001
CONFIG_DIR = Path("/app/data/xray-monitor")
CONFIG_PATH = CONFIG_DIR / "config.json"
PROXYCHAINS_DIR = Path("/tmp/proxychains")
CHECK_HISTORY_RETENTION_HOURS = 24
SUB_REFRESH_INTERVAL_SEC = 3600
PAUSE_BETWEEN_SERVERS = 5
SPEEDTEST_TIMEOUT = 120


def _build_outbound(server: XrayMonitorServer) -> dict | None:
    """Build xray-core outbound JSON from a parsed server record."""
    if not is_valid_server(server.address, server.port):
        return None

    config = json.loads(server.config_json) if server.config_json else {}
    protocol = server.protocol
    tag = f"out-{server.id}"

    if protocol == "vless":
        stream = _build_stream_settings(config)
        user: dict = {"id": config.get("id", ""), "encryption": "none"}
        flow = config.get("flow", "")
        if flow:
            user["flow"] = flow
        return {
            "tag": tag,
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": server.address,
                    "port": server.port,
                    "users": [user],
                }]
            },
            "streamSettings": stream,
        }

    if protocol == "vmess":
        stream = _build_stream_settings(config)
        return {
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

    if protocol == "trojan":
        stream = _build_stream_settings(config)
        return {
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

    if protocol == "shadowsocks":
        return {
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

    return None


def _build_stream_settings(config: dict) -> dict:
    """Build streamSettings from parsed key params."""
    stream: dict = {}
    net = config.get("net", config.get("type", "tcp"))
    security = config.get("security", config.get("tls", ""))

    stream["network"] = net

    if net == "ws":
        ws: dict = {"path": config.get("path", "/")}
        if config.get("host"):
            ws["headers"] = {"Host": config["host"]}
        stream["wsSettings"] = ws
    elif net == "grpc":
        stream["grpcSettings"] = {"serviceName": config.get("serviceName", config.get("path", ""))}
    elif net in ("h2", "http"):
        stream["httpSettings"] = {
            "path": config.get("path", "/"),
            "host": [config["host"]] if config.get("host") else [],
        }
    elif net == "tcp" and config.get("headerType") == "http":
        stream["tcpSettings"] = {
            "header": {"type": "http", "request": {"path": [config.get("path", "/")]}},
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
        tls_obj["fingerprint"] = fp or "chrome"
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


def _generate_xray_config(servers: list[XrayMonitorServer]) -> tuple[dict, int]:
    """Generate xray-core config, returns (config_dict, valid_count)."""
    inbounds = []
    outbounds = []
    rules = []
    skipped = 0

    for srv in servers:
        if not srv.socks_port:
            continue

        outbound = _build_outbound(srv)
        if outbound is None:
            skipped += 1
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
        outbounds.append(outbound)
        rules.append({"type": "field", "inboundTag": [in_tag], "outboundTag": out_tag})

    if not outbounds:
        outbounds.append({"tag": "direct", "protocol": "freedom", "settings": {}})

    config = {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {"rules": rules} if rules else {},
    }
    if skipped:
        logger.info(f"Skipped {skipped} servers with invalid address/port")
    return config, len(inbounds)


def _write_proxychains_config(socks_port: int, server_id: int) -> Path:
    """Write a proxychains4 config file for a specific SOCKS5 port."""
    PROXYCHAINS_DIR.mkdir(parents=True, exist_ok=True)
    conf_path = PROXYCHAINS_DIR / f"pc_{server_id}.conf"
    conf_path.write_text(
        "strict_chain\n"
        "quiet_mode\n"
        "proxy_dns\n"
        "[ProxyList]\n"
        f"socks5 127.0.0.1 {socks_port}\n"
    )
    return conf_path


async def _run_speedtest_via_proxy(socks_port: int, server_id: int) -> dict:
    """Run Ookla speedtest CLI through proxychains4 SOCKS5 proxy.

    Returns dict with keys: ok, download_mbps, upload_mbps, ping_ms, error, server_name.
    """
    conf_path = _write_proxychains_config(socks_port, server_id)

    cmd = [
        PROXYCHAINS_BIN, "-q", "-f", str(conf_path),
        SPEEDTEST_BIN, "--format=json", "--accept-license", "--accept-gdpr",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SPEEDTEST_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return {"ok": False, "error": "speedtest timeout"}
    except FileNotFoundError as e:
        return {"ok": False, "error": f"binary not found: {e.filename}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    finally:
        try:
            conf_path.unlink(missing_ok=True)
        except Exception:
            pass

    raw = stdout.decode(errors="replace").strip()
    if proc.returncode != 0 or not raw:
        err = stderr.decode(errors="replace").strip()
        if not err:
            err = raw or f"exit code {proc.returncode}"
        return {"ok": False, "error": err[:300]}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"invalid JSON: {raw[:200]}"}

    if "error" in data:
        return {"ok": False, "error": data["error"][:300]}

    dl_bw = data.get("download", {}).get("bandwidth", 0)
    ul_bw = data.get("upload", {}).get("bandwidth", 0)
    download_mbps = round(dl_bw * 8 / 1_000_000, 2) if dl_bw else 0
    upload_mbps = round(ul_bw * 8 / 1_000_000, 2) if ul_bw else 0
    ping_ms = round(data.get("ping", {}).get("latency", 0), 1)

    srv_info = data.get("server", {})
    server_name = srv_info.get("name", "")
    location = srv_info.get("location", "")
    if location:
        server_name = f"{server_name} ({location})"

    return {
        "ok": True,
        "download_mbps": download_mbps,
        "upload_mbps": upload_mbps,
        "ping_ms": ping_ms,
        "server_name": server_name,
        "server_host": srv_info.get("host", ""),
    }


class XrayMonitorService:
    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._xray_proc: Optional[asyncio.subprocess.Process] = None
        self._config_dirty = True
        self._last_check: Optional[datetime] = None
        self._time_since_check = 0
        self._time_since_refresh = 0
        self._xray_healthy = False
        self._testing_server_id: Optional[int] = None

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
            "xray_running": self._xray_healthy,
            "last_check": self._last_check.isoformat() if self._last_check else None,
            "testing_server_id": self._testing_server_id,
        }

    # ------------------------------------------------------------------ loop
    async def _loop(self):
        self._time_since_check = 0
        self._time_since_refresh = SUB_REFRESH_INTERVAL_SEC - 10

        while self._running:
            try:
                settings = await self._load_settings()

                if self._config_dirty:
                    await self._reload_xray()
                    self._config_dirty = False

                if self._time_since_refresh >= SUB_REFRESH_INTERVAL_SEC:
                    await self._auto_refresh_subscriptions()
                    self._time_since_refresh = 0

                speedtest_enabled = settings and settings.speedtest_enabled
                interval_sec = max(600, (settings.speedtest_interval or 30) * 60) if settings else 1800

                should_check = speedtest_enabled and self._time_since_check >= interval_sec

                if should_check:
                    await self._check_all(settings)
                    self._time_since_check = 0

                await asyncio.sleep(1)
                self._time_since_check += 1
                self._time_since_refresh += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Xray monitor loop error: {e}")
                await asyncio.sleep(10)
                self._time_since_check += 10
                self._time_since_refresh += 10

    async def _load_settings(self) -> Optional[XrayMonitorSettings]:
        async with async_session() as db:
            result = await db.execute(select(XrayMonitorSettings).limit(1))
            return result.scalar_one_or_none()

    # ------------------------------------------------------------------ xray management
    async def _reload_xray(self):
        """Regenerate config, assign ports, (re)start xray-core."""
        ignore_set = await self._load_ignore_set()

        async with async_session() as db:
            result = await db.execute(
                select(XrayMonitorServer).where(XrayMonitorServer.enabled == True)  # noqa: E712
            )
            servers = list(result.scalars().all())

        valid_servers = [
            s for s in servers
            if is_valid_server(s.address, s.port)
            and not is_ignored_address(s.address, ignore_set)
        ]

        port = SOCKS_PORT_BASE
        for srv in valid_servers:
            srv.socks_port = port
            port += 1

        async with async_session() as db:
            for srv in valid_servers:
                await db.execute(
                    XrayMonitorServer.__table__.update()
                    .where(XrayMonitorServer.id == srv.id)
                    .values(socks_port=srv.socks_port)
                )
            invalid_ids = [s.id for s in servers if not is_valid_server(s.address, s.port)]
            if invalid_ids:
                for inv_id in invalid_ids:
                    await db.execute(
                        XrayMonitorServer.__table__.update()
                        .where(XrayMonitorServer.id == inv_id)
                        .values(socks_port=None)
                    )
            await db.commit()

        config, valid_count = _generate_xray_config(valid_servers)
        CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False))
        logger.info(f"Xray monitor config generated: {valid_count} valid servers (total {len(servers)})")

        await self._stop_xray()
        if valid_count > 0:
            await self._start_xray()
        else:
            self._xray_healthy = False

    async def _start_xray(self):
        if not Path(XRAY_BIN).exists():
            logger.warning(f"xray binary not found at {XRAY_BIN}")
            self._xray_healthy = False
            return

        try:
            test_proc = await asyncio.create_subprocess_exec(
                XRAY_BIN, "run", "-test", "-c", str(CONFIG_PATH),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(test_proc.communicate(), timeout=15)
            if test_proc.returncode != 0:
                combined = (stdout.decode() + stderr.decode()).strip()
                logger.error(f"xray config validation failed (exit {test_proc.returncode}): {combined[:800]}")
                self._xray_healthy = False
                return
        except asyncio.TimeoutError:
            logger.error("xray config validation timed out")
            self._xray_healthy = False
            return
        except Exception as e:
            logger.error(f"xray config validation error: {e}")
            self._xray_healthy = False
            return

        try:
            self._xray_proc = await asyncio.create_subprocess_exec(
                XRAY_BIN, "run", "-c", str(CONFIG_PATH),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.sleep(2)
            if self._xray_proc.returncode is not None:
                stdout = await self._xray_proc.stdout.read()
                stderr = await self._xray_proc.stderr.read()
                combined = (stdout.decode() + stderr.decode()).strip()
                logger.error(f"xray-core exited immediately (code {self._xray_proc.returncode}): {combined[:800]}")
                self._xray_proc = None
                self._xray_healthy = False
            else:
                logger.info(f"xray-core started (pid={self._xray_proc.pid})")
                self._xray_healthy = True
        except Exception as e:
            logger.error(f"Failed to start xray-core: {e}")
            self._xray_proc = None
            self._xray_healthy = False

    async def _stop_xray(self):
        if self._xray_proc and self._xray_proc.returncode is None:
            try:
                self._xray_proc.terminate()
                try:
                    await asyncio.wait_for(self._xray_proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._xray_proc.kill()
                    await self._xray_proc.wait()
            except Exception as e:
                logger.warning(f"Error stopping xray-core: {e}")
        self._xray_proc = None
        self._xray_healthy = False

    # ------------------------------------------------------------------ speedtest checks
    async def _ensure_xray_running(self) -> bool:
        xray_alive = self._xray_proc and self._xray_proc.returncode is None
        if not xray_alive:
            logger.warning("xray-core not running, attempting restart...")
            await self._reload_xray()
            self._config_dirty = False
            await asyncio.sleep(2)
        return self._xray_healthy

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

            sub_result = await db.execute(select(XrayMonitorSubscription))
            sub_names = {s.id: s.name for s in sub_result.scalars().all()}

        if not servers:
            return

        if not await self._ensure_xray_running():
            logger.error("xray-core still not running, skipping checks")
            return

        events: list[dict] = []
        for i, srv in enumerate(servers):
            if not self._running:
                break
            try:
                srv_events = await self._speedtest_server(srv, settings)
                events.extend(srv_events)
            except Exception as e:
                logger.warning(f"Speedtest failed for {srv.name}: {e}")

            if self._running and i < len(servers) - 1:
                await asyncio.sleep(PAUSE_BETWEEN_SERVERS)

        if events:
            await self._send_batched_notifications(settings, events, sub_names)

        await self._cleanup_old_checks()
        logger.info(f"Speedtest cycle completed: {len(servers)} servers")

    async def _speedtest_server(self, srv: XrayMonitorServer, settings: XrayMonitorSettings) -> list[dict]:
        """Run speedtest through Xray SOCKS5 proxy for a single server."""
        if not self._xray_healthy:
            return []

        self._testing_server_id = srv.id
        try:
            return await self._do_speedtest_server(srv, settings)
        finally:
            self._testing_server_id = None

    async def _do_speedtest_server(self, srv: XrayMonitorServer, settings: XrayMonitorSettings) -> list[dict]:
        logger.info(f"Speedtest: {srv.name} ({srv.address}:{srv.port}) via SOCKS5 :{srv.socks_port}")

        result = await _run_speedtest_via_proxy(srv.socks_port, srv.id)

        was_offline = srv.status == "offline"
        fail_threshold = settings.fail_threshold or 2
        check_time = datetime.now(timezone.utc).replace(tzinfo=None)

        check_ok = result["ok"]
        ping_ms = result.get("ping_ms") if check_ok else None
        download_mbps = result.get("download_mbps") if check_ok else None
        upload_mbps = result.get("upload_mbps") if check_ok else None
        error_msg = result.get("error") if not check_ok else None

        if check_ok:
            logger.info(
                f"Speedtest {srv.name}: {download_mbps} Mbit/s down, "
                f"{upload_mbps} Mbit/s up, {ping_ms} ms ping"
            )

        async with async_session() as db:
            if check_ok:
                await db.execute(
                    XrayMonitorServer.__table__.update()
                    .where(XrayMonitorServer.id == srv.id)
                    .values(
                        status="online",
                        last_ping_ms=ping_ms,
                        last_download_mbps=download_mbps,
                        last_upload_mbps=upload_mbps,
                        last_check=check_time,
                        fail_count=0,
                    )
                )
                db.add(XrayMonitorCheck(
                    server_id=srv.id, status="ok",
                    ping_ms=ping_ms, download_mbps=download_mbps, upload_mbps=upload_mbps,
                ))
            else:
                new_fail = (srv.fail_count or 0) + 1
                new_status = "offline" if new_fail >= fail_threshold else srv.status
                await db.execute(
                    XrayMonitorServer.__table__.update()
                    .where(XrayMonitorServer.id == srv.id)
                    .values(
                        status=new_status, last_check=check_time,
                        fail_count=new_fail, last_ping_ms=None,
                        last_download_mbps=None, last_upload_mbps=None,
                    )
                )
                db.add(XrayMonitorCheck(server_id=srv.id, status="fail", error=error_msg))
            await db.commit()

        base = {
            "name": srv.name, "address": srv.address,
            "port": srv.port, "sub_id": srv.subscription_id,
        }
        events: list[dict] = []

        if check_ok and was_offline and settings.notify_recovery:
            events.append({**base, "type": "recovery", "ping_ms": ping_ms, "download_mbps": download_mbps})

        if check_ok and ping_ms and settings.notify_latency:
            threshold = settings.latency_threshold_ms or 500
            if ping_ms > threshold:
                events.append({**base, "type": "latency", "ping_ms": ping_ms, "threshold": threshold})

        if check_ok and download_mbps is not None and settings.notify_slow_speed:
            speed_threshold = settings.speed_threshold_mbps or 100
            if download_mbps < speed_threshold:
                events.append({
                    **base, "type": "slow_speed",
                    "download_mbps": download_mbps, "threshold": speed_threshold,
                })

        if not check_ok:
            new_fail = (srv.fail_count or 0) + 1
            if new_fail == fail_threshold and settings.notify_down:
                err = error_msg or ""
                if len(err) > 80:
                    err = err[:80] + "…"
                events.append({**base, "type": "down", "error": err})

        return events

    async def run_manual_speedtest(self, server_id: int) -> dict:
        """Run speedtest for a single server on demand. Returns result dict."""
        if not await self._ensure_xray_running():
            return {"ok": False, "error": "xray-core is not running"}

        async with async_session() as db:
            result = await db.execute(
                select(XrayMonitorServer).where(
                    XrayMonitorServer.id == server_id,
                    XrayMonitorServer.socks_port.isnot(None),
                )
            )
            srv = result.scalar_one_or_none()

        if not srv:
            return {"ok": False, "error": "Server not found or has no SOCKS port"}

        if self._testing_server_id:
            return {"ok": False, "error": "Another speedtest is in progress"}

        self._testing_server_id = srv.id
        try:
            st_result = await _run_speedtest_via_proxy(srv.socks_port, srv.id)

            check_time = datetime.now(timezone.utc).replace(tzinfo=None)
            async with async_session() as db:
                if st_result["ok"]:
                    await db.execute(
                        XrayMonitorServer.__table__.update()
                        .where(XrayMonitorServer.id == srv.id)
                        .values(
                            status="online",
                            last_ping_ms=st_result.get("ping_ms"),
                            last_download_mbps=st_result.get("download_mbps"),
                            last_upload_mbps=st_result.get("upload_mbps"),
                            last_check=check_time,
                            fail_count=0,
                        )
                    )
                    db.add(XrayMonitorCheck(
                        server_id=srv.id, status="ok",
                        ping_ms=st_result.get("ping_ms"),
                        download_mbps=st_result.get("download_mbps"),
                        upload_mbps=st_result.get("upload_mbps"),
                    ))
                else:
                    db.add(XrayMonitorCheck(
                        server_id=srv.id, status="fail",
                        error=st_result.get("error", "")[:500],
                    ))
                await db.commit()

            return st_result
        finally:
            self._testing_server_id = None

    # ------------------------------------------------------------------ auto-refresh
    async def _load_ignore_set(self) -> set[str]:
        try:
            async with async_session() as db:
                result = await db.execute(select(XrayMonitorSettings).limit(1))
                s = result.scalar_one_or_none()
            if not s or not s.ignore_list:
                return set()
            return set(json.loads(s.ignore_list))
        except Exception:
            return set()

    async def _auto_refresh_subscriptions(self):
        """Refresh all auto_refresh subscriptions every hour."""
        try:
            ignore_set = await self._load_ignore_set()

            async with async_session() as db:
                result = await db.execute(
                    select(XrayMonitorSubscription).where(
                        XrayMonitorSubscription.enabled == True,  # noqa: E712
                        XrayMonitorSubscription.auto_refresh == True,  # noqa: E712
                    )
                )
                subs = list(result.scalars().all())

            if not subs:
                return

            changed = False
            for sub in subs:
                try:
                    keys = await fetch_subscription(sub.url)
                    valid_keys = [
                        k for k in keys
                        if is_valid_server(k.get("address", ""), int(k.get("port", 0)))
                        and not is_ignored_address(k.get("address", ""), ignore_set)
                    ]

                    async with async_session() as db:
                        await db.execute(
                            delete(XrayMonitorServer).where(XrayMonitorServer.subscription_id == sub.id)
                        )
                        count = 0
                        for idx, k in enumerate(valid_keys):
                            db.add(XrayMonitorServer(
                                subscription_id=sub.id,
                                position=idx,
                                name=k.get("name", "Unknown"),
                                protocol=k.get("protocol", "unknown"),
                                address=k.get("address", "").strip(),
                                port=int(k.get("port", 0)),
                                raw_key=k.get("raw_key", ""),
                                config_json=json.dumps(k.get("config", {}), ensure_ascii=False),
                            ))
                            count += 1

                        await db.execute(
                            XrayMonitorSubscription.__table__.update()
                            .where(XrayMonitorSubscription.id == sub.id)
                            .values(
                                server_count=count,
                                last_refreshed=datetime.now(timezone.utc).replace(tzinfo=None),
                                last_error=None,
                            )
                        )
                        await db.commit()
                    changed = True
                    logger.info(f"Auto-refreshed subscription '{sub.name}': {count} servers")
                except Exception as e:
                    logger.warning(f"Failed to auto-refresh subscription '{sub.name}': {e}")
                    async with async_session() as db:
                        await db.execute(
                            XrayMonitorSubscription.__table__.update()
                            .where(XrayMonitorSubscription.id == sub.id)
                            .values(last_error=str(e)[:500])
                        )
                        await db.commit()

            if changed:
                self._config_dirty = True

        except Exception as e:
            logger.error(f"Auto-refresh subscriptions error: {e}")

    # ------------------------------------------------------------------ telegram
    _CATEGORY_ORDER = ("down", "recovery", "latency", "slow_speed")
    _CATEGORY_HEADER = {
        "down": "\U0001f534 <b>Серверы DOWN</b>",
        "recovery": "\u2705 <b>Восстановились</b>",
        "latency": "\U0001f7e1 <b>Высокий пинг</b>",
        "slow_speed": "\U0001f7e0 <b>Низкая скорость</b>",
    }
    TG_MSG_LIMIT = 4096

    async def _send_batched_notifications(
        self,
        settings: XrayMonitorSettings,
        events: list[dict],
        sub_names: dict[int, str],
    ):
        by_cat: dict[str, list[dict]] = {}
        for ev in events:
            by_cat.setdefault(ev["type"], []).append(ev)

        sections: list[str] = []
        for cat in self._CATEGORY_ORDER:
            items = by_cat.get(cat)
            if not items:
                continue

            lines = [self._CATEGORY_HEADER[cat]]

            by_source: dict[Optional[int], list[dict]] = {}
            for ev in items:
                by_source.setdefault(ev["sub_id"], []).append(ev)

            for sub_id in sorted(by_source, key=lambda x: (x is None, x or 0)):
                group = by_source[sub_id]
                if sub_id and sub_id in sub_names:
                    lines.append(f"\n\U0001f4e6 <b>{sub_names[sub_id]}</b>")
                elif sub_id is None:
                    lines.append("\n\U0001f511 <b>Ручные ключи</b>")
                else:
                    lines.append(f"\n\U0001f4e6 <b>Подписка #{sub_id}</b>")

                for ev in group:
                    entry = f"  \u2022 {ev['name']} ({ev['address']}:{ev['port']})"
                    if cat == "down":
                        entry += f" \u2014 {ev.get('error', '?')}"
                    elif cat == "recovery":
                        ping = ev.get('ping_ms', '?')
                        dl = ev.get('download_mbps', '')
                        entry += f" \u2014 {ping} ms"
                        if dl:
                            entry += f", {dl} Mbit/s"
                    elif cat == "latency":
                        entry += f" \u2014 {ev.get('ping_ms', '?')} ms (\u043f\u043e\u0440\u043e\u0433 {ev.get('threshold', '?')})"
                    elif cat == "slow_speed":
                        entry += f" \u2014 {ev.get('download_mbps', '?')} Mbit/s (\u043f\u043e\u0440\u043e\u0433 {ev.get('threshold', '?')})"
                    lines.append(entry)

            sections.append("\n".join(lines))

        if not sections:
            return

        header = "\U0001f4e1 <b>Xray Monitor</b>\n"
        full_text = header + "\n\n".join(sections)

        for chunk in self._split_message(full_text):
            await self._send_telegram(settings, chunk)

    @staticmethod
    def _split_message(text: str, limit: int = 4096) -> list[str]:
        if len(text) <= limit:
            return [text]

        chunks: list[str] = []
        while text:
            if len(text) <= limit:
                chunks.append(text)
                break

            cut = text.rfind("\n\n", 0, limit)
            if cut < 1:
                cut = text.rfind("\n", 0, limit)
            if cut < 1:
                cut = limit

            chunks.append(text[:cut].rstrip())
            text = text[cut:].lstrip("\n")

        return chunks

    async def _send_telegram(self, settings: XrayMonitorSettings, message: str):
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

    # ------------------------------------------------------------------ cleanup
    async def _cleanup_old_checks(self):
        try:
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=CHECK_HISTORY_RETENTION_HOURS)
            async with async_session() as db:
                await db.execute(delete(XrayMonitorCheck).where(XrayMonitorCheck.timestamp < cutoff))
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
