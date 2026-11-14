"""SSH-доступ панели к уже добавленному серверу (креды хранятся на записи Server).

Им пользуются доставка образа ноды (по кнопке и как запасной путь, когда нода не
смогла обновиться сама) и установка Remnawave, когда сервер за ТСПУ качает всё
через панель.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import asyncssh
from python_socks import ProxyError as SocksProxyError
from python_socks import ProxyType
from python_socks.async_.asyncio import Proxy as SocksProxy
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from app.database import async_session_maker
from app.models import Server
from app.services.http_client import parse_proxy_input, sanitize_proxy

logger = logging.getLogger(__name__)

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
    # Запись сервера и закреплённый за ней ключ хоста (формат OpenSSH): с ключом
    # сервер сверяется до аутентификации, без него первое подключение его запоминает
    server_id: Optional[int] = None
    host_key: Optional[str] = None


class SSHTargetError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason  # no_host | not_root | no_creds


class HostKeyChangedError(Exception):
    """Сервер предъявил не тот ключ хоста, что закреплён за ним. Сверка идёт до
    аутентификации, поэтому пароль и ключ панели такому серверу не уходят."""

    def __init__(self, host: str):
        super().__init__(
            f"SSH: ключ хоста {host} не совпадает с запомненным при первом подключении — "
            "соединение прервано до входа, пароль и ключ не отправлены. Если сервер "
            "переустановлен, сохраните его SSH-доступ заново (с паролем или ключом): "
            "панель примет новый ключ хоста"
        )


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
        server_id=server.id, host_key=server.ssh_host_key,
    )


def _trust_only(host_key: str) -> tuple:
    """known_hosts для asyncssh: доверенные ключи хоста, доверенные CA, отозванные ключи."""
    return ([asyncssh.import_public_key(host_key)], [], [])


def presented_host_key(conn: asyncssh.SSHClientConnection) -> Optional[str]:
    """Ключ хоста, которым сервер подтвердил это подключение, в формате OpenSSH."""
    key = conn.get_server_host_key()
    return key.export_public_key("openssh").decode().strip() if key else None


def ssh_connect_kwargs(t: SSHTarget) -> dict:
    kwargs: dict = {
        "host": t.host,
        "port": t.port,
        "username": t.user,
        "known_hosts": _trust_only(t.host_key) if t.host_key else None,
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
    connect_kwargs: dict, socks5_proxy: Optional[str], server_id: Optional[int] = None,
) -> asyncssh.SSHClientConnection:
    """SSH-подключение напрямую или через SOCKS5-прокси панель→сервер.

    Без закреплённого ключа хоста (known_hosts=None) подключение доверяет
    предъявленному и закрепляет его: в connect_kwargs — следующие подключения
    той же задачи сверяются уже с ним — и в записи сервера server_id.
    """
    pinned = connect_kwargs["known_hosts"] is not None
    try:
        conn = await _connect(connect_kwargs, socks5_proxy)
    except asyncssh.HostKeyNotVerifiable as exc:
        if not pinned:
            raise
        raise HostKeyChangedError(connect_kwargs["host"]) from exc
    if pinned:
        return conn
    try:
        await _pin_host_key(conn, connect_kwargs, server_id)
    except BaseException:
        # Задачу отменили, пока ключ писался в базу: подключение открыто, а отдать его некому
        conn.close()
        raise
    return conn


async def _pin_host_key(
    conn: asyncssh.SSHClientConnection, connect_kwargs: dict, server_id: Optional[int],
) -> None:
    host_key = presented_host_key(conn)
    if not host_key:
        return
    connect_kwargs["known_hosts"] = _trust_only(host_key)
    if server_id is not None:
        await _remember_host_key(server_id, host_key)


async def _remember_host_key(server_id: int, host_key: str) -> None:
    try:
        async with async_session_maker() as db:
            # IS NULL: параллельное первое подключение могло закрепить ключ раньше —
            # эталоном остаётся записанный первым
            result = await db.execute(
                update(Server)
                .where(Server.id == server_id, Server.ssh_host_key.is_(None))
                .values(ssh_host_key=host_key)
            )
            await db.commit()
    except (SQLAlchemyError, OSError) as exc:
        # Само подключение состоялось; ключ закрепит следующее
        logger.warning(f"SSH host key of server {server_id} was not saved: {exc}")
        return
    if result.rowcount:
        logger.info(f"SSH host key pinned for server {server_id}")


async def _connect(connect_kwargs: dict, socks5_proxy: Optional[str]) -> asyncssh.SSHClientConnection:
    """С прокси сокет открывает python-socks, asyncssh получает его через sock=.
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
