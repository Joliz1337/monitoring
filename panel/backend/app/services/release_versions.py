"""Последние версии панели, ноды и оптимизаций в выбранном канале обновлений на GitHub.

Один кэш на панель: страница «Обновления» опрашивает его каждые 12 секунд,
значок в меню — из каждой открытой вкладки, а на GitHub уходит не больше
трёх запросов за время жизни кэша.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from app.services import update_channel
from app.services.http_client import get_external_client
from app.services.reserved_ports_sync import _version_tuple

logger = logging.getLogger(__name__)

# raw.githubusercontent.com сам отдаёт файлы из CDN с max-age=300 — чаще спрашивать бессмысленно
LATEST_VERSIONS_TTL_SEC = 300.0
# Сбой GitHub не должен на 5 минут прятать обновления — повторяем скоро
LATEST_VERSIONS_FAILED_TTL_SEC = 30.0
GITHUB_TIMEOUT_SEC = 10.0


@dataclass(frozen=True)
class LatestVersions:
    panel: Optional[str]
    node: Optional[str]
    optimizations: Optional[str]

    @property
    def complete(self) -> bool:
        return all((self.panel, self.node, self.optimizations))


_cached: Optional[tuple[str, float, LatestVersions]] = None
_lock = asyncio.Lock()


def is_newer(candidate: Optional[str], current: Optional[str]) -> bool:
    """candidate строго новее current. Неизвестная версия с любой стороны — не новее:
    после переключения с dev на стабильный канал более старая версия в main
    не должна выглядеть обновлением."""
    if not candidate or not current:
        return False
    return _version_tuple(candidate) > _version_tuple(current)


async def _fetch_version_file(url: str) -> Optional[str]:
    try:
        response = await get_external_client().get(url, timeout=GITHUB_TIMEOUT_SEC)
    except httpx.HTTPError as e:
        logger.error("github_version_fetch_failed url=%s error=%r", url, e)
        return None
    if response.status_code != 200:
        logger.warning("github_version_fetch_failed url=%s status=%s", url, response.status_code)
        return None
    return response.text.strip() or None


async def _fetch_latest_versions() -> LatestVersions:
    raw_base = update_channel.github_raw_base()
    panel, node, optimizations = await asyncio.gather(
        _fetch_version_file(f"{raw_base}/panel/VERSION"),
        _fetch_version_file(f"{raw_base}/node/VERSION"),
        _fetch_version_file(f"{update_channel.github_configs_base()}/VERSION"),
    )
    return LatestVersions(panel=panel, node=node, optimizations=optimizations)


def _fresh_cached(branch: str) -> Optional[LatestVersions]:
    if _cached is None:
        return None
    cached_branch, fetched_at, versions = _cached
    ttl = LATEST_VERSIONS_TTL_SEC if versions.complete else LATEST_VERSIONS_FAILED_TTL_SEC
    if cached_branch != branch or time.monotonic() - fetched_at > ttl:
        return None
    return versions


async def latest_versions() -> LatestVersions:
    """Версии из канала обновлений; кэш привязан к ветке — смена канала его сбрасывает."""
    global _cached
    branch = update_channel.current_branch()
    versions = _fresh_cached(branch)
    if versions:
        return versions
    async with _lock:
        versions = _fresh_cached(branch)
        if versions:
            return versions
        versions = await _fetch_latest_versions()
        _cached = (branch, time.monotonic(), versions)
        return versions
