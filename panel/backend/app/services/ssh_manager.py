import logging

import httpx
from app.services.http_client import get_node_client, node_auth_headers

logger = logging.getLogger(__name__)

RECOMMENDED_PRESET = {
    "ssh": {
        "port": 1794,
        "permit_root_login": "yes",
        "password_authentication": True,
        "pubkey_authentication": True,
        "max_auth_tries": 3,
        "login_grace_time": 60,
        "client_alive_interval": 300,
        "client_alive_count_max": 2,
        "max_sessions": 3,
        "x11_forwarding": False,
        "allow_users": ["root"],
    },
    "fail2ban": {
        "enabled": True,
        "max_retry": 5,
        "ban_time": 3600,
        "find_time": 600,
    },
}

MAXIMUM_PRESET = {
    "ssh": {
        "port": 1794,
        "permit_root_login": "no",
        "password_authentication": False,
        "pubkey_authentication": True,
        "max_auth_tries": 2,
        "login_grace_time": 30,
        "client_alive_interval": 120,
        "client_alive_count_max": 2,
        "max_sessions": 2,
        "x11_forwarding": False,
    },
    "fail2ban": {
        "enabled": True,
        "max_retry": 3,
        "ban_time": 86400,
        "find_time": 3600,
    },
}


async def proxy_to_node(
    server,
    method: str,
    path: str,
    json_data: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    client = get_node_client(server)
    url = f"{server.url}{path}"
    headers = node_auth_headers(server)

    try:
        response = await client.request(
            method=method,
            url=url,
            headers=headers,
            json=json_data,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except httpx.ConnectError:
        raise ConnectionError(f"Node {server.name} unreachable")
    except httpx.TimeoutException:
        raise TimeoutError(f"Node {server.name} request timed out")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise LookupError(f"Node {server.name} does not support SSH management (update required)")
        detail = ""
        try:
            detail = e.response.json().get("detail", "")
        except Exception:
            pass
        raise RuntimeError(detail or f"Node {server.name} returned {e.response.status_code}")
