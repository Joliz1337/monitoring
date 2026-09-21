"""HTTP к эндпоинтам exit-прокси агента; гейт прав и версии ноды — здесь же.

Все запросы короткие: конфиг применяется мгновенно, проверки нода гоняет в
фоне, а панель забирает результат через /status. Поэтому таймаут ниже
30-секундного `location /` в nginx ноды — своего location у этих путей нет.
"""

from typing import Any, Optional

import httpx

from app.models import Server
from app.services.http_client import get_node_client, node_auth_headers
from app.services.node_capabilities import Capability, denied_message, learn_from_denial, server_allows
from app.services.reserved_ports_sync import _version_tuple

MIN_NODE_VERSION_EXIT_PROXY = "10.29.0"
NODE_TIMEOUT_SEC = 25.0
BASE_PATH = "/api/system/exit-proxy"


class ExitProxyNodeError(Exception):
    pass


class ExitProxyNodeDenied(ExitProxyNodeError):
    pass


class ExitProxyNodeUnsupported(ExitProxyNodeError):
    pass


def node_supports_exit_proxy(node_version: Optional[str]) -> bool:
    if not node_version:
        return False
    return _version_tuple(node_version) >= _version_tuple(MIN_NODE_VERSION_EXIT_PROXY)


def ensure_node_ready(server: Server) -> None:
    if not node_supports_exit_proxy(server.node_version):
        raise ExitProxyNodeUnsupported(
            f"агент {server.node_version or 'unknown'} старше {MIN_NODE_VERSION_EXIT_PROXY} — обновите ноду"
        )
    if not server_allows(server, Capability.SYSTEM, write=True):
        raise ExitProxyNodeDenied(denied_message(Capability.SYSTEM, True))


async def _request(
    server: Server, method: str, path: str, *, json_body: Any = None, params: Optional[dict] = None,
) -> httpx.Response:
    ensure_node_ready(server)
    try:
        client = get_node_client(server)
        response = await client.request(
            method, f"{server.url}{BASE_PATH}{path}",
            headers=node_auth_headers(server), json=json_body, params=params, timeout=NODE_TIMEOUT_SEC,
        )
    except httpx.TimeoutException as exc:
        raise ExitProxyNodeError("таймаут соединения с нодой") from exc
    except httpx.RequestError as exc:
        raise ExitProxyNodeError(f"ошибка соединения с нодой: {exc}") from exc

    if response.status_code == 404:
        raise ExitProxyNodeUnsupported("нода не знает exit-прокси — обновите ноду")
    if response.status_code >= 400 and response.status_code != 409:
        try:
            body = response.json()
        except ValueError:
            body = response.text
        await learn_from_denial(server.id, response.status_code, body)
        detail = body.get("detail") if isinstance(body, dict) else None
        raise ExitProxyNodeError(f"нода ответила HTTP {response.status_code}: {str(detail or response.text)[:200]}")
    return response


async def push_config(server: Server, config: dict) -> dict:
    response = await _request(server, "PUT", "/config", json_body=config)
    return response.json()


async def fetch_status(server: Server) -> dict:
    response = await _request(server, "GET", "/status")
    return response.json()


async def start_check(server: Server) -> bool:
    """True — прогон запущен, False — нода уже проверяет (409)."""
    response = await _request(server, "POST", "/check")
    return response.status_code != 409


async def switch_exit(server: Server, candidate: str) -> dict:
    response = await _request(server, "POST", "/switch", json_body={"candidate": candidate})
    return response.json()
