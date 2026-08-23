"""Бинарники прокси-ядер: выбор, загрузка, проверка целостности.

Ядра не вшиты в образ панели: они весят под 75 МБ на обе архитектуры, а версия
ядра не должна быть прибита к релизу панели — xray и sing-box выходят чаще.
Скачиваются один раз в volume panel-data и живут там между обновлениями.

Каждый архив сверяется по SHA-256: зеркало ghfast.top, которым панель пользуется
на заблокированных серверах, доверенным источником не является.
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
from app.services.xray_test.errors import CoreDownloadError, UnsupportedConfigError
from app.services.xray_test.models import Core, Protocol, ProxyEndpoint, Transport

logger = logging.getLogger(__name__)

CORES_DIR = Path("/app/data/xray-test/cores")
GITHUB_MIRROR = "https://ghfast.top/"
DOWNLOAD_TIMEOUT = 180.0
MAX_ARCHIVE_BYTES = 120 * 1024 * 1024

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
class CoreAsset:
    filename: str
    sha256: str
    member: str


@dataclass(frozen=True)
class CoreRelease:
    version: str
    base_url: str
    binary: str
    assets: dict[str, CoreAsset]


CORE_RELEASES: dict[Core, CoreRelease] = {
    Core.XRAY: CoreRelease(
        version="26.3.27",
        base_url="https://github.com/XTLS/Xray-core/releases/download/v26.3.27",
        binary="xray",
        assets={
            "amd64": CoreAsset(
                filename="Xray-linux-64.zip",
                sha256="23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae",
                member="xray",
            ),
            "arm64": CoreAsset(
                filename="Xray-linux-arm64-v8a.zip",
                sha256="4d30283ae614e3057f730f67cd088a42be6fdf91f8639d82cb69e48cde80413c",
                member="xray",
            ),
        },
    ),
    Core.SINGBOX: CoreRelease(
        version="1.13.19",
        base_url="https://github.com/SagerNet/sing-box/releases/download/v1.13.19",
        binary="sing-box",
        assets={
            "amd64": CoreAsset(
                filename="sing-box-1.13.19-linux-amd64.tar.gz",
                sha256="ef88a9e577d474210867bd708933d042e9b70106529df2656182c9db90106aa1",
                member="sing-box-1.13.19-linux-amd64/sing-box",
            ),
            "arm64": CoreAsset(
                filename="sing-box-1.13.19-linux-arm64.tar.gz",
                sha256="7fe3597a95a3c5ad67477b1d7653b9ce097e0be7c676758eba1fcf558f353d57",
                member="sing-box-1.13.19-linux-arm64/sing-box",
            ),
        },
    ),
}

_locks: dict[Core, asyncio.Lock] = {core: asyncio.Lock() for core in CORE_RELEASES}


def detect_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return "amd64"


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


def binary_path(core: Core, arch: Optional[str] = None) -> Path:
    release = CORE_RELEASES[core]
    return CORES_DIR / core.value / release.version / (arch or detect_arch()) / release.binary


def is_installed(core: Core) -> bool:
    path = binary_path(core)
    return path.is_file() and os.access(path, os.X_OK)


def installed_status() -> list[dict]:
    return [
        {
            "core": core.value,
            "version": release.version,
            "installed": is_installed(core),
            "path": str(binary_path(core)),
            "size": binary_path(core).stat().st_size if is_installed(core) else None,
        }
        for core, release in CORE_RELEASES.items()
    ]


async def ensure_core(core: Core) -> Path:
    """Путь к бинарнику ядра, при необходимости скачав его.

    Блокировка на ядро: параллельные тесты не должны качать один архив
    несколько раз и писать в один и тот же файл.
    """
    path = binary_path(core)
    if is_installed(core):
        return path

    async with _locks[core]:
        if is_installed(core):
            return path
        await asyncio.to_thread(_install_sync, core, await _download(core))
        return path


async def _download(core: Core) -> bytes:
    arch = detect_arch()
    release = CORE_RELEASES[core]
    asset = release.assets.get(arch)
    if asset is None:
        raise CoreDownloadError(f"Нет сборки {core.value} для архитектуры {arch}")

    direct = f"{release.base_url}/{asset.filename}"
    errors: list[str] = []
    for url in (direct, f"{GITHUB_MIRROR}{direct}"):
        try:
            payload = await _fetch(url)
        except Exception as exc:  # noqa: BLE001 — фолбэк на зеркало по любой сетевой причине
            errors.append(f"{url}: {exc}")
            continue

        digest = sha256(payload).hexdigest()
        if digest != asset.sha256:
            errors.append(f"{url}: контрольная сумма не совпала ({digest[:12]}…)")
            continue

        logger.info("xray-test: downloaded %s %s (%s)", core.value, release.version, arch)
        return payload

    raise CoreDownloadError("; ".join(errors))


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


def _install_sync(core: Core, payload: bytes) -> None:
    """Распаковка stdlib-средствами: unzip в образе панели нет."""
    release = CORE_RELEASES[core]
    arch = detect_arch()
    asset = release.assets[arch]
    target = binary_path(core, arch)
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=str(target.parent)) as tmp:
        tmp_dir = Path(tmp)
        archive = tmp_dir / asset.filename
        archive.write_bytes(payload)
        extracted = tmp_dir / release.binary

        if asset.filename.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                with zf.open(asset.member) as src, extracted.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        else:
            with tarfile.open(archive, "r:gz") as tf:
                member = tf.extractfile(asset.member)
                if member is None:
                    raise CoreDownloadError(f"В архиве нет файла {asset.member}")
                with extracted.open("wb") as dst:
                    shutil.copyfileobj(member, dst)

        extracted.chmod(extracted.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(extracted, target)
