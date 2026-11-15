"""Проверка адреса назначения «как клиент».

Подключение к серверу проходит и там, где данные не идут: рукопожатие TCP
фильтрация на пути пропускает, а дальше съедает всё. Замер потерь такого не
видит — он считает только подключения. Здесь релей начинает с сервером
настоящее TLS-рукопожатие, как обычный клиент, и ждёт любого ответа.

Ответом считается любой байт: и продолжение рукопожатия, и отказ от него
(алерт сайта-заглушки) показывают одно — данные по пути ходят в обе стороны.
Что именно ответил сервер, не читается и ничему не доверяется.

Сервер, которому правило HAProxy шлёт PROXY-заголовок, без него разговаривать
не станет — заголовок уходит перед рукопожатием, тем же видом, что в правиле.
"""

import asyncio
import ipaddress
import ssl
import struct
from enum import Enum
from typing import Optional

CLIENT_PROBE_TIMEOUT_SEC = 4.0
PROXY_V2_SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"
PROXY_V2_VERSION_COMMAND = b"\x21"
PROXY_V2_TCP4 = b"\x11"
PROXY_V2_TCP6 = b"\x21"

Endpoint = tuple[str, int]


class ProxyHeader(str, Enum):
    V1 = "v1"
    V2 = "v2"


def proxy_header(version: ProxyHeader, source: Endpoint, destination: Endpoint) -> bytes:
    """Заголовок с адресами самого проверочного подключения — как у проверок HAProxy."""
    source_ip, destination_ip = ipaddress.ip_address(source[0]), ipaddress.ip_address(destination[0])
    over_ipv6 = source_ip.version == 6
    if version is ProxyHeader.V1:
        family = "TCP6" if over_ipv6 else "TCP4"
        return f"PROXY {family} {source_ip} {destination_ip} {source[1]} {destination[1]}\r\n".encode()
    addresses = source_ip.packed + destination_ip.packed + struct.pack("!HH", source[1], destination[1])
    family = PROXY_V2_TCP6 if over_ipv6 else PROXY_V2_TCP4
    return PROXY_V2_SIGNATURE + PROXY_V2_VERSION_COMMAND + family + struct.pack("!H", len(addresses)) + addresses


def _client_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Сертификат не проверяется: ответ сервера не читается, важен только сам факт ответа
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(["h2", "http/1.1"])
    return context


_CONTEXT = _client_context()


def client_hello() -> bytes:
    """Первый пакет рукопожатия от настоящей TLS-библиотеки."""
    outgoing = ssl.MemoryBIO()
    handshake = _CONTEXT.wrap_bio(ssl.MemoryBIO(), outgoing)
    try:
        handshake.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return outgoing.read()


async def probe(ip: str, port: int, proxy: Optional[ProxyHeader] = None,
                timeout: float = CLIENT_PROBE_TIMEOUT_SEC) -> Optional[bool]:
    """True — сервер ответил; False — подключение есть, ответа нет; None — подключиться
    не удалось: это потери, а не молчание."""
    hello = client_hello()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except (asyncio.TimeoutError, OSError):
        return None
    try:
        if proxy is not None:
            source, destination = writer.get_extra_info("sockname"), writer.get_extra_info("peername")
            if source is None or destination is None:
                # Сервер оборвал соединение сразу после подключения: адресов для заголовка уже нет
                return False
            hello = proxy_header(proxy, source[:2], destination[:2]) + hello
        writer.write(hello)
        return bool(await asyncio.wait_for(reader.read(1), timeout))
    except (asyncio.TimeoutError, OSError):
        return False
    finally:
        writer.transport.abort()
