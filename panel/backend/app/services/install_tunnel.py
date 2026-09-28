"""HTTP-прокси панели для установки ноды через SSH-туннель.

Сервер за ТСПУ не достаёт GitHub, Docker и реестры образов, а панель достаёт.
Панель просит sshd сервера слушать 127.0.0.1:<порт> и все подключения к нему
получает обратно по тому же SSH-соединению — установщик ходит в интернет через
панель. Слушатель живёт, пока жива SSH-сессия; на самой панели порт не открывается.

Сервер — не доверенная сторона для внутренней сети панели: наружу пускаем только
на 80/443 и только на публичные адреса, иначе через туннель были бы видны
postgres, бэкенд и метаданные облака панели.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable
from urllib.parse import SplitResult, urlsplit

import asyncssh

LISTEN_HOST = "127.0.0.1"
ALLOWED_PORTS = frozenset({80, 443})
MAX_HEADER_BYTES = 16384
HEADER_READ_TIMEOUT = 30
UPSTREAM_CONNECT_TIMEOUT = 15
MAX_TUNNEL_CONNECTIONS = 32
PIPE_CHUNK_BYTES = 65536
BYTES_IN_MB = 1024 * 1024

HEADER_END = b"\r\n\r\n"
DEFAULT_HTTP_PORT = 80
# Заголовки прокси-хопа: origin-серверу их не отдаём, соединение закрываем после ответа
HOP_HEADERS = frozenset({"proxy-connection", "proxy-authorization", "connection", "keep-alive"})
STATUS_PHRASES = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}
CONNECT_ESTABLISHED = b"HTTP/1.1 200 Connection established\r\n\r\n"

_STREAM_ERRORS = (OSError, asyncio.TimeoutError, asyncssh.Error, asyncio.IncompleteReadError)


class TunnelForwardingDenied(Exception):
    """sshd сервера отказал в пробросе порта (AllowTcpForwarding no)."""


class ProxyRequestError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class ProxyRequest:
    host: str
    port: int
    # Для обычного HTTP — переписанный заголовок запроса к origin; у CONNECT пусто
    upstream_head: bytes = b""

    @property
    def is_connect(self) -> bool:
        return not self.upstream_head

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"


def _split_host_port(authority: str) -> tuple[str, int]:
    host, sep, port = authority.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ProxyRequestError(400, f"некорректная цель {authority!r}")
    return host.strip("[]"), int(port)


def _origin_head(method: str, url: SplitResult, version: str, header_lines: list[str]) -> bytes:
    """Absolute-form → origin-form: заголовки прокси-хопа убираются, соединение
    с origin закрывается после ответа — один запрос на одно соединение туннеля."""
    path = url.path or "/"
    if url.query:
        path = f"{path}?{url.query}"

    kept = [line for line in header_lines if line.split(":", 1)[0].strip().lower() not in HOP_HEADERS]
    if not any(line.lower().startswith("host:") for line in kept):
        kept.append(f"Host: {url.netloc}")
    kept.append("Connection: close")
    return "\r\n".join([f"{method} {path} {version}", *kept]).encode("latin-1") + HEADER_END


def parse_proxy_request(head: bytes) -> ProxyRequest:
    """Разбирает заголовок запроса к прокси: CONNECT host:port или
    absolute-form `GET http://host/path` (apt ходит так на http-зеркала)."""
    lines = head.decode("latin-1").split("\r\n")
    request_line, header_lines = lines[0], [line for line in lines[1:] if line]
    parts = request_line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise ProxyRequestError(400, "некорректная строка запроса")
    method, target, version = parts

    if method.upper() == "CONNECT":
        host, port = _split_host_port(target)
        return ProxyRequest(host=host, port=port)

    url = urlsplit(target)
    if url.scheme != "http" or not url.hostname:
        raise ProxyRequestError(400, f"ожидался CONNECT или http://-адрес, получено {target!r}")
    try:
        port = url.port or DEFAULT_HTTP_PORT
    except ValueError as exc:
        raise ProxyRequestError(400, f"некорректный порт в {target!r}") from exc
    return ProxyRequest(
        host=url.hostname,
        port=port,
        upstream_head=_origin_head(method, url, version, header_lines),
    )


def is_allowed_address(raw_ip: str) -> bool:
    """Только глобально маршрутизируемые адреса: без loopback, RFC1918, CGNAT,
    link-local (169.254.169.254 — метаданные облака), ULA и multicast."""
    try:
        ip = ipaddress.ip_address(raw_ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def resolve_host(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ProxyRequestError(502, f"не резолвится: {exc}") from exc
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    # IPv4 первым: у контейнера панели IPv6-выхода может не быть
    return sorted(addresses, key=lambda address: ":" in address)


async def resolve_allowed_target(host: str, port: int, resolver: Resolver = resolve_host) -> list[str]:
    """Адреса цели, к которым можно подключаться. Подключение идёт к ним, а не
    к имени — повторный резолв мог бы вернуть уже внутренний адрес (DNS-rebinding)."""
    if port not in ALLOWED_PORTS:
        raise ProxyRequestError(403, f"порт {port} закрыт — туннель пускает только на 80/443")
    addresses = await resolver(host, port)
    if not addresses:
        raise ProxyRequestError(502, "не резолвится")
    blocked = [address for address in addresses if not is_allowed_address(address)]
    if blocked:
        raise ProxyRequestError(403, f"непубличный адрес {blocked[0]}")
    return addresses


@dataclass
class TunnelStats:
    connections: int = 0
    bytes_transferred: int = 0
    _notes: list[str] = field(default_factory=list)
    _seen_problems: set[str] = field(default_factory=set)

    def problem(self, target: str, reason: str) -> None:
        key = f"{target} — {reason}"
        if key in self._seen_problems:
            return
        self._seen_problems.add(key)
        self._notes.append(f"[panel] туннель: {key}")

    def drain_notes(self) -> list[str]:
        notes, self._notes = self._notes, []
        return notes

    def summary(self) -> str:
        megabytes = self.bytes_transferred / BYTES_IN_MB
        return f"[panel] Через панель передано {megabytes:.1f} МБ, соединений: {self.connections}"


async def _read_head(reader: asyncssh.SSHReader) -> tuple[bytes, bytes]:
    """Заголовок запроса и всё, что клиент успел прислать после него."""
    buffer = b""
    while HEADER_END not in buffer:
        if len(buffer) > MAX_HEADER_BYTES:
            raise ProxyRequestError(400, "слишком длинный заголовок")
        chunk = await reader.read(PIPE_CHUNK_BYTES)
        if not chunk:
            raise ProxyRequestError(400, "соединение закрыто до конца заголовка")
        buffer += chunk
    head, _, rest = buffer.partition(HEADER_END)
    if len(head) > MAX_HEADER_BYTES:
        raise ProxyRequestError(400, "слишком длинный заголовок")
    return head + HEADER_END, rest


async def _open_upstream(addresses: list[str], port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    last_error = "нет адресов"
    for address in addresses:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(address, port), timeout=UPSTREAM_CONNECT_TIMEOUT
            )
        except asyncio.TimeoutError:
            last_error = f"таймаут {UPSTREAM_CONNECT_TIMEOUT} с"
        except OSError as exc:
            last_error = exc.strerror or str(exc)
    raise ProxyRequestError(502, f"панель не может подключиться: {last_error}")


def _error_response(status: int) -> bytes:
    phrase = STATUS_PHRASES.get(status, "Error")
    return f"HTTP/1.1 {status} {phrase}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()


class InstallTunnel:
    def __init__(self, resolver: Resolver = resolve_host):
        self.stats = TunnelStats()
        self.port = 0
        self._resolver = resolver
        self._slots = asyncio.Semaphore(MAX_TUNNEL_CONNECTIONS)
        self._handlers: set[asyncio.Task] = set()

    @property
    def proxy_url(self) -> str:
        return f"http://{LISTEN_HOST}:{self.port}"

    def handler_factory(self, _orig_host: str, _orig_port: int):
        return self._handle

    async def _handle(self, reader: asyncssh.SSHReader, writer: asyncssh.SSHWriter) -> None:
        task = asyncio.current_task()
        if task:
            self._handlers.add(task)
        try:
            async with self._slots:
                await self._serve(reader, writer)
        finally:
            if task:
                self._handlers.discard(task)
            writer.close()

    async def _serve(self, reader: asyncssh.SSHReader, writer: asyncssh.SSHWriter) -> None:
        request: ProxyRequest | None = None
        try:
            head, rest = await asyncio.wait_for(_read_head(reader), timeout=HEADER_READ_TIMEOUT)
            request = parse_proxy_request(head)
            addresses = await resolve_allowed_target(request.host, request.port, self._resolver)
            up_reader, up_writer = await _open_upstream(addresses, request.port)
        except ProxyRequestError as exc:
            self.stats.problem(request.target if request else "запрос", exc.reason)
            with suppress(*_STREAM_ERRORS):
                writer.write(_error_response(exc.status))
            return
        except _STREAM_ERRORS:
            return

        self.stats.connections += 1
        try:
            if request.is_connect:
                writer.write(CONNECT_ESTABLISHED)
            else:
                up_writer.write(request.upstream_head)
            if rest:
                up_writer.write(rest)
            await asyncio.gather(
                self._pump(reader, up_writer),
                self._pump(up_reader, writer),
            )
        except _STREAM_ERRORS:
            pass
        finally:
            up_writer.close()

    async def _pump(self, source, sink) -> None:
        try:
            while chunk := await source.read(PIPE_CHUNK_BYTES):
                sink.write(chunk)
                await sink.drain()
                self.stats.bytes_transferred += len(chunk)
        except _STREAM_ERRORS:
            pass
        finally:
            # Полузакрытие: TLS и HTTP/1.0-клиенты дочитывают ответ после своего EOF
            with suppress(*_STREAM_ERRORS):
                sink.write_eof()

    async def close(self) -> None:
        handlers = list(self._handlers)
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


@asynccontextmanager
async def open_install_tunnel(
    conn: asyncssh.SSHClientConnection, resolver: Resolver = resolve_host
) -> AsyncIterator[InstallTunnel]:
    """Прокси на 127.0.0.1 сервера на время блока. Порт выбирает sshd сервера."""
    tunnel = InstallTunnel(resolver)
    try:
        listener = await conn.start_server(tunnel.handler_factory, LISTEN_HOST, 0)
    except asyncssh.ChannelListenError as exc:
        raise TunnelForwardingDenied(str(exc)) from exc
    tunnel.port = listener.get_port()
    try:
        yield tunnel
    finally:
        listener.close()
        await tunnel.close()
