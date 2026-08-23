"""Бинарники прокси-ядер: выбор версии, загрузка, проверка целостности.

Ядра не вшиты в образ панели: они весят под 75 МБ на обе архитектуры, а версия
не должна быть прибита к релизу панели — Xray и sing-box выходят чаще. Каждая
версия лежит в volume отдельно, поэтому переключение между ними не требует
перекачивания.

Выбранная версия кэшируется в памяти (как ветка канала обновлений): она нужна
фоновым проверкам, где сессии БД под рукой нет.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import stat
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Optional

from app.services.http_client import get_external_client
from app.services.xray_test import core_registry
from app.services.xray_test.core_registry import LATEST, ReleaseInfo
from app.services.xray_test.errors import CoreDownloadError, UnsupportedConfigError
from app.services.xray_test.models import Core, Protocol, ProxyEndpoint, Transport

logger = logging.getLogger(__name__)

CORES_DIR = Path("/app/data/xray-test/cores")
GITHUB_MIRROR = "https://ghfast.top/"
DOWNLOAD_TIMEOUT = 180.0
MAX_ARCHIVE_BYTES = 120 * 1024 * 1024

BINARY_NAMES = {Core.XRAY: "xray", Core.SINGBOX: "sing-box"}

# Границы проверены на живых бинарниках xray 26.3.27 и sing-box 1.13.19.
SINGBOX_ONLY_PROTOCOLS = frozenset({
    Protocol.HYSTERIA2, Protocol.TUIC, Protocol.ANYTLS, Protocol.SHADOWTLS,
})
# HTTP/2 как отдельный транспорт из xray убран (мигрировал в XHTTP stream-one),
# у sing-box он остался — поэтому такие ссылки уходят к нему.
SINGBOX_ONLY_TRANSPORTS = frozenset({Transport.H2})
# xhttp sing-box не знает вовсе, mKCP у него нет как транспорта.
XRAY_ONLY_TRANSPORTS = frozenset({Transport.XHTTP, Transport.MKCP})


@dataclass(frozen=True)
class PinnedRelease:
    """Версия с известным хэшем: её можно тянуть через зеркало."""

    version: str
    digests: dict[str, str]


# Проверены вручную; служат и фолбэком, когда GitHub API недоступен
PINNED_RELEASES: dict[Core, PinnedRelease] = {
    Core.XRAY: PinnedRelease(
        version="26.3.27",
        digests={
            "amd64": "23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae",
            "arm64": "4d30283ae614e3057f730f67cd088a42be6fdf91f8639d82cb69e48cde80413c",
        },
    ),
    Core.SINGBOX: PinnedRelease(
        version="1.13.19",
        digests={
            "amd64": "ef88a9e577d474210867bd708933d042e9b70106529df2656182c9db90106aa1",
            "arm64": "7fe3597a95a3c5ad67477b1d7653b9ce097e0be7c676758eba1fcf558f353d57",
        },
    ),
}

SETTING_KEYS = {
    Core.XRAY: "xray_test_version_xray",
    Core.SINGBOX: "xray_test_version_singbox",
}

_selected: dict[Core, str] = {core: LATEST for core in Core}
_locks: dict[Core, asyncio.Lock] = {core: asyncio.Lock() for core in Core}


def detect_arch() -> str:
    machine = platform.machine().lower()
    return "arm64" if machine in ("aarch64", "arm64") else "amd64"


def select_core(endpoint: ProxyEndpoint) -> Core:
    """Ядро, способное поднять эту конфигурацию. Xray в приоритете.

    Отключённая проверка сертификата тоже уводит конфиг к sing-box: xray 26
    удалил allowInsecure, а молча проверять сертификат там, где пользователь
    просил не проверять, — значит показать «не работает» на рабочем ключе.
    """
    transport = endpoint.transport.kind
    needs_xray = transport in XRAY_ONLY_TRANSPORTS
    needs_singbox = (
        endpoint.protocol in SINGBOX_ONLY_PROTOCOLS
        or transport in SINGBOX_ONLY_TRANSPORTS
        or endpoint.tls.allow_insecure
    )

    if transport is Transport.MKCP and (endpoint.transport.seed or endpoint.transport.header_type):
        raise UnsupportedConfigError(
            "Обфускация mKCP (seed/headerType) удалена из Xray и не поддерживается ни одним ядром"
        )

    if needs_singbox and needs_xray:
        raise UnsupportedConfigError(
            f"Транспорт {transport.value} умеет только Xray, "
            f"а остальные параметры конфигурации — только sing-box"
        )
    return Core.SINGBOX if needs_singbox else Core.XRAY


def selected_version(core: Core) -> str:
    return _selected.get(core, LATEST)


def set_selected_version(core: Core, version: str) -> None:
    _selected[core] = version or LATEST


def load_selected(values: dict[str, str]) -> None:
    """Восстановить выбор из настроек панели при старте."""
    for core, key in SETTING_KEYS.items():
        value = (values.get(key) or "").strip()
        if value:
            _selected[core] = value


def binary_path(core: Core, version: str, arch: Optional[str] = None) -> Path:
    return CORES_DIR / core.value / version / (arch or detect_arch()) / BINARY_NAMES[core]


def is_installed(core: Core, version: str) -> bool:
    path = binary_path(core, version)
    return path.is_file() and os.access(path, os.X_OK)


def installed_versions(core: Core) -> list[str]:
    root = CORES_DIR / core.value
    if not root.is_dir():
        return []
    return sorted(
        (item.name for item in root.iterdir() if item.is_dir() and is_installed(core, item.name)),
        reverse=True,
    )


def remove_version(core: Core, version: str) -> None:
    target = CORES_DIR / core.value / version
    if not target.is_dir():
        raise CoreDownloadError(f"Версия {core.value} {version} не установлена")
    shutil.rmtree(target, ignore_errors=True)


async def ensure_core(core: Core, version: Optional[str] = None) -> Path:
    """Путь к бинарнику ядра, при необходимости скачав его.

    Блокировка на ядро: параллельные проверки не должны качать один архив
    несколько раз и писать в один файл.
    """
    async with _locks[core]:
        release = await _resolve(core, version)
        path = binary_path(core, release.version)
        if is_installed(core, release.version):
            return path

        payload, digest = await _download(core, release)
        await asyncio.to_thread(_install_sync, core, release, payload)
        logger.info(
            "xray-test: installed %s %s (%s, sha256 %s)",
            core.value, release.version, detect_arch(), (digest or "не сверялся")[:12],
        )
        return path


async def resolve_release(core: Core, version: Optional[str] = None) -> ReleaseInfo:
    return await _resolve(core, version)


async def _resolve(core: Core, version: Optional[str]) -> ReleaseInfo:
    wanted = version or selected_version(core)
    try:
        return await core_registry.resolve_version(core, wanted)
    except CoreDownloadError:
        # GitHub недоступен: закреплённая версия остаётся рабочим вариантом
        pinned = PINNED_RELEASES[core]
        if wanted in (LATEST, pinned.version) and is_installed(core, pinned.version):
            return _pinned_release(core)
        if wanted in (LATEST, pinned.version):
            logger.warning("xray-test: releases unavailable, falling back to pinned %s", pinned.version)
            return _pinned_release(core)
        raise


def _pinned_release(core: Core) -> ReleaseInfo:
    pinned = PINNED_RELEASES[core]
    arch = detect_arch()
    name = core_registry.ASSET_NAMES[core][arch].format(version=pinned.version)
    base = {
        Core.XRAY: f"https://github.com/XTLS/Xray-core/releases/download/v{pinned.version}",
        Core.SINGBOX: f"https://github.com/SagerNet/sing-box/releases/download/v{pinned.version}",
    }[core]
    return ReleaseInfo(
        version=pinned.version,
        tag=f"v{pinned.version}",
        prerelease=False,
        published_at="",
        asset_url=f"{base}/{name}",
        asset_name=name,
        asset_size=None,
        digest_url=None,
    )


async def _expected_digest(core: Core, release: ReleaseInfo) -> Optional[str]:
    arch = detect_arch()
    pinned = PINNED_RELEASES[core]
    if release.version == pinned.version:
        return pinned.digests.get(arch)
    return await core_registry.fetch_digest(release)


async def _download(core: Core, release: ReleaseInfo) -> tuple[bytes, Optional[str]]:
    if not release.asset_url:
        raise CoreDownloadError(f"У {core.value} {release.version} нет сборки под {detect_arch()}")

    expected = await _expected_digest(core, release)
    # Зеркало допустимо только с известным хэшем: иначе подменённый бинарник
    # запустится у нас же, а это выполнение чужого кода
    urls = [release.asset_url]
    if expected:
        urls.append(f"{GITHUB_MIRROR}{release.asset_url}")

    errors: list[str] = []
    for url in urls:
        try:
            payload = await _fetch(url)
        except Exception as exc:  # noqa: BLE001 — фолбэк на зеркало по любой сетевой причине
            errors.append(f"{url.split('//')[1][:40]}…: {exc}")
            continue

        digest = sha256(payload).hexdigest()
        if expected and digest != expected:
            errors.append(f"контрольная сумма не совпала ({digest[:12]}…)")
            continue
        if release.asset_size and len(payload) != release.asset_size:
            errors.append(f"размер не совпал ({len(payload)} вместо {release.asset_size})")
            continue
        return payload, digest if expected else None

    hint = "" if expected else (
        " Для этой версии контрольная сумма не опубликована, поэтому зеркало не используется — "
        "нужен прямой доступ к github.com"
    )
    raise CoreDownloadError("; ".join(errors) + hint)


async def _fetch(url: str) -> bytes:
    client = get_external_client()
    async with client.stream("GET", url, timeout=DOWNLOAD_TIMEOUT) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise CoreDownloadError(f"Архив больше {MAX_ARCHIVE_BYTES // 1024 // 1024} МБ")
            chunks.append(chunk)
    return b"".join(chunks)


def _install_sync(core: Core, release: ReleaseInfo, payload: bytes) -> None:
    """Распаковка stdlib-средствами: unzip в образе панели нет."""
    arch = detect_arch()
    target = binary_path(core, release.version, arch)
    target.parent.mkdir(parents=True, exist_ok=True)
    member = core_registry.asset_member(core, release.version, arch)
    name = release.asset_name or ""

    with tempfile.TemporaryDirectory(dir=str(target.parent)) as tmp:
        tmp_dir = Path(tmp)
        archive = tmp_dir / (name or "core.bin")
        archive.write_bytes(payload)
        extracted = tmp_dir / BINARY_NAMES[core]

        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                with zf.open(member) as src, extracted.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        else:
            with tarfile.open(archive, "r:gz") as tf:
                source = tf.extractfile(member)
                if source is None:
                    raise CoreDownloadError(f"В архиве нет файла {member}")
                with extracted.open("wb") as dst:
                    shutil.copyfileobj(source, dst)

        extracted.chmod(extracted.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(extracted, target)
