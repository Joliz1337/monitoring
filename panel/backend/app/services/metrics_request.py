"""Запрос метрик у ноды: напрямую — общей сессией aiohttp, через SOCKS5 — клиентом httpx."""
from __future__ import annotations

import json
import zlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from app.services.http_client import (
    get_metrics_session,
    get_node_client,
    node_auth_headers,
    node_ssl_context,
)

if TYPE_CHECKING:
    from app.models import Server

# Ответ ноды — десятки килобайт JSON. Потолок нужен против взломанной ноды:
# без него она каждый цикл сбора отдавала бы gzip-бомбу и выедала память панели.
MAX_BODY_BYTES = 8 * 1024 * 1024
_HTTP_OK = 200
_GZIP_WBITS = zlib.MAX_WBITS | 16
_PLAIN_ENCODINGS = ("", "identity")
# Просим только gzip: остальное сжатие нода отдаёт лишь по запросу, а распаковывать его нечем
_ACCEPT_ENCODING = {"Accept-Encoding": "gzip"}


class MetricsBodyRejected(Exception):
    """Тело ответа с метриками не принято: больше потолка либо в неизвестном сжатии."""


class CappedBody:
    """Тело ответа, которое после распаковки не длиннее MAX_BODY_BYTES.

    Распаковка идёт здесь, а не в HTTP-клиенте: и aiohttp, и httpx разворачивают
    принятый кусок целиком, и килобайты gzip становятся десятками мегабайт раньше,
    чем размер удаётся проверить.
    """

    def __init__(self, content_encoding: str) -> None:
        encoding = content_encoding.strip().lower()
        if encoding != "gzip" and encoding not in _PLAIN_ENCODINGS:
            raise MetricsBodyRejected(f"unsupported Content-Encoding {content_encoding[:40]!r}")
        self._inflater = zlib.decompressobj(wbits=_GZIP_WBITS) if encoding == "gzip" else None
        self._parts: list[bytes] = []
        self._size = 0

    def feed(self, chunk: bytes) -> None:
        if self._inflater is None:
            self._keep(chunk)
            return
        while chunk:
            # На байт больше остатка: так «ровно потолок» отличим от «больше потолка»
            self._keep(self._inflater.decompress(chunk, MAX_BODY_BYTES - self._size + 1))
            chunk = self._inflater.unconsumed_tail

    def _keep(self, data: bytes) -> None:
        self._size += len(data)
        if self._size > MAX_BODY_BYTES:
            raise MetricsBodyRejected(f"body is over {MAX_BODY_BYTES} bytes")
        self._parts.append(data)

    def json(self) -> Any:
        return json.loads(b"".join(self._parts))


async def _read_json(content_encoding: str, chunks: AsyncIterator[bytes]) -> Any:
    body = CappedBody(content_encoding)
    async for chunk in chunks:
        body.feed(chunk)
    return body.json()


async def request_metrics(server: "Server", window_seconds: int, client_probe: bool = False) -> tuple[int, Any]:
    """HTTP-статус ответа ноды на `/api/metrics` и разобранное тело (None, если статус не 200).

    `client_probe` — просьба проверять адреса назначения как клиент: нода ведёт
    проверку, пока просьба повторяется, а без неё серверам ничего не шлёт.

    Сбой соединения приходит исключением своего транспорта: aiohttp у прямого
    запроса, httpx у запроса через прокси.
    """
    url = f"{server.url}/api/metrics"
    params = {"window": window_seconds}
    if client_probe:
        params["client_probe"] = 1
    headers = {**node_auth_headers(server), **_ACCEPT_ENCODING}
    if getattr(server, "proxy_url", None):
        return await _request_via_proxy(server, url, params, headers)
    return await _request_directly(server, url, params, headers)


async def _request_directly(
    server: "Server", url: str, params: dict[str, int], headers: dict[str, str],
) -> tuple[int, Any]:
    async with get_metrics_session().get(
        url, params=params, headers=headers, ssl=node_ssl_context(server), allow_redirects=False,
    ) as response:
        if response.status != _HTTP_OK:
            return response.status, None
        encoding = response.headers.get("Content-Encoding", "")
        return response.status, await _read_json(encoding, response.content.iter_any())


async def _request_via_proxy(
    server: "Server", url: str, params: dict[str, int], headers: dict[str, str],
) -> tuple[int, Any]:
    # Таймауты — из клиента ноды (_NODE_TIMEOUT)
    async with get_node_client(server).stream("GET", url, params=params, headers=headers) as response:
        if response.status_code != _HTTP_OK:
            return response.status_code, None
        encoding = response.headers.get("Content-Encoding", "")
        return response.status_code, await _read_json(encoding, response.aiter_raw())
