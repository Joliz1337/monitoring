"""Виноват ли SOCKS5-прокси в том, что нода за ним не отвечает.

Приветствие и авторизацию SOCKS5 (RFC 1928/1929) прокси обрабатывает сам, не
подключаясь к цели, поэтому сбой на этих шагах указывает на прокси, а не на ноду.
Команду CONNECT не шлём: её отказ или зависание не отличить от мёртвой ноды —
прокси честно пытается до неё достучаться.
"""

import asyncio
import enum

from app.services.http_client import parse_proxy_input

SOCKS_VERSION = 0x05
AUTH_SUBNEGOTIATION_VERSION = 0x01
METHOD_NO_AUTH = 0x00
METHOD_USERNAME_PASSWORD = 0x02
AUTH_SUCCESS = 0x00
MAX_CREDENTIAL_BYTES = 255


class ProxyFault(str, enum.Enum):
    UNREACHABLE = "unreachable"
    SILENT = "silent"
    AUTH_REJECTED = "auth_rejected"
    NOT_SOCKS5 = "not_socks5"


async def find_proxy_fault(raw_proxy: str, timeout: float) -> ProxyFault | None:
    """None — прокси принял приветствие и авторизацию, то есть исправен."""
    host, port, login, password = parse_proxy_input(raw_proxy)
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError):
        return ProxyFault.UNREACHABLE

    try:
        return await asyncio.wait_for(_handshake(reader, writer, login, password), timeout)
    except (OSError, TimeoutError, asyncio.IncompleteReadError):
        return ProxyFault.SILENT
    finally:
        writer.close()


async def _handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    login: str | None,
    password: str | None,
) -> ProxyFault | None:
    method = METHOD_NO_AUTH if login is None else METHOD_USERNAME_PASSWORD
    writer.write(bytes([SOCKS_VERSION, 1, method]))
    await writer.drain()

    version, chosen_method = await reader.readexactly(2)
    if version != SOCKS_VERSION:
        return ProxyFault.NOT_SOCKS5
    if chosen_method != method:
        return ProxyFault.AUTH_REJECTED
    if method == METHOD_NO_AUTH:
        return None

    user, secret = login.encode(), (password or "").encode()
    if len(user) > MAX_CREDENTIAL_BYTES or len(secret) > MAX_CREDENTIAL_BYTES:
        return ProxyFault.AUTH_REJECTED
    writer.write(bytes([AUTH_SUBNEGOTIATION_VERSION, len(user)]) + user + bytes([len(secret)]) + secret)
    await writer.drain()

    _, status = await reader.readexactly(2)
    return None if status == AUTH_SUCCESS else ProxyFault.AUTH_REJECTED
