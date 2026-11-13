"""Списки IP/CIDR по URL (Cloudflare, Yandex Cloud…) — часть общего белого списка.

Адрес из такого списка получает на нодах то же, что и вписанный руками: проход
мимо блок-листа, файрвола и лимитов аварийного режима. Составляет список при
этом чужой сайт, поэтому в белый список идут только публичные IPv4 не шире /8,
а сбой скачивания или ответ без адресов оставляют в силе последнюю удачную
копию — иначе заминка внешнего сайта сняла бы CDN с белого списка всего парка.
"""

import asyncio
import ipaddress
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select

from app.database import async_session
from app.models import AllowlistSource
from app.services.http_client import get_external_client
from app.services.net_utils import is_public_range

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 3600
# Список, который не скачался, спрашиваем чаще, но не на каждой сборке белого
# списка: зависший сайт задерживал бы каждую на весь таймаут
RETRY_SECONDS = 300
FETCH_TIMEOUT = 15.0
# Шире /8 — от шестнадцати миллионов адресов: это уже не список CDN
WIDEST_PREFIX = 8
NO_ADDRESSES_ERROR = "no public IPv4/CIDR found in the response"
ERROR_MESSAGE_LIMIT = 500

# Формат не важен (JSON Yandex Cloud, текст Cloudflare): адреса ищутся в теле целиком
_IPV4_CIDR_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?:/\d{1,2})?\b")


def extract_networks(content: str) -> list[str]:
    """Публичные IPv4-сети не шире /8 из произвольного текста, без повторов."""
    found: dict[str, None] = {}
    for match in _IPV4_CIDR_RE.finditer(content):
        try:
            network = ipaddress.ip_network(match.group(0), strict=False)
        except ValueError:
            continue
        if network.prefixlen < WIDEST_PREFIX or not is_public_range(str(network)):
            continue
        found[str(network)] = None
    return list(found)


@dataclass(frozen=True)
class SourceAnswer:
    networks: list[str]
    error: Optional[str] = None


class AllowlistSources:
    def __init__(self) -> None:
        self._good: dict[str, list[str]] = {}
        self._next_fetch: dict[str, float] = {}
        # Сборки белого списка идут и параллельно: один список — одно скачивание
        self._lock = asyncio.Lock()

    def expire(self) -> None:
        """Скачать все списки заново при следующей сборке. Прежние копии
        остаются запасными."""
        self._next_fetch.clear()

    async def collect(self) -> list[str]:
        """Адреса всех включённых списков; итог скачивания отмечается в базе."""
        async with self._lock:
            async with async_session() as db:
                rows = (await db.execute(
                    select(AllowlistSource.id, AllowlistSource.url)
                    .where(AllowlistSource.enabled == True)  # noqa: E712
                    .order_by(AllowlistSource.id)
                )).all()
            self._forget_all_but({url for _, url in rows})

            now = asyncio.get_running_loop().time()
            due = [(source_id, url) for source_id, url in rows if now >= self._next_fetch.get(url, now)]
            if due:
                answers = await asyncio.gather(*(self._download(url) for _, url in due))
                for (_, url), answer in zip(due, answers):
                    self._remember(url, answer, now)
                await self._record({source_id: answer for (source_id, _), answer in zip(due, answers)})

            return [network for _, url in rows for network in self._good.get(url, [])]

    def _forget_all_but(self, urls: set[str]) -> None:
        self._good = {url: networks for url, networks in self._good.items() if url in urls}
        self._next_fetch = {url: due for url, due in self._next_fetch.items() if url in urls}

    def _remember(self, url: str, answer: SourceAnswer, now: float) -> None:
        if answer.error is None:
            self._good[url] = answer.networks
            self._next_fetch[url] = now + REFRESH_SECONDS
            return
        self._next_fetch[url] = now + RETRY_SECONDS
        kept = "the last good copy stays in the allowlist" if url in self._good else "nothing to fall back on"
        logger.warning(f"Allowlist source {url} failed: {answer.error} — {kept}")

    async def _download(self, url: str) -> SourceAnswer:
        try:
            response = await get_external_client().get(url, timeout=FETCH_TIMEOUT)
        except httpx.TimeoutException:
            return SourceAnswer([], "timeout")
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as error:
            return SourceAnswer([], str(error) or type(error).__name__)
        if response.status_code != 200:
            return SourceAnswer([], f"HTTP {response.status_code}")
        networks = extract_networks(response.text)
        if not networks:
            return SourceAnswer([], NO_ADDRESSES_ERROR)
        return SourceAnswer(networks)

    async def _record(self, answers: dict[int, SourceAnswer]) -> None:
        async with async_session() as db:
            sources = (await db.execute(
                select(AllowlistSource).where(AllowlistSource.id.in_(answers))
            )).scalars().all()
            for source in sources:
                answer = answers[source.id]
                source.error_message = answer.error[:ERROR_MESSAGE_LIMIT] if answer.error else None
                # Счётчик — то, что список даёт белому списку сейчас: после сбоя
                # это запасная копия, а без неё (панель перезапускалась) — ничего
                source.ip_count = len(self._good.get(source.url, []))
                if answer.error is None:
                    source.last_updated = datetime.now(timezone.utc)
            await db.commit()


_sources: Optional[AllowlistSources] = None


def get_allowlist_sources() -> AllowlistSources:
    global _sources
    if _sources is None:
        _sources = AllowlistSources()
    return _sources
