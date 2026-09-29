"""Обновление HAProxy на нодах до новейшей официальной LTS-сборки.

Какая ветка собрана под релиз ноды, панель узнаёт по репозиториям сборщика
пакетов Debian/Ubuntu (PPA vbernat, haproxy.debian.net) — тем же, из которых
ставит install.sh (HAPROXY_LTS_BRANCHES там, менять вместе). Само обновление —
install.sh с MON_INSTALL_HAPROXY=1 на хосте через агента ноды, фоновой задачей
с логом: переключение reload'ом без обрыва соединений и откат при ошибке.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import AsyncIterator, Optional

import httpx

from app.models import Server
from app.services.deploy_service import build_haproxy_upgrade_command
from app.services.http_client import get_external_client
from app.services.remnawave_node_install import HostInstallJobManager, run_install_on_node

logger = logging.getLogger(__name__)

LTS_BRANCHES = ("3.4", "3.2", "3.0")
BRANCH_CACHE_TTL_SECONDS = 6 * 3600
PROBE_RETRY_SECONDS = 600
# Страница «Обновления» не ждёт медленную сеть панели: проверка доезжает в фоне
PROBE_WAIT_SECONDS = 3
# Каждая задача держит стрим к агенту ноды до 10 минут — массовый запуск идёт очередью
UPGRADE_CONCURRENCY = 10

_BRANCH_RE = re.compile(r"^(?:\d+:)?(\d+)\.(\d+)")

# (os_id, codename) → (истекает, ветка)
_branch_cache: dict[tuple[str, str], tuple[float, Optional[str]]] = {}
_probes: dict[tuple[str, str], asyncio.Task] = {}
_upgrade_slots = asyncio.Semaphore(UPGRADE_CONCURRENCY)

_manager = HostInstallJobManager(start_message="Обновляю HAProxy на «{name}»…")


def get_haproxy_upgrade_manager() -> HostInstallJobManager:
    return _manager


def parse_branch(version: Optional[str]) -> Optional[tuple[int, int]]:
    """2.8.16-0ubuntu0.24.04.3 → (2, 8)."""
    match = _BRANCH_RE.match(version or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def release_url(os_id: str, codename: str, branch: str) -> Optional[str]:
    if os_id == "ubuntu":
        return f"https://ppa.launchpadcontent.net/vbernat/haproxy-{branch}/ubuntu/dists/{codename}/Release"
    if os_id == "debian":
        return f"https://haproxy.debian.net/dists/{codename}-backports-{branch}/Release"
    return None


async def _probe(os_id: str, codename: str) -> Optional[str]:
    key = (os_id, codename)
    for branch in LTS_BRANCHES:
        url = release_url(os_id, codename, branch)
        if url is None:
            break
        try:
            response = await get_external_client().head(url, follow_redirects=True)
        except httpx.HTTPError as exc:
            logger.warning("HAProxy repository probe %s failed: %s", url, exc)
            previous = _branch_cache.get(key)
            _branch_cache[key] = (time.monotonic() + PROBE_RETRY_SECONDS, previous[1] if previous else None)
            return _branch_cache[key][1]
        if response.status_code == 200:
            _branch_cache[key] = (time.monotonic() + BRANCH_CACHE_TTL_SECONDS, branch)
            return branch
    _branch_cache[key] = (time.monotonic() + BRANCH_CACHE_TTL_SECONDS, None)
    return None


async def newest_branch(os_id: str, codename: str) -> Optional[str]:
    """Новейшая LTS-ветка, собранная под этот релиз, или None."""
    key = (os_id, codename)
    cached = _branch_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    task = _probes.get(key)
    if task is None:
        task = asyncio.create_task(_probe(os_id, codename))
        _probes[key] = task
        task.add_done_callback(lambda _: _probes.pop(key, None))
    try:
        return await asyncio.wait_for(asyncio.shield(task), PROBE_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return cached[1] if cached else None


async def describe_haproxy(info: Optional[dict]) -> Optional[dict]:
    """HAProxy ноды для страницы «Обновления»: версия и ветка, до которой можно обновиться."""
    if not info:
        return None
    version = info.get("version")
    os_id, codename = info.get("os_id"), info.get("os_codename")
    target = await newest_branch(os_id, codename) if os_id and codename else None
    current = parse_branch(version)
    if target and current is not None and current >= parse_branch(target):
        target = None
    return {"version": version, "target_branch": target}


def running_job_id(server_id: int) -> Optional[str]:
    for job in _manager.list_jobs():
        if job["server_id"] == server_id and job["status"] == "running":
            return job["job_id"]
    return None


async def _queued(events: AsyncIterator[dict]) -> AsyncIterator[dict]:
    if _upgrade_slots.locked():
        yield {"type": "log", "line": f"[panel] В очереди: одновременно обновляется не больше {UPGRADE_CONCURRENCY} нод"}
    async with _upgrade_slots:
        async for event in events:
            yield event


def start_upgrade(server: Server) -> str:
    return _manager.start(server, _queued(run_install_on_node(server, build_haproxy_upgrade_command())))
