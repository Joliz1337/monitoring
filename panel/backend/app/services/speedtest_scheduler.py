"""Background speedtest scheduler.

Tests nodes sequentially (one at a time) to avoid overloading iperf3 servers.
Supports three methods: iperf3 (improved), Ookla CLI, auto (geo-based selection).
"""

import asyncio
import json
import logging
import shutil
import signal
from typing import Optional

import aiohttp
import httpx
from sqlalchemy import select, update

from app.database import async_session
from app.models import Server, PanelSettings, AlertSettings

logger = logging.getLogger(__name__)

DEFAULT_IPERF_SERVERS = [
    # Europe
    {"host": "ping.online.net", "port": 5200, "label": "Online.net (100G)", "region": "EU-FR"},
    {"host": "speedtest.serverius.net", "port": 5002, "label": "Serverius (10G)", "region": "EU-NL"},
    {"host": "iperf3.moji.fr", "port": 5200, "label": "Moji (100G)", "region": "EU-FR"},
    {"host": "paris.bbr.iperf.bytel.fr", "port": 9200, "label": "Bouygues (10G)", "region": "EU-FR"},
    # Russia
    {"host": "spd-rudp.hostkey.ru", "port": 5201, "label": "Hostkey Moscow", "region": "RU-MOW"},
    {"host": "st.spb.ertelecom.ru", "port": 5201, "label": "Ertelecom SPb", "region": "RU-SPB"},
    {"host": "st.ekat.ertelecom.ru", "port": 5201, "label": "Ertelecom Yekaterinburg", "region": "RU-SVE"},
    # Asia
    {"host": "speedtest.uztelecom.uz", "port": 5200, "label": "Uztelecom (10G)", "region": "Asia-UZ"},
    {"host": "iperf.biznetnetworks.com", "port": 5201, "label": "Biznet Jakarta", "region": "Asia-SG"},
    {"host": "iperf3.as49465.net", "port": 5200, "label": "AS49465 Tokyo", "region": "Asia-JP"},
    # US
    {"host": "iperf3.he.net", "port": 5201, "label": "Hurricane Electric", "region": "US-LA"},
    {"host": "nyc.speedtest.clouvider.net", "port": 5200, "label": "Clouvider NYC", "region": "US-NY"},
]

OOKLA_BLOCKED_REGIONS = {"RU"}

SETTINGS_KEYS = {
    "speedtest_enabled": "true",
    "speedtest_method": "auto",
    "speedtest_mode": "both",
    "speedtest_servers": json.dumps(DEFAULT_IPERF_SERVERS),
    "speedtest_threshold": "500",
    "speedtest_interval": "60",
    "speedtest_duration": "5",
    "speedtest_streams": "4",
    "speedtest_test_mode": "quick",
    "speedtest_panel_port": "5201",
    "speedtest_panel_address": "",
    "speedtest_notify_slow": "true",
    "speedtest_notify_error": "true",
    "speedtest_notify_recovery": "false",
    "speedtest_use_custom_bot": "false",
    "speedtest_bot_token": "",
    "speedtest_chat_id": "",
    "speedtest_ignore_list": "[]",
}


