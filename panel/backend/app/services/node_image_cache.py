"""Кэш образа ноды на панели — для доставки на заблокированные ноды (ТСПУ).

Панель достаёт реестр (обновляет себя с GHCR), тянет образ ноды и хранит gzip-tar
ровно одного образа, переиспользуя его на весь флот. Пересохраняет только когда
digest сменился — при «Обновить все» многие ноды берут один и тот же tar дёшево.
"""
import asyncio
import gzip
import logging
from pathlib import Path

import docker

logger = logging.getLogger(__name__)

NODE_IMAGE_REPO = "ghcr.io/joliz1337/monitoring-node-api"
CACHE_DIR = Path("/app/data/node-images")

_locks: dict[str, asyncio.Lock] = {}


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


def _pull_and_save(ref: str, dest: Path) -> None:
    client = docker.from_env()
    image = client.images.pull(ref)
    digest_file = dest.with_suffix(".digest")
    if dest.exists() and digest_file.exists() and digest_file.read_text().strip() == image.id:
        return  # tar актуален — pull переиспользовал слои, пересохранять нечего
    tmp = dest.with_suffix(".tmp")
    # named=True кладёт repo:tag в архив — иначе docker load не восстановил бы тег
    with gzip.open(tmp, "wb") as gz:
        for chunk in image.save(named=True):
            gz.write(chunk)
    tmp.replace(dest)
    digest_file.write_text(image.id)


async def ensure_image(tag: str) -> Path:
    """Гарантировать локальный gzip-tar образа ноды тега `tag`. Возвращает путь."""
    ref = image_ref(tag)
    dest = _tar_path(tag)
    async with _lock_for(tag):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_pull_and_save, ref, dest)
    logger.info(f"Node image ready for delivery: {dest.name}")
    return dest
