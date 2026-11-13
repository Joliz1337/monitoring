"""SSH-доступ панели к уже добавленному серверу (креды хранятся на записи Server).

Им пользуются доставка образа ноды (по кнопке и как запасной путь, когда нода не
смогла обновиться сама) и установка Remnawave, когда сервер за ТСПУ качает всё
через панель.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

import asyncssh
from python_socks import ProxyError as SocksProxyError
from python_socks import ProxyType
from python_socks.async_.asyncio import Proxy as SocksProxy

from app.services.http_client import parse_proxy_input, sanitize_proxy

if TYPE_CHECKING:
    from app.models import Server

CONNECT_TIMEOUT = 30


@dataclass
class SSHTarget:
    host: str
    port: int = 22
    user: str = "root"
    password: Optional[str] = None
    private_key: Optional[str] = None
    passphrase: Optional[str] = None
    # SOCKS5-прокси панель→сервер ("ip:port[@login:pass]"): сервер, до которого
    # панель достаёт только через прокси, по SSH напрямую тоже не ответит
    socks5_proxy: Optional[str] = None


class SSHTargetError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason  # no_host | not_root | no_creds


def host_from_url(url: str) -> str:
    return urlparse(url).hostname or ""


def has_stored_creds(server: "Server") -> bool:
    return bool(server.ssh_password or server.ssh_private_key)


def resolve_ssh_target(
    server: "Server",
    *,
    ssh_host: Optional[str] = None,
    ssh_port: Optional[int] = None,
    ssh_user: Optional[str] = None,
    ssh_password: Optional[str] = None,
    ssh_private_key: Optional[str] = None,
    ssh_passphrase: Optional[str] = None,
) -> SSHTarget:
    """SSH-цель сервера: переданные поля поверх сохранённых у сервера."""
    host = (ssh_host or server.ssh_host or host_from_url(server.url)).strip()
    port = ssh_port or server.ssh_port or 22
    user = (ssh_user or server.ssh_user or "root").strip()
    password = ssh_password if ssh_password is not None else server.ssh_password
    private_key = ssh_private_key if ssh_private_key is not None else server.ssh_private_key
    passphrase = ssh_passphrase if ssh_passphrase is not None else server.ssh_passphrase

    if not host:
        raise SSHTargetError("no_host", "Не удалось определить SSH-хост ноды")
    if user != "root":
        raise SSHTargetError("not_root", "Нужен root-доступ по SSH")
    if not password and not private_key:
        raise SSHTargetError("no_creds", "Нет SSH-кредов: сохраните их у сервера или укажите в запросе")

    return SSHTarget(
        host=host, port=port, user=user,
        password=password, private_key=private_key, passphrase=passphrase,
        socks5_proxy=server.proxy_url or None,
    )


def ssh_connect_kwargs(t: SSHTarget) -> dict:
    kwargs: dict = {
        "host": t.host,
        "port": t.port,
        "username": t.user,
        "known_hosts": None,
        "connect_timeout": CONNECT_TIMEOUT,
        # Долгая заливка образа или установка по медленному/throttled-каналу:
        # keepalive держит SSH-сессию живой в паузах между шагами.
        "keepalive_interval": 15,
        "keepalive_count_max": 8,
    }
    if t.private_key:
        kwargs["client_keys"] = [asyncssh.import_private_key(t.private_key, t.passphrase or None)]
    elif t.password:
        kwargs["password"] = t.password
    return kwargs


async def open_ssh_connection(
    connect_kwargs: dict, socks5_proxy: Optional[str],
) -> asyncssh.SSHClientConnection:
    """SSH-подключение напрямую или через SOCKS5-прокси панель→сервер.

    С прокси сокет открывает python-socks, asyncssh получает его через sock=.
    Ошибки прокси приходят как OSError с адресом прокси без логина и пароля —
    их ловят те же обработчики, что и сетевые ошибки прямого подключения.
    """
    if not socks5_proxy:
        return await asyncssh.connect(**connect_kwargs)

    host, port, login, password = parse_proxy_input(socks5_proxy)
    proxy = SocksProxy(ProxyType.SOCKS5, host, port, username=login, password=password)
    timeout = connect_kwargs.get("connect_timeout", CONNECT_TIMEOUT)
    proxy_address = sanitize_proxy(socks5_proxy)
    try:
        sock = await proxy.connect(
            dest_host=connect_kwargs["host"], dest_port=connect_kwargs["port"], timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise OSError(f"SOCKS5-прокси {proxy_address} не ответил за {timeout} с") from exc
    except (SocksProxyError, OSError) as exc:
        raise OSError(f"SOCKS5-прокси {proxy_address}: {exc}") from exc
    return await asyncssh.connect(**connect_kwargs, sock=sock)
