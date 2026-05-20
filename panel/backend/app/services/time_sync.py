import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import docker
from docker.errors import DockerException, ImageNotFound

import httpx
from sqlalchemy import select

from app.database import async_session
from app.models import Server, PanelSettings
from app.services.http_client import get_node_client, node_auth_headers

logger = logging.getLogger(__name__)

SYNC_INTERVAL = 86400  # 24 hours
SYNC_CONTAINER_NAME = "panel-time-sync"
SYNC_CONTAINER_IMAGE = "docker:cli"


class TimeSyncService:

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._sync_in_progress = False
        self._last_sync: Optional[datetime] = None
        self._last_results: list[dict] = []

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Time sync service started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Time sync service stopped")

    def get_status(self) -> dict:
        next_sync_in: Optional[int] = None
        if self._running and self._last_sync:
            elapsed = (datetime.now(timezone.utc) - self._last_sync).total_seconds()
            next_sync_in = max(0, int(SYNC_INTERVAL - elapsed))

        return {
            "sync_in_progress": self._sync_in_progress,
            "last_sync": self._last_sync.isoformat() if self._last_sync else None,
            "next_sync_in": next_sync_in,
            "last_results": self._last_results,
        }

    # ------------------------------------------------------------------
    async def _loop(self):
        # Начальная задержка — дать другим сервисам стартовать
        await asyncio.sleep(30)

        while self._running:
            try:
                enabled = await self._get_setting("time_sync_enabled")
                if enabled == "true":
                    should_sync = (
                        self._last_sync is None
                        or (datetime.now(timezone.utc) - self._last_sync).total_seconds() >= SYNC_INTERVAL
                    )
                    if should_sync:
                        await self.sync_all_servers()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Time sync loop error: {e}")

            await asyncio.sleep(60)

    # ------------------------------------------------------------------
    async def sync_all_servers(self, tz: Optional[str] = None):
        if self._sync_in_progress:
            return
        self._sync_in_progress = True

        try:
            tz = tz or await self._get_setting("server_timezone") or "Europe/Moscow"

            async with async_session() as db:
                result = await db.execute(
                    select(Server).where(Server.is_active == True)  # noqa: E712
                )
                servers = list(result.scalars().all())

            # Синхронизация нод параллельно
            node_tasks = [self._sync_node(s, tz) for s in servers]
            panel_task = self._sync_panel_host(tz)
            all_results = await asyncio.gather(
                *node_tasks, panel_task,
                return_exceptions=True,
            )

            results: list[dict] = []
            for i, res in enumerate(all_results):
                if isinstance(res, Exception):
                    name = servers[i].name if i < len(servers) else "panel"
                    results.append({"name": name, "success": False, "error": str(res)})
                elif isinstance(res, dict):
                    results.append(res)

            self._last_sync = datetime.now(timezone.utc)
            self._last_results = results

            # Сохраняем результат в настройки
            await self._save_sync_status(results)

            ok = sum(1 for r in results if r.get("success"))
            total = len(results)
            logger.info(f"Time sync completed: {ok}/{total} servers, tz={tz}")

        except Exception as e:
            logger.error(f"Time sync failed: {e}")
        finally:
            self._sync_in_progress = False

    async def sync_single_server(self, server_id: int, tz: Optional[str] = None):
        try:
            tz = tz or await self._get_setting("server_timezone") or "Europe/Moscow"

            async with async_session() as db:
                result = await db.execute(
                    select(Server).where(Server.id == server_id, Server.is_active == True)  # noqa: E712
                )
                server = result.scalar_one_or_none()

            if not server:
                return

            res = await self._sync_node(server, tz)
            logger.info(f"Time sync for {server.name}: success={res.get('success')}")
        except Exception as e:
            logger.error(f"Time sync for server {server_id} failed: {e}")

    # ------------------------------------------------------------------
    async def _sync_node(self, server: Server, tz: str) -> dict:
        try:
            client = get_node_client(server)
            response = await client.post(
                f"{server.url}/api/system/time-sync",
                json={"timezone": tz},
                headers=node_auth_headers(server),
                timeout=30.0,
            )

            if response.status_code == 200:
                data = response.json()
                return {
                    "name": server.name,
                    "server_id": server.id,
                    "success": data.get("success", False),
                    "timezone": data.get("timezone", ""),
                    "ntp_synchronized": data.get("ntp_synchronized", False),
                }

            return {
                "name": server.name,
                "server_id": server.id,
                "success": False,
                "error": f"HTTP {response.status_code}",
            }
        except httpx.TimeoutException:
            return {"name": server.name, "server_id": server.id, "success": False, "error": "timeout"}
        except httpx.RequestError as e:
            return {"name": server.name, "server_id": server.id, "success": False, "error": str(e)}

    async def _sync_panel_host(self, tz: str) -> dict:
        """Синхронизация хоста панели через Docker контейнер с nsenter."""
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._run_panel_sync_container, tz)
            return result
        except Exception as e:
            logger.error(f"Panel host time sync failed: {e}")
            return {"name": "panel", "success": False, "error": str(e)}

    def _run_panel_sync_container(self, tz: str) -> dict:
        """Запуск Docker-контейнера для синхронизации времени на хосте панели."""
        try:
            client = docker.from_env()
        except DockerException as e:
            return {"name": "panel", "success": False, "error": f"Docker: {e}"}

        # Удалить старый контейнер
        try:
            old = client.containers.get(SYNC_CONTAINER_NAME)
            old.remove(force=True)
        except docker.errors.NotFound:
            pass

        try:
            client.images.get(SYNC_CONTAINER_IMAGE)
        except ImageNotFound:
            client.images.pull(SYNC_CONTAINER_IMAGE)

        script = f"""#!/bin/sh
set -e
command -v nsenter >/dev/null 2>&1 || apk add --no-cache util-linux-misc >/dev/null 2>&1 || apk add --no-cache util-linux >/dev/null 2>&1
nsenter -t 1 -m -u -n -i -p -- timedatectl set-timezone {tz}
nsenter -t 1 -m -u -n -i -p -- timedatectl set-ntp true
nsenter -t 1 -m -u -n -i -p -- systemctl restart systemd-timesyncd 2>/dev/null || true
sleep 2
nsenter -t 1 -m -u -n -i -p -- timedatectl show --no-pager
"""

        try:
            container = client.containers.run(
                image=SYNC_CONTAINER_IMAGE,
                command=["sh", "-c", script],
                name=SYNC_CONTAINER_NAME,
                privileged=True,
                pid_mode="host",
                network_mode="host",
                detach=True,
                remove=False,
            )

            result = container.wait(timeout=60)
            logs = container.logs().decode("utf-8", errors="replace")

            try:
                container.remove(force=True)
            except Exception:
                pass

            exit_code = result.get("StatusCode", -1)
            if exit_code != 0:
                return {"name": "panel", "success": False, "error": f"Exit code {exit_code}: {logs[-500:]}"}

            # Парсим вывод timedatectl show
            ntp_synced = False
            current_tz = tz
            for line in logs.strip().split("\n"):
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if key == "Timezone":
                    current_tz = value
                elif key == "NTPSynchronized":
                    ntp_synced = value == "yes"

            return {
                "name": "panel",
                "success": True,
                "timezone": current_tz,
                "ntp_synchronized": ntp_synced,
            }

        except Exception as e:
            # Cleanup
            try:
                c = client.containers.get(SYNC_CONTAINER_NAME)
                c.remove(force=True)
            except Exception:
                pass
            return {"name": "panel", "success": False, "error": str(e)}

    # ------------------------------------------------------------------
    async def _get_setting(self, key: str) -> Optional[str]:
        defaults = {
            "server_timezone": "Europe/Moscow",
            "time_sync_enabled": "true",
        }
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(PanelSettings).where(PanelSettings.key == key)
                )
                setting = result.scalar_one_or_none()
                return setting.value if setting else defaults.get(key)
        except Exception:
            return defaults.get(key)

    async def _save_sync_status(self, results: list[dict]):
        try:
            async with async_session() as db:
                # last_run
                now_iso = datetime.now(timezone.utc).isoformat()
                run_setting = (await db.execute(
                    select(PanelSettings).where(PanelSettings.key == "time_sync_last_run")
                )).scalar_one_or_none()
                if run_setting:
                    run_setting.value = now_iso
                else:
                    db.add(PanelSettings(key="time_sync_last_run", value=now_iso))

                # last_status
                safe_results = json.dumps(results, ensure_ascii=False, default=str)
                status_setting = (await db.execute(
                    select(PanelSettings).where(PanelSettings.key == "time_sync_last_status")
                )).scalar_one_or_none()
                if status_setting:
                    status_setting.value = safe_results
                else:
                    db.add(PanelSettings(key="time_sync_last_status", value=safe_results))

                await db.commit()
        except Exception as e:
            logger.error(f"Failed to save time sync status: {e}")


# Module-level singleton
_service: Optional[TimeSyncService] = None


def get_time_sync_service() -> TimeSyncService:
    global _service
    if _service is None:
        _service = TimeSyncService()
    return _service


async def start_time_sync():
    service = get_time_sync_service()
    await service.start()


async def stop_time_sync():
    service = get_time_sync_service()
    await service.stop()
