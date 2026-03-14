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

import httpx
from sqlalchemy import select, update

from app.database import async_session
from app.models import Server, PanelSettings

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
    "speedtest_duration": "3",
    "speedtest_streams": "4",
    "speedtest_panel_port": "5201",
    "speedtest_panel_address": "",
}


class SpeedtestScheduler:
    PAUSE_BETWEEN_NODES = 10  # seconds between testing different nodes

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._settings_task: Optional[asyncio.Task] = None
        self._iperf_server_proc: Optional[asyncio.subprocess.Process] = None

        self._enabled = True
        self._mode = "both"
        self._servers = list(DEFAULT_IPERF_SERVERS)
        self._threshold = 500.0
        self._interval = 60  # minutes
        self._duration = 3
        self._streams = 4
        self._panel_port = 5201
        self._panel_address = ""

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
            self._panel_port = int(_get("speedtest_panel_port"))
            self._panel_address = _get("speedtest_panel_address")

            try:
                self._servers = json.loads(_get("speedtest_servers"))
                if not isinstance(self._servers, list):
                    self._servers = list(DEFAULT_IPERF_SERVERS)
            except (json.JSONDecodeError, TypeError):
                self._servers = list(DEFAULT_IPERF_SERVERS)

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

        if self._mode in ("panel", "both") and self._panel_address:
            servers.append({
                "host": self._panel_address,
                "port": self._panel_port,
                "label": "Panel",
                "region": "panel",
            })

        if self._mode in ("public", "both"):
            servers.extend(self._servers)

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

        server_list = self._build_server_list()
        if not server_list:
            logger.warning("Speedtest: no iperf3 servers configured")
            return

        logger.info(f"Speedtest: starting cycle for {len(servers)} nodes, {len(server_list)} iperf3 servers")

        for srv in servers:
            if not self._running:
                break
            try:
                await self._test_single_node(srv, server_list)
            except Exception as e:
                logger.debug(f"Speedtest failed for {srv.name}: {e}")

            if self._running:
                await asyncio.sleep(self.PAUSE_BETWEEN_NODES)

        logger.info("Speedtest: cycle completed")

    async def _test_single_node(self, server: Server, server_list: list[dict]):
        """Send speedtest request to a single node and store result."""
        payload = {
            "servers": server_list,
            "duration": self._duration,
            "streams": self._streams,
            "threshold_mbps": self._threshold,
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
                    return
                else:
                    logger.warning(f"Speedtest {server.name}: HTTP {response.status_code}")
                    return
        except httpx.TimeoutException:
            logger.warning(f"Speedtest {server.name}: timeout")
            return
        except Exception as e:
            logger.debug(f"Speedtest {server.name}: {e}")
            return

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

        payload = {
            "servers": server_list,
            "duration": self._duration,
            "streams": self._streams,
            "threshold_mbps": self._threshold,
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
