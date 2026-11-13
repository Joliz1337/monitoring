"""Кэш образа ноды на панели — для доставки на заблокированные ноды (ТСПУ).

Панель достаёт реестр (обновляет себя с GHCR), тянет образ ноды и хранит gzip-tar
ровно одного образа, переиспользуя его на весь флот. Пересохраняет только когда
образ сменился — при «Обновить все» многие ноды берут один и тот же tar дёшево.
Рядом с архивом лежит его описание: sha256 (по нему доставка узнаёт на ноде свой
недокачанный файл и сверяет докачанный) и размер распакованного образа (сколько
места он займёт на диске ноды).
"""
import asyncio
import gzip
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import docker

logger = logging.getLogger(__name__)

NODE_IMAGE_REPO = "ghcr.io/joliz1337/monitoring-node-api"
CACHE_DIR = Path("/app/data/node-images")

_locks: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class NodeImageArchive:
    path: Path
    sha256: str
    image_size: int  # распакованный образ, байт

    @property
    def size(self) -> int:
        return self.path.stat().st_size


def _lock_for(tag: str) -> asyncio.Lock:
    lock = _locks.get(tag)
    if lock is None:
        lock = asyncio.Lock()
        _locks[tag] = lock
    return lock


def image_ref(tag: str) -> str:
    return f"{NODE_IMAGE_REPO}:{tag}"


def _tar_path(tag: str) -> Path:
    return CACHE_DIR / f"node-api-{tag}.tar.gz"


def _read_meta(meta_file: Path, image_id: str) -> Optional[dict]:
    """Описание архива, если оно про этот образ; повреждённое или чужое — None."""
    try:
        meta = json.loads(meta_file.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("image_id") != image_id:
        return None
    if not isinstance(meta.get("sha256"), str) or not isinstance(meta.get("image_size"), int):
        return None
    return meta


def _pull_and_save(ref: str, dest: Path) -> NodeImageArchive:
    client = docker.from_env()
    image = client.images.pull(ref)
    meta_file = dest.with_suffix(".json")
    meta = _read_meta(meta_file, image.id) if dest.exists() else None
    if meta is not None:
        # tar актуален — pull переиспользовал слои, пересохранять нечего
        return NodeImageArchive(dest, meta["sha256"], meta["image_size"])

    tmp = dest.with_suffix(".tmp")
    # named=True кладёт repo:tag в архив — иначе docker load не восстановил бы тег
    with gzip.open(tmp, "wb") as gz:
        for chunk in image.save(named=True):
            gz.write(chunk)
    with tmp.open("rb") as archive:
        sha256 = hashlib.file_digest(archive, "sha256").hexdigest()
    tmp.replace(dest)

    image_size = int(image.attrs.get("Size") or 0)
    meta_file.write_text(json.dumps({"image_id": image.id, "sha256": sha256, "image_size": image_size}))
    # Файл прежнего формата кэша (только id образа)
    dest.with_suffix(".digest").unlink(missing_ok=True)
    return NodeImageArchive(dest, sha256, image_size)


async def ensure_image(tag: str) -> NodeImageArchive:
    """Гарантировать локальный gzip-tar образа ноды тега `tag`."""
    ref = image_ref(tag)
    dest = _tar_path(tag)
    async with _lock_for(tag):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        archive = await asyncio.to_thread(_pull_and_save, ref, dest)
    logger.info(f"Node image ready for delivery: {dest.name}")
    return archive
