"""Коммит, на который ветка (или тег) указывает в репозитории на GitHub.

Апдейтеры панели и нод при недоступном github.com клонируют код через стороннее
зеркало и сверяют полученный HEAD с этим коммитом (update_source). Узнаётся он
только у самого GitHub — спроси панель зеркало, сверка ничего бы не значила.
Не узнали — None: апдейтеры обойдутся без зеркала, обновление не отменяется.
"""

import asyncio
import logging
import re
import time
from typing import Optional
from urllib.parse import quote

import httpx

from app.services.http_client import get_external_client
from app.services.update_channel import GITHUB_REPO
from app.services.update_source import COMMIT_SHA_PATTERN, GIT_REF_PATTERN

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
# Ответом будет голый SHA вместо JSON коммита
SHA_MEDIA_TYPE = "application/vnd.github.sha"
GITHUB_TIMEOUT_SEC = 10.0
# Таймаут httpx — на каждую фазу запроса, а не на весь: ответ, капающий по байту,
# держал бы общий лок, а за ним — всю волну обновлений
GITHUB_TOTAL_TIMEOUT_SEC = 15.0
# «Обновить всё» запускает обновления всех нод разом, а без токена GitHub API
# даёт 60 запросов в час на IP — один ответ на всю волну. Дольше держать нельзя:
# после пуша клон с зеркала разошёлся бы со старым SHA и обновление отменилось бы
COMMIT_TTL_SEC = 60.0
# Неудачу тоже запоминаем: иначе каждая нода волны ждала бы свой таймаут к GitHub по очереди
COMMIT_FAILED_TTL_SEC = 30.0

_COMMIT_RE = re.compile(COMMIT_SHA_PATTERN)
_REF_RE = re.compile(GIT_REF_PATTERN)

_cache: dict[str, tuple[float, Optional[str]]] = {}
_lock = asyncio.Lock()


def _is_plain_ref(ref: object) -> bool:
    # Ветку ноды браузер присылает как есть, проверяет её уже сама нода.
    # «..» git в именах ссылок не допускает, а в пути запроса к API это выход из /commits/
    return isinstance(ref, str) and bool(_REF_RE.fullmatch(ref)) and ".." not in ref


async def _fetch_commit(ref: str) -> Optional[str]:
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/commits/{quote(ref, safe='/')}"
    try:
        response = await asyncio.wait_for(
            get_external_client().get(
                url, headers={"Accept": SHA_MEDIA_TYPE}, timeout=GITHUB_TIMEOUT_SEC,
            ),
            GITHUB_TOTAL_TIMEOUT_SEC,
        )
    except (httpx.HTTPError, TimeoutError) as e:
        logger.warning("upstream_commit_fetch_failed ref=%s error=%r", ref, e)
        return None
    if response.status_code != 200:
        logger.warning("upstream_commit_fetch_failed ref=%s status=%s", ref, response.status_code)
        return None
    commit = response.text.strip()
    if not _COMMIT_RE.fullmatch(commit):
        logger.warning("upstream_commit_unexpected_body ref=%s body=%r", ref, commit[:80])
        return None
    return commit


def _cached(ref: str) -> tuple[bool, Optional[str]]:
    entry = _cache.get(ref)
    if entry is None or time.monotonic() >= entry[0]:
        return False, None
    return True, entry[1]


async def resolve(ref: str) -> Optional[str]:
    """Полный SHA коммита ref на GitHub или None, если узнать не удалось."""
    if not _is_plain_ref(ref):
        return None
    async with _lock:
        hit, commit = _cached(ref)
        if hit:
            return commit
        commit = await _fetch_commit(ref)
        ttl = COMMIT_TTL_SEC if commit else COMMIT_FAILED_TTL_SEC
        _cache[ref] = (time.monotonic() + ttl, commit)
        return commit
