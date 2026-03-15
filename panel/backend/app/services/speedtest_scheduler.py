"""Background speedtest scheduler.

Tests nodes sequentially (one at a time) to avoid overloading iperf3 servers.
Supports public iperf3 servers and panel-as-server mode.
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
    {"host": "ping.online.net", "port": 5200, "label": "Online.net (100G)", "region": "EU-FR"},
    {"host": "speedtest.serverius.net", "port": 5002, "label": "Serverius (10G)", "region": "EU-NL"},
    {"host": "iperf3.moji.fr", "port": 5200, "label": "Moji (100G)", "region": "EU-FR"},
    {"host": "paris.bbr.iperf.bytel.fr", "port": 9200, "label": "Bouygues (10G)", "region": "EU-FR"},
    {"host": "spd-rudp.hostkey.ru", "port": 5201, "label": "Hostkey Moscow", "region": "RU-MOW"},
    {"host": "st.spb.ertelecom.ru", "port": 5201, "label": "Ertelecom SPb", "region": "RU-SPB"},
    {"host": "st.ekat.ertelecom.ru", "port": 5201, "label": "Ertelecom Yekaterinburg", "region": "RU-SVE"},
    {"host": "speedtest.uztelecom.uz", "port": 5200, "label": "Uztelecom (10G)", "region": "Asia-UZ"},
]

SETTINGS_KEYS = {
    "speedtest_enabled": "true",
    "speedtest_mode": "both",
    "speedtest_servers": json.dumps(DEFAULT_IPERF_SERVERS),
    "speedtest_threshold": "500",
    "speedtest_interval": "60",
    "speedtest_duration": "2",
    "speedtest_streams": "1",
    "speedtest_test_mode": "light",
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
    PAUSE_BETWEEN_NODES = 10
    TG_MSG_LIMIT = 4096

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._settings_task: Optional[asyncio.Task] = None
        self._iperf_server_proc: Optional[asyncio.subprocess.Process] = None

        self._enabled = True
        self._mode = "both"
        self._servers = list(DEFAULT_IPERF_SERVERS)
        self._threshold = 500.0
        self._interval = 60
        self._duration = 2
        self._streams = 1
        self._test_mode = "light"
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
            self._mode = _get("speedtest_mode")
            self._threshold = float(_get("speedtest_threshold"))
            self._interval = max(1, int(_get("speedtest_interval")))
            self._duration = max(1, min(30, int(_get("speedtest_duration"))))
            self._streams = max(1, min(16, int(_get("speedtest_streams"))))
            self._test_mode = _get("speedtest_test_mode") if _get("speedtest_test_mode") in ("light", "full") else "light"
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
        should_run = self._mode in ("panel", "both") and self._enabled
        iperf3_bin = shutil.which("iperf3") or "/usr/bin/iperf3"

        if should_run and self._iperf_server_proc is None:
            try:
                self._iperf_server_proc = await asyncio.create_subprocess_exec(
                    iperf3_bin, "-s", "-p", str(self._panel_port), "--one-off",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                logger.info(f"iperf3 server started on port {self._panel_port}")
            except Exception as e:
                logger.warning(f"Failed to start iperf3 server: {e}")
                self._iperf_server_proc = None

        if should_run and self._iperf_server_proc is not None:
            if self._iperf_server_proc.returncode is not None:
                try:
                    self._iperf_server_proc = await asyncio.create_subprocess_exec(
                        iperf3_bin, "-s", "-p", str(self._panel_port), "--one-off",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                except Exception:
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

    async def start(self):
        if self._running:
            return
        await self._load_settings()
        self._running = True
        self._task = asyncio.create_task(self._main_loop())
        self._settings_task = asyncio.create_task(self._settings_loop())
        await self._manage_iperf_server()
        logger.info(f"Speedtest scheduler started (interval: {self._interval}min, enabled: {self._enabled})")

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
        await asyncio.sleep(30)  # initial delay
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

        server_list = self._build_server_list()
        if not server_list:
            logger.warning("Speedtest: no iperf3 servers configured")
            return

        logger.info(f"Speedtest: starting cycle for {len(servers)} nodes, {len(server_list)} iperf3 servers")

        events: list[dict] = []
        for srv in servers:
            if not self._running:
                break
            try:
                event = await self._test_single_node(srv, server_list)
                if event:
                    events.append(event)
            except Exception as e:
                logger.debug(f"Speedtest failed for {srv.name}: {e}")

            if self._running:
                await asyncio.sleep(self.PAUSE_BETWEEN_NODES)

        logger.info("Speedtest: cycle completed")

        if events:
            await self._send_notifications(events)

    async def _test_single_node(self, server: Server, server_list: list[dict]) -> Optional[dict]:
        """Send speedtest request to a single node and store result. Returns notification event or None."""
        bw_limit = f"{int(self._threshold * 2)}M" if self._test_mode == "light" else ""
        payload = {
            "servers": server_list,
            "duration": self._duration,
            "streams": self._streams,
            "threshold_mbps": self._threshold,
            "bandwidth_limit": bw_limit,
            "test_mode": self._test_mode,
        }

        try:
            async with httpx.AsyncClient(verify=False, timeout=self._duration * len(server_list) + 30) as client:
                response = await client.post(
                    f"{server.url}/api/speedtest",
                    headers={"X-API-Key": server.api_key},
                    json=payload,
                )

                if response.status_code == 200:
                    result = response.json()
                    speed = result.get("best_speed_mbps", 0)
                    logger.info(f"Speedtest {server.name}: {speed:.1f} Mbit/s")
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
            lines = ["🔴 <b>Низкая скорость</b>"]
            for e in slow:
                lines.append(f"  • {e['name']} — {e['speed']:.1f} Mbit/s (порог {e['threshold']:.0f})")
            sections.append("\n".join(lines))

        if errors:
            lines = ["⚠️ <b>Ошибки тестирования</b>"]
            for e in errors:
                lines.append(f"  • {e['name']} — {e['detail']}")
            sections.append("\n".join(lines))

        if recovered:
            lines = ["✅ <b>Скорость восстановлена</b>"]
            for e in recovered:
                lines.append(f"  • {e['name']} — {e['speed']:.1f} Mbit/s")
            sections.append("\n".join(lines))

        if not sections:
            return

        header = "🚀 <b>Speed Test</b>\n"
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

    async def test_single_node_by_id(self, server_id: int) -> Optional[dict]:
        """Manual test trigger — returns result directly."""
        async with async_session() as db:
            result = await db.execute(
                select(Server).where(Server.id == server_id, Server.is_active == True)
            )
            server = result.scalar_one_or_none()

        if not server:
            return None

        server_list = self._build_server_list()
        if not server_list:
            return {"error": "No iperf3 servers configured"}

        bw_limit = f"{int(self._threshold * 2)}M" if self._test_mode == "light" else ""
        payload = {
            "servers": server_list,
            "duration": self._duration,
            "streams": self._streams,
            "threshold_mbps": self._threshold,
            "bandwidth_limit": bw_limit,
            "test_mode": self._test_mode,
        }

        try:
            timeout = self._duration * len(server_list) + 30
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
