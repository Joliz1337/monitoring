"""Раздача ядер нодам.

Нода не ходит в GitHub: под жёсткой блокировкой он ей недоступен, а панель у
себя ядро уже скачала. Бинарник отдаётся по одноразовой ссылке, а команда для
ноды несёт ожидаемый SHA-256 — поэтому скачивание с самоподписанным
сертификатом панели безопасно: подмену ловит сверка хэша, а не TLS.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.services.net_utils import resolve_panel_ip
from app.services.xray_test.core_manager import ensure_core, resolve_release
from app.services.xray_test.models import Core

TOKEN_TTL_SECONDS = 300


@dataclass(frozen=True)
class BundleTicket:
    token: str
    url: str
    sha256: str
    version: str
    core: Core


@dataclass
class _Grant:
    path: Path
    digest: str
    expires_at: float


_grants: dict[str, _Grant] = {}
_digest_cache: dict[Path, str] = {}


async def issue_ticket(core: Core) -> BundleTicket:
    """Скачать ядро себе (если ещё нет) и выдать ноде одноразовую ссылку.

    Хэш считается по файлу, который панель уже проверила у себя, — нода
    сверяет с ним и потому не зависит от доверия к транспорту.
    """
    release = await resolve_release(core)
    path = await ensure_core(core, release.version)
    digest = await _digest(path)
    _drop_expired()

    token = secrets.token_urlsafe(32)
    _grants[token] = _Grant(path=path, digest=digest, expires_at=time.time() + TOKEN_TTL_SECONDS)

    return BundleTicket(
        token=token,
        url=f"{await panel_base_url()}/api/xray-test/bundle/{token}",
        sha256=digest,
        version=release.version,
        core=core,
    )


def redeem(token: str) -> Optional[Path]:
    """Одноразовое погашение: повторное скачивание по той же ссылке невозможно."""
    _drop_expired()
    grant = _grants.pop(token, None)
    if grant is None or grant.expires_at < time.time():
        return None
    return grant.path


async def panel_base_url() -> str:
    settings = get_settings()
    host = settings.domain.strip() or await resolve_panel_ip() or ""
    if not host:
        raise ValueError("Не задан домен панели — ноде некуда обратиться за ядром")

    port = settings.panel_port
    return f"https://{host}" if port == 443 else f"https://{host}:{port}"


async def _digest(path: Path) -> str:
    cached = _digest_cache.get(path)
    if cached:
        return cached
    digest = await asyncio.to_thread(_digest_sync, path)
    _digest_cache[path] = digest
    return digest


def _digest_sync(path: Path) -> str:
    hasher = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _drop_expired() -> None:
    now = time.time()
    for token in [key for key, grant in _grants.items() if grant.expires_at < now]:
        _grants.pop(token, None)
