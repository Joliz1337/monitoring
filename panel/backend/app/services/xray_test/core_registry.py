"""Список доступных версий ядер и выбор используемой.

Xray и sing-box выходят чаще панели, а пре-релизы часто и нужны — там сначала
появляются новые транспорты. Поэтому версия не прибита к коду: панель тянет
список релизов с GitHub, оператор выбирает нужную, по умолчанию берётся самая
свежая, включая пре-релиз.

Целостность. Бинарник ядра панель запускает у себя, поэтому подмена — это
выполнение чужого кода. Закреплённые в коде версии сверяются по известному
SHA-256 и могут качаться через зеркало. Для остальных хэш берётся из файла
`.dgst` рядом с релизом (у Xray он есть), а если такого файла нет (sing-box) —
скачивание идёт только напрямую с github.com, где гарантию даёт TLS. Через
недоверенное зеркало незакреплённая версия не загружается.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from app.services.http_client import get_external_client
from app.services.xray_test.errors import CoreDownloadError
from app.services.xray_test.models import Core

logger = logging.getLogger(__name__)

LATEST = "latest"
RELEASES_TTL_SECONDS = 1800
RELEASES_LIMIT = 30
API_TIMEOUT = 20.0

GITHUB_API = {
    Core.XRAY: "https://api.github.com/repos/XTLS/Xray-core/releases",
    Core.SINGBOX: "https://api.github.com/repos/SagerNet/sing-box/releases",
}

# Имена ассетов внутри релиза по архитектуре. sing-box подставляет версию,
# поэтому шаблон, а не константа.
ASSET_NAMES = {
    Core.XRAY: {"amd64": "Xray-linux-64.zip", "arm64": "Xray-linux-arm64-v8a.zip"},
    Core.SINGBOX: {
        "amd64": "sing-box-{version}-linux-amd64.tar.gz",
        "arm64": "sing-box-{version}-linux-arm64.tar.gz",
    },
}

# Файл внутри архива, который и есть бинарник
ASSET_MEMBERS = {
    Core.XRAY: "xray",
    Core.SINGBOX: "sing-box-{version}-linux-{arch}/sing-box",
}

_DGST_RE = re.compile(r"SHA2-256=\s*([0-9a-f]{64})", re.IGNORECASE)

_cache: dict[Core, tuple[float, list["ReleaseInfo"]]] = {}
_cache_lock = asyncio.Lock()


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    prerelease: bool
    published_at: str
    asset_url: Optional[str]
    asset_name: Optional[str]
    asset_size: Optional[int]
    digest_url: Optional[str]

    @property
    def available(self) -> bool:
        return bool(self.asset_url)


def asset_member(core: Core, version: str, arch: str) -> str:
    return ASSET_MEMBERS[core].format(version=version, arch=arch)


async def list_releases(core: Core, *, refresh: bool = False) -> list[ReleaseInfo]:
    """Версии ядра с GitHub. Кэш на полчаса — список меняется редко."""
    async with _cache_lock:
        cached = _cache.get(core)
        if cached and not refresh and time.time() - cached[0] < RELEASES_TTL_SECONDS:
            return cached[1]

        try:
            releases = await _fetch_releases(core)
        except (httpx.HTTPError, ValueError) as exc:
            if cached:
                logger.warning("xray-test: releases refresh failed, using cache: %s", exc)
                return cached[1]
            raise CoreDownloadError(f"Не получить список версий {core.value}: {exc}") from exc

        _cache[core] = (time.time(), releases)
        return releases


async def _fetch_releases(core: Core) -> list[ReleaseInfo]:
    from app.services.xray_test.core_manager import detect_arch

    arch = detect_arch()
    wanted_template = ASSET_NAMES[core][arch]

    response = await get_external_client().get(
        GITHUB_API[core],
        params={"per_page": RELEASES_LIMIT},
        headers={"Accept": "application/vnd.github+json"},
        timeout=API_TIMEOUT,
    )
    response.raise_for_status()

    releases: list[ReleaseInfo] = []
    for item in response.json():
        if item.get("draft"):
            continue
        tag = str(item.get("tag_name") or "")
        version = tag.lstrip("v")
        if not version:
            continue

        wanted = wanted_template.format(version=version)
        asset = next(
            (a for a in item.get("assets", []) if a.get("name") == wanted), None
        )
        digest = next(
            (a for a in item.get("assets", []) if a.get("name") == f"{wanted}.dgst"), None
        )
        releases.append(ReleaseInfo(
            version=version,
            tag=tag,
            prerelease=bool(item.get("prerelease")),
            published_at=str(item.get("published_at") or ""),
            asset_url=asset.get("browser_download_url") if asset else None,
            asset_name=asset.get("name") if asset else None,
            asset_size=asset.get("size") if asset else None,
            digest_url=digest.get("browser_download_url") if digest else None,
        ))
    return releases


async def resolve_version(core: Core, selected: str) -> ReleaseInfo:
    """Выбранная настройка → конкретный релиз.

    `latest` — самый свежий из опубликованных, пре-релизы включительно: именно в
    них появляются новые транспорты, ради которых версию и переключают.
    """
    releases = [item for item in await list_releases(core) if item.available]
    if not releases:
        raise CoreDownloadError(f"У {core.value} нет сборок под эту архитектуру")

    if selected == LATEST:
        return releases[0]

    wanted = selected.lstrip("v")
    for item in releases:
        if item.version == wanted:
            return item
    raise CoreDownloadError(f"Версия {core.value} {selected} не найдена среди последних релизов")


async def fetch_digest(release: ReleaseInfo) -> Optional[str]:
    """SHA-256 из файла .dgst рядом с релизом, если он опубликован."""
    if not release.digest_url:
        return None
    try:
        response = await get_external_client().get(release.digest_url, timeout=API_TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("xray-test: digest fetch failed for %s: %s", release.tag, exc)
        return None

    match = _DGST_RE.search(response.text)
    return match.group(1).lower() if match else None
