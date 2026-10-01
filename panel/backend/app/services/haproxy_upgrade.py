"""Обновление HAProxy на нодах до новейшей официальной LTS-сборки.

Что доступно релизу ноды, панель читает из индекса пакетов репозиториев
сборщика пакетов Debian/Ubuntu (PPA vbernat, haproxy.debian.net) — тех же, из
которых ставит install.sh (HAPROXY_LTS_BRANCHES там, менять вместе): новейшую
ветку, собранную под релиз, и последнюю версию в ней. Так значок на карточке
появляется и на переход между ветками, и на исправления внутри ветки. Само
обновление — install.sh с MON_INSTALL_HAPROXY=1 на хосте через агента ноды,
фоновой задачей с логом: переключение reload'ом без обрыва соединений и откат
при ошибке.
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import re
import time
import zlib
from typing import AsyncIterator, Optional

import httpx

from app.models import Server
from app.services.deploy_service import build_haproxy_upgrade_command
from app.services.http_client import get_external_client
from app.services.remnawave_node_install import HostInstallJobManager, run_install_on_node

logger = logging.getLogger(__name__)

LTS_BRANCHES = ("3.4", "3.2", "3.0")
# Сборщик выкладывает исправления раз в несколько недель — часа хватает, чтобы значок не запаздывал
RELEASE_CACHE_TTL_SECONDS = 3600
PROBE_RETRY_SECONDS = 600
# Страница «Обновления» не ждёт медленную сеть панели: проверка доезжает в фоне
PROBE_WAIT_SECONDS = 3
# Ноды старее этой доработки архитектуру не сообщают
DEFAULT_ARCH = "amd64"
# Каждая задача держит стрим к агенту ноды до 10 минут — массовый запуск идёт очередью
UPGRADE_CONCURRENCY = 10

_VERSION_RE = re.compile(r"^(?:\d+:)?(\d+(?:\.\d+)*)")

ReleaseKey = tuple[str, str, str]

# (os_id, codename, arch) → (истекает, новейшая версия)
_release_cache: dict[ReleaseKey, tuple[float, Optional[str]]] = {}
_probes: dict[ReleaseKey, asyncio.Task] = {}
_upgrade_slots = asyncio.Semaphore(UPGRADE_CONCURRENCY)

_manager = HostInstallJobManager(start_message="Обновляю HAProxy на «{name}»…")


class RepositoryProbeError(Exception):
    def __init__(self, url: str, reason: str):
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason


def get_haproxy_upgrade_manager() -> HostInstallJobManager:
    return _manager


def parse_version(version: Optional[str]) -> Optional[tuple[int, ...]]:
    """2.8.16-0ubuntu0.24.04.3 → (2, 8, 16): ревизия пакета в сравнении не участвует."""
    match = _VERSION_RE.match(version or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def upstream_version(version: str) -> str:
    """1:3.4.6-1ppa1~resolute → 3.4.6."""
    return version.split(":", 1)[-1].split("-", 1)[0]


def packages_url(os_id: str, codename: str, branch: str, arch: str) -> Optional[str]:
    if os_id == "ubuntu":
        return f"https://ppa.launchpadcontent.net/vbernat/haproxy-{branch}/ubuntu/dists/{codename}/main/binary-{arch}/Packages.gz"
    if os_id == "debian":
        return f"https://haproxy.debian.net/dists/{codename}-backports-{branch}/main/binary-{arch}/Packages.gz"
    return None


def newest_haproxy_version(index: str) -> Optional[str]:
    """Новейший haproxy в индексе Packages — в репозитории Debian их там несколько."""
    best: Optional[str] = None
    for stanza in index.split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in stanza.splitlines() if ": " in line and not line.startswith(" ")
        )
        version = fields.get("Version")
        if fields.get("Package") != "haproxy" or parse_version(version) is None:
            continue
        if best is None or parse_version(version) > parse_version(best):
            best = version
    return best


async def _fetch_index(url: str) -> Optional[str]:
    """Текст индекса Packages; None — такой ветки под этот релиз нет."""
    try:
        response = await get_external_client().get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise RepositoryProbeError(url, str(exc)) from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise RepositoryProbeError(url, f"HTTP {response.status_code}")
    try:
        return gzip.decompress(response.content).decode("utf-8", errors="replace")
    except (OSError, EOFError, zlib.error) as exc:
        raise RepositoryProbeError(url, f"bad gzip: {exc}") from exc


async def _probe(key: ReleaseKey) -> Optional[str]:
    os_id, codename, arch = key
    try:
        for branch in LTS_BRANCHES:
            url = packages_url(os_id, codename, branch, arch)
            if url is None:
                break
            index = await _fetch_index(url)
            version = newest_haproxy_version(index) if index else None
            if version:
                _release_cache[key] = (time.monotonic() + RELEASE_CACHE_TTL_SECONDS, upstream_version(version))
                return _release_cache[key][1]
    except RepositoryProbeError as exc:
        logger.warning("HAProxy repository probe failed: %s", exc)
        previous = _release_cache.get(key)
        _release_cache[key] = (time.monotonic() + PROBE_RETRY_SECONDS, previous[1] if previous else None)
        return _release_cache[key][1]
    _release_cache[key] = (time.monotonic() + RELEASE_CACHE_TTL_SECONDS, None)
    return None


async def newest_version(os_id: str, codename: str, arch: str) -> Optional[str]:
    """Последняя версия новейшей LTS-ветки, собранной под этот релиз, или None."""
    key = (os_id, codename, arch)
    cached = _release_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    task = _probes.get(key)
    if task is None:
        task = asyncio.create_task(_probe(key))
        _probes[key] = task
        task.add_done_callback(lambda _: _probes.pop(key, None))
    try:
        return await asyncio.wait_for(asyncio.shield(task), PROBE_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return cached[1] if cached else None


async def describe_haproxy(info: Optional[dict]) -> Optional[dict]:
    """HAProxy ноды для страницы «Обновления»: версия и версия, до которой можно обновиться."""
    if not info:
        return None
    version = info.get("version")
    os_id, codename = info.get("os_id"), info.get("os_codename")
    target = await newest_version(os_id, codename, info.get("arch") or DEFAULT_ARCH) if os_id and codename else None
    current = parse_version(version)
    if target and current is not None and current >= parse_version(target):
        target = None
    return {"version": version, "target_version": target}


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