class SpeedtestScheduler:
    PAUSE_BETWEEN_NODES = 5
    TG_MSG_LIMIT = 4096

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._settings_task: Optional[asyncio.Task] = None
        self._iperf_server_proc: Optional[asyncio.subprocess.Process] = None

        self._enabled = True
        self._method = "auto"
        self._mode = "both"
        self._servers = list(DEFAULT_IPERF_SERVERS)
        self._threshold = 500.0
        self._interval = 60
        self._duration = 5
        self._streams = 4
        self._test_mode = "quick"
        self._panel_port = 5201
        self._panel_address = ""

        self._notify_slow = True
        self._notify_error = True
        self._notify_recovery = False
        self._use_custom_bot = False
        self._bot_token = ""
        self._chat_id = ""
        self._ignore_list: set[int] = set()

        self._prev_slow: set[int] = set()

    async def _load_settings(self):
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(PanelSettings).where(
                        PanelSettings.key.in_(SETTINGS_KEYS.keys())
                    )
                )
                db_settings = {s.key: s.value for s in result.scalars().all()}

            def _get(key: str) -> str:
                return db_settings.get(key, SETTINGS_KEYS[key])

            self._enabled = _get("speedtest_enabled").lower() == "true"
            raw_method = _get("speedtest_method")
            self._method = raw_method if raw_method in ("auto", "ookla", "iperf3") else "auto"
            self._mode = _get("speedtest_mode")
            self._threshold = float(_get("speedtest_threshold"))
            self._interval = max(1, int(_get("speedtest_interval")))
            self._duration = max(1, min(30, int(_get("speedtest_duration"))))
            self._streams = max(1, min(64, int(_get("speedtest_streams"))))
            raw_mode = _get("speedtest_test_mode")
            self._test_mode = "full" if raw_mode == "full" else "quick"
            self._panel_port = int(_get("speedtest_panel_port"))
            self._panel_address = _get("speedtest_panel_address")

            try:
                self._servers = json.loads(_get("speedtest_servers"))
                if not isinstance(self._servers, list):
                    self._servers = list(DEFAULT_IPERF_SERVERS)
            except (json.JSONDecodeError, TypeError):
                self._servers = list(DEFAULT_IPERF_SERVERS)

            self._notify_slow = _get("speedtest_notify_slow").lower() == "true"
            self._notify_error = _get("speedtest_notify_error").lower() == "true"
            self._notify_recovery = _get("speedtest_notify_recovery").lower() == "true"
            self._use_custom_bot = _get("speedtest_use_custom_bot").lower() == "true"
            self._bot_token = _get("speedtest_bot_token")
            self._chat_id = _get("speedtest_chat_id")

            try:
                ignore_raw = json.loads(_get("speedtest_ignore_list"))
                self._ignore_list = {int(x) for x in ignore_raw if str(x).isdigit()}
            except (json.JSONDecodeError, TypeError):
                self._ignore_list = set()

        except Exception as e:
            logger.debug(f"Failed to load speedtest settings: {e}")

    async def _settings_loop(self):
        while self._running:
            try:
                await asyncio.sleep(30)
                await self._load_settings()
                await self._manage_iperf_server()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Speedtest settings reload error: {e}")

    async def _manage_iperf_server(self):
        """Start or stop the panel-side iperf3 server based on mode."""
        import os
        if os.environ.get("IPERF_SERVER_DISABLED", "").lower() in ("1", "true", "yes"):
            should_run = False
        else:
            should_run = self._mode in ("panel", "both") and self._enabled
        iperf3_bin = shutil.which("iperf3") or "/usr/bin/iperf3"

        if should_run:
            alive = self._iperf_server_proc is not None and self._iperf_server_proc.returncode is None
            if not alive:
                self._iperf_server_proc = None
                try:
                    self._iperf_server_proc = await asyncio.create_subprocess_exec(
                        iperf3_bin, "-s", "-p", str(self._panel_port),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    logger.info(f"iperf3 server started on port {self._panel_port}")
                except Exception as e:
                    logger.warning(f"Failed to start iperf3 server: {e}")
                    self._iperf_server_proc = None

        if not should_run and self._iperf_server_proc is not None:
            await self._stop_iperf_server()

    async def _stop_iperf_server(self):
        if self._iperf_server_proc and self._iperf_server_proc.returncode is None:
            try:
                self._iperf_server_proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(self._iperf_server_proc.wait(), timeout=5)
            except Exception:
                try:
                    self._iperf_server_proc.kill()
                except Exception:
                    pass
            logger.info("iperf3 server stopped")
        self._iperf_server_proc = None

    def _build_server_list(self) -> list[dict]:
        """Build the server list based on mode (public / panel / both)."""
        servers = []

        if self._mode in ("public", "both"):
            servers.extend(self._servers)

        if self._mode in ("panel", "both") and self._panel_address:
            servers.append({
                "host": self._panel_address,
                "port": self._panel_port,
                "label": "Panel",
                "region": "panel",
            })

        return servers

    async def _build_server_list_for_node(self, server: Server) -> list[dict]:
        """Build geo-filtered iperf3 server list for a specific node."""
        all_servers = self._build_server_list()
        if not all_servers:
            return []

        geo_region = getattr(server, "geo_region", None)
        if not geo_region:
            try:
                from app.services.geo_resolver import resolve_server_geo
                geo_region = await resolve_server_geo(server)
            except Exception:
                pass

        if not geo_region:
            return all_servers

        from app.services.geo_resolver import filter_servers_by_geo
        filtered = filter_servers_by_geo(all_servers, geo_region)
        return filtered if filtered else all_servers

    def _resolve_method_for_node(self, server: Server) -> str:
        """Determine the test method for a node based on settings and geo."""
        if self._method in ("ookla", "iperf3"):
            return self._method

        geo_region = getattr(server, "geo_region", None) or ""
        if geo_region in OOKLA_BLOCKED_REGIONS:
            return "iperf3"
        return "ookla"

    async def start(self):
        if self._running:
            return
        await self._load_settings()
        self._running = True
        self._task = asyncio.create_task(self._main_loop())
        self._settings_task = asyncio.create_task(self._settings_loop())
        await self._manage_iperf_server()
        logger.info(f"Speedtest scheduler started (interval: {self._interval}min, method: {self._method})")

    async def stop(self):
        self._running = False
        for task in (self._task, self._settings_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self._stop_iperf_server()
        logger.info("Speedtest scheduler stopped")

    async def _main_loop(self):
        await asyncio.sleep(30)
        while self._running:
            try:
                if self._enabled:
                    await self._test_all_servers()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Speedtest cycle error: {e}")

            await asyncio.sleep(self._interval * 60)

    async def _test_all_servers(self):
        """Test all active nodes sequentially."""
        async with async_session() as db:
            result = await db.execute(
                select(Server).where(Server.is_active == True)
            )
            servers = result.scalars().all()

        if not servers:
            return

        servers = [s for s in servers if s.id not in self._ignore_list]
        if not servers:
            return

        logger.info(f"Speedtest: starting cycle for {len(servers)} nodes (method: {self._method})")

        events: list[dict] = []
        for srv in servers:
            if not self._running:
                break
            try:
                node_method = self._resolve_method_for_node(srv)
                node_servers = await self._build_server_list_for_node(srv) if node_method == "iperf3" else []
                event = await self._test_single_node(srv, node_servers, node_method)
                if event:
                    events.append(event)
            except Exception as e:
                logger.debug(f"Speedtest failed for {srv.name}: {e}")

            if self._running:
                await asyncio.sleep(self.PAUSE_BETWEEN_NODES)

        logger.info("Speedtest: cycle completed")

        if events:
            await self._send_notifications(events)

    async def _test_single_node(
        self, server: Server, server_list: list[dict], method: str = "iperf3",
    ) -> Optional[dict]:
        """Send speedtest request to a single node and store result."""
        payload: dict = {
            "duration": self._duration,
            "streams": self._streams,
            "threshold_mbps": self._threshold,
            "test_mode": self._test_mode,
            "method": method,
        }

        if method == "iperf3":
            if not server_list:
                base_list = self._build_server_list()
                if not base_list:
                    logger.warning(f"Speedtest {server.name}: no iperf3 servers configured")
                    return None
                server_list = base_list
            payload["servers"] = server_list

        if method == "ookla":
            timeout = 150
        else:
            timeout = self._duration * max(len(server_list), 1) + 60

        try:
            async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
                response = await client.post(
                    f"{server.url}/api/speedtest",
                    headers={"X-API-Key": server.api_key},
                    json=payload,
                )

                if response.status_code == 200:
                    result = response.json()
                    speed = result.get("best_speed_mbps", 0)
                    logger.info(f"Speedtest {server.name} [{method}]: {speed:.1f} Mbit/s")
                elif response.status_code == 409:
                    logger.debug(f"Speedtest {server.name}: test already in progress")
                    return None
                else:
                    logger.warning(f"Speedtest {server.name}: HTTP {response.status_code}")
                    if self._notify_error:
                        self._prev_slow.discard(server.id)
                        return {"type": "error", "name": server.name, "detail": f"HTTP {response.status_code}"}
                    return None
        except httpx.TimeoutException:
            logger.warning(f"Speedtest {server.name}: timeout")
            if self._notify_error:
                self._prev_slow.discard(server.id)
                return {"type": "error", "name": server.name, "detail": "timeout"}
            return None
        except Exception as e:
            logger.debug(f"Speedtest {server.name}: {e}")
            if self._notify_error:
                self._prev_slow.discard(server.id)
                return {"type": "error", "name": server.name, "detail": str(e)[:100]}
            return None

        for attempt in range(1, 4):
            try:
                async with async_session() as db:
                    await db.execute(
                        update(Server).where(Server.id == server.id).values(
                            last_speedtest=json.dumps(result)
                        )
                    )
                    await db.commit()
                break
            except Exception as db_err:
                if "deadlock" in str(db_err).lower() and attempt < 3:
                    await asyncio.sleep(0.3 * attempt)
                    continue
                logger.warning(f"Failed to save speedtest for {server.name}: {db_err}")

        speed = result.get("best_speed_mbps", 0)
        was_slow = server.id in self._prev_slow
        is_slow = speed < self._threshold

        if is_slow:
            self._prev_slow.add(server.id)
            if self._notify_slow:
                return {"type": "slow", "name": server.name, "speed": speed, "threshold": self._threshold}
        else:
            self._prev_slow.discard(server.id)
            if was_slow and self._notify_recovery:
                return {"type": "recovery", "name": server.name, "speed": speed}

        return None

    async def _send_notifications(self, events: list[dict]):
        sections: list[str] = []

        slow = [e for e in events if e["type"] == "slow"]
        errors = [e for e in events if e["type"] == "error"]
        recovered = [e for e in events if e["type"] == "recovery"]

        if slow:
            lines = ["\U0001f534 <b>\u041d\u0438\u0437\u043a\u0430\u044f \u0441\u043a\u043e\u0440\u043e\u0441\u0442\u044c</b>"]
            for e in slow:
                lines.append(f"  \u2022 {e['name']} \u2014 {e['speed']:.1f} Mbit/s (\u043f\u043e\u0440\u043e\u0433 {e['threshold']:.0f})")
            sections.append("\n".join(lines))

        if errors:
            lines = ["\u26a0\ufe0f <b>\u041e\u0448\u0438\u0431\u043a\u0438 \u0442\u0435\u0441\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u044f</b>"]
            for e in errors:
                lines.append(f"  \u2022 {e['name']} \u2014 {e['detail']}")
            sections.append("\n".join(lines))

        if recovered:
            lines = ["\u2705 <b>\u0421\u043a\u043e\u0440\u043e\u0441\u0442\u044c \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0430</b>"]
            for e in recovered:
                lines.append(f"  \u2022 {e['name']} \u2014 {e['speed']:.1f} Mbit/s")
            sections.append("\n".join(lines))

        if not sections:
            return

        header = "\U0001f680 <b>Speed Test</b>\n"
        full_text = header + "\n\n".join(sections)

        for chunk in self._split_message(full_text):
            await self._send_telegram(chunk)

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

    async def _send_telegram(self, message: str):
        bot_token = self._bot_token if self._use_custom_bot else ""
        chat_id = self._chat_id if self._use_custom_bot else ""

        if not bot_token or not chat_id:
            try:
                async with async_session() as db:
                    result = await db.execute(select(AlertSettings).limit(1))
                    alert_settings = result.scalar_one_or_none()
                if alert_settings and alert_settings.telegram_bot_token and alert_settings.telegram_chat_id:
                    bot_token = alert_settings.telegram_bot_token
                    chat_id = alert_settings.telegram_chat_id
            except Exception:
                pass

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

    async def test_single_node_by_id(
        self, server_id: int, test_mode: Optional[str] = None, method: Optional[str] = None,
    ) -> Optional[dict]:
        """Manual test trigger — returns result directly."""
        async with async_session() as db:
            result = await db.execute(
                select(Server).where(Server.id == server_id, Server.is_active == True)
            )
            server = result.scalar_one_or_none()

        if not server:
            return None

        effective_mode = test_mode if test_mode in ("quick", "full") else self._test_mode
        effective_method = method if method in ("iperf3", "ookla", "auto") else self._method

        if effective_method == "auto":
            effective_method = self._resolve_method_for_node(server)

        payload: dict = {
            "duration": self._duration,
            "streams": self._streams,
            "threshold_mbps": self._threshold,
            "test_mode": effective_mode,
            "method": effective_method,
        }

        if effective_method == "iperf3":
            server_list = await self._build_server_list_for_node(server)
            if not server_list:
                return {"error": "No iperf3 servers configured"}
            payload["servers"] = server_list

        if effective_method == "ookla":
            timeout = 150
        else:
            server_list = payload.get("servers", [])
            timeout = self._duration * max(len(server_list), 1) + 60

        try:
            async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
                response = await client.post(
                    f"{server.url}/api/speedtest",
                    headers={"X-API-Key": server.api_key},
                    json=payload,
                )

                if response.status_code == 200:
                    test_result = response.json()

                    async with async_session() as db:
                        await db.execute(
                            update(Server).where(Server.id == server.id).values(
                                last_speedtest=json.dumps(test_result)
                            )
                        )
                        await db.commit()

                    return test_result
                elif response.status_code == 409:
                    return {"error": "Test already in progress on this node"}
                else:
                    return {"error": f"Node returned HTTP {response.status_code}"}
        except httpx.TimeoutException:
            return {"error": "Connection to node timed out"}
        except Exception as e:
            return {"error": str(e)[:200]}


_scheduler: Optional[SpeedtestScheduler] = None


def get_speedtest_scheduler() -> SpeedtestScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = SpeedtestScheduler()
    return _scheduler


async def start_speedtest_scheduler():
    scheduler = get_speedtest_scheduler()
    await scheduler.start()


async def stop_speedtest_scheduler():
    scheduler = get_speedtest_scheduler()
    await scheduler.stop()
