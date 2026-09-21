"""HTTP к эндпоинтам пула исходящих адресов агента; гейт прав и версии ноды — здесь же.

Запросы короткие: нода применяет раскладку сразу и отвечает состоянием. Таймаут
ниже 30-секундного `location /` в nginx ноды — своего location у этих путей нет.
"""

from typing import Any, Optional

import httpx

from app.models import Server
from app.services.http_client import get_node_client, node_auth_headers
from app.services.node_capabilities import Capability, denied_message, learn_from_denial, server_allows
from app.services.reserved_ports_sync import _version_tuple

MIN_NODE_VERSION_SOURCE_POOL = "10.29.0"
NODE_TIMEOUT_SEC = 25.0
BASE_PATH = "/api/system/source-pool"


class SourcePoolNodeError(Exception):
    pass


class SourcePoolNodeDenied(SourcePoolNodeError):
    pass


class SourcePoolNodeUnsupported(SourcePoolNodeError):
    pass


class SourcePoolNodeConflict(SourcePoolNodeError):
    """Нода отказала: на ней включён exit-прокси, который сам выбирает исходящий адрес."""


def node_supports_source_pool(node_version: Optional[str]) -> bool:
    if not node_version:
        return False
    return _version_tuple(node_version) >= _version_tuple(MIN_NODE_VERSION_SOURCE_POOL)


def ensure_node_ready(server: Server) -> None:
    if not node_supports_source_pool(server.node_version):
        raise SourcePoolNodeUnsupported(
            f"агент {server.node_version or 'unknown'} старше {MIN_NODE_VERSION_SOURCE_POOL} — обновите ноду"
        )
    if not server_allows(server, Capability.SYSTEM, write=True):
        raise SourcePoolNodeDenied(denied_message(Capability.SYSTEM, True))


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail or response.text)[:200]


async def _request(server: Server, method: str, path: str, *, json_body: Any = None) -> httpx.Response:
    ensure_node_ready(server)
    try:
        client = get_node_client(server)
        response = await client.request(
            method, f"{server.url}{BASE_PATH}{path}",
            headers=node_auth_headers(server), json=json_body, timeout=NODE_TIMEOUT_SEC,
        )
    except httpx.TimeoutException as exc:
        raise SourcePoolNodeError("таймаут соединения с нодой") from exc
    except httpx.RequestError as exc:
        raise SourcePoolNodeError(f"ошибка соединения с нодой: {exc}") from exc

    if response.status_code == 404:
        raise SourcePoolNodeUnsupported("нода не знает пул исходящих адресов — обновите ноду")
    if response.status_code == 409:
        raise SourcePoolNodeConflict(_detail(response))
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = response.text
        await learn_from_denial(server.id, response.status_code, body)
        raise SourcePoolNodeError(f"нода ответила HTTP {response.status_code}: {_detail(response)}")
    return response


async def push_config(server: Server, config: dict) -> dict:
    response = await _request(server, "PUT", "/config", json_body=config)
    return response.json()


async def fetch_state(server: Server) -> dict:
    response = await _request(server, "GET", "/state")
    return response.json()
