"""SOCKS5-сервер на loopback: соединения выходят через выбранный менеджером IP или WARP.

Только CONNECT по TCP: сюда xray Remnawave шлёт Google-трафик, а UDP/QUIC он
режет сам правилом. Каждое соединение помнит, через какой выход установлено:
при смене выхода менеджер рвёт все чужие — держать сессию на «протухшем» IP
смысла нет, клиент переподключится уже через новый.
"""

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

SOCKS_VERSION = 0x05
METHOD_NO_AUTH = 0x00
METHOD_NO_ACCEPTABLE = 0xFF
CMD_CONNECT = 0x01
ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_IPV6 = 0x04

REP_SUCCESS = 0x00
REP_GENERAL_FAILURE = 0x01
REP_NETWORK_UNREACHABLE = 0x03
REP_HOST_UNREACHABLE = 0x04
REP_CONNECTION_REFUSED = 0x05
REP_COMMAND_NOT_SUPPORTED = 0x07
REP_ADDRESS_NOT_SUPPORTED = 0x08

HANDSHAKE_TIMEOUT_SEC = 10
CONNECT_TIMEOUT_SEC = 15
PIPE_CHUNK = 64 * 1024
MAX_CONNECTIONS = 4096
MAX_DOMAIN_LENGTH = 255
LISTEN_BACKLOG = 512

Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
# Текущий выход: (id, как через него соединяться); None — выхода нет
Route = Callable[[], Optional[tuple[str, Connector]]]


class SocksProtocolError(Exception):
    """Клиент нарушил протокол; `reply` — код для ответа, None если отвечать уже нечем."""

    def __init__(self, reply: Optional[int], message: str):
        super().__init__(message)
        self.reply = reply


class SocksUpstreamError(OSError):
    """Socks-прокси WARP не установил соединение."""


@dataclass
class _Connection:
    exit_id: str
    client: asyncio.StreamWriter
    remote: Optional[asyncio.StreamWriter] = None


class SocksServer:
    def __init__(
        self,
        port: int,
        route: Route,
        host: str = "127.0.0.1",
        max_connections: int = MAX_CONNECTIONS,
    ):
        self._port = port
        self._route = route
        self._host = host
        self._max_connections = max_connections
        self._server: Optional[asyncio.AbstractServer] = None
        self._connections: dict[asyncio.Task, _Connection] = {}
        self.total_connections = 0
        self.failed_connections = 0

    @property
    def port(self) -> int:
        return self._port

    @property
    def listening(self) -> bool:
        return self._server is not None and self._server.is_serving()

    @property
    def active_connections(self) -> int:
        return len(self._connections)

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._serve_client, self._host, self._port, reuse_address=True, backlog=LISTEN_BACKLOG,
        )
        logger.info("Exit proxy SOCKS5 listening on %s:%s", self._host, self._port)

    async def stop(self) -> None:
        server, self._server = self._server, None
        self.drop_connections()
        if server is None:
            return
        server.close()
        await server.wait_closed()
        logger.info("Exit proxy SOCKS5 on %s:%s stopped", self._host, self._port)

    def drop_connections(self, except_exit: Optional[str] = None) -> int:
        """Оборвать соединения всех выходов, кроме `except_exit`. Возвращает число оборванных."""
        victims = [task for task, connection in self._connections.items() if connection.exit_id != except_exit]
        for task in victims:
            task.cancel()
        return len(victims)

    async def _serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        remote_writer: Optional[asyncio.StreamWriter] = None
        try:
            try:
                host, port = await asyncio.wait_for(self._handshake(reader, writer), HANDSHAKE_TIMEOUT_SEC)
            except SocksProtocolError as exc:
                if exc.reply is not None:
                    await self._reply(writer, exc.reply)
                return

            route = self._route()
            if route is None or len(self._connections) >= self._max_connections:
                self.failed_connections += 1
                await self._reply(writer, REP_GENERAL_FAILURE)
                return
            exit_id, connector = route
            connection = _Connection(exit_id=exit_id, client=writer)
            self._connections[task] = connection

            try:
                remote_reader, remote_writer = await asyncio.wait_for(connector(host, port), CONNECT_TIMEOUT_SEC)
            except (OSError, asyncio.TimeoutError, ValueError) as exc:
                self.failed_connections += 1
                logger.debug("Exit proxy: connect to %s:%s via %s failed: %s", host, port, exit_id, exc)
                await self._reply(writer, _reply_for(exc))
                return
            connection.remote = remote_writer
            self.total_connections += 1
            await self._reply(writer, REP_SUCCESS)
            await _relay(reader, writer, remote_reader, remote_writer)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            # Обрыв с любой стороны — обычное дело для прокси, не событие
            pass
        finally:
            self._connections.pop(task, None)
            _close(writer)
            _close(remote_writer)

    @staticmethod
    async def _handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[str, int]:
        version, method_count = await reader.readexactly(2)
        if version != SOCKS_VERSION:
            raise SocksProtocolError(None, "not a SOCKS5 client")
        methods = await reader.readexactly(method_count)
        if METHOD_NO_AUTH not in methods:
            writer.write(bytes([SOCKS_VERSION, METHOD_NO_ACCEPTABLE]))
            await writer.drain()
            raise SocksProtocolError(None, "client requires authentication")
        writer.write(bytes([SOCKS_VERSION, METHOD_NO_AUTH]))
        await writer.drain()

        version, command, _reserved, address_type = await reader.readexactly(4)
        if version != SOCKS_VERSION:
            raise SocksProtocolError(REP_GENERAL_FAILURE, "bad request version")
        # Адрес читается до проверки команды: ответ должен уйти на корректно разобранный запрос
        if address_type == ATYP_IPV4:
            host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
        elif address_type == ATYP_DOMAIN:
            length = (await reader.readexactly(1))[0]
            raw = await reader.readexactly(length)
            try:
                host = raw.decode("ascii")
            except UnicodeDecodeError:
                raise SocksProtocolError(REP_ADDRESS_NOT_SUPPORTED, "non-ascii domain")
        elif address_type == ATYP_IPV6:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        else:
            raise SocksProtocolError(REP_ADDRESS_NOT_SUPPORTED, f"address type {address_type}")
        port = int.from_bytes(await reader.readexactly(2), "big")

        if command != CMD_CONNECT:
            raise SocksProtocolError(REP_COMMAND_NOT_SUPPORTED, f"command {command}")
        if not host or port == 0:
            raise SocksProtocolError(REP_ADDRESS_NOT_SUPPORTED, "empty address")
        return host, port

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, reply: int) -> None:
        writer.write(bytes([SOCKS_VERSION, reply, 0x00, ATYP_IPV4]) + b"\x00\x00\x00\x00\x00\x00")
        await writer.drain()


def _reply_for(exc: BaseException) -> int:
    if isinstance(exc, asyncio.TimeoutError) or isinstance(exc, socket.gaierror):
        return REP_HOST_UNREACHABLE
    if isinstance(exc, ConnectionRefusedError):
        return REP_CONNECTION_REFUSED
    if isinstance(exc, OSError) and exc.errno in (101, 51):  # ENETUNREACH linux / bsd
        return REP_NETWORK_UNREACHABLE
    return REP_GENERAL_FAILURE


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    remote_reader: asyncio.StreamReader,
    remote_writer: asyncio.StreamWriter,
) -> None:
    upstream = asyncio.create_task(_pipe(client_reader, remote_writer))
    downstream = asyncio.create_task(_pipe(remote_reader, client_writer))
    pending = {upstream, downstream}
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            # Ошибка с одной стороны — обрыв; ждать второе направление нечего.
            # Чистый EOF — полузакрытие: ответ противоположной стороны ещё идёт.
            if any(task.exception() is not None for task in done):
                break
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(PIPE_CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    finally:
        if writer.can_write_eof():
            try:
                writer.write_eof()
            except (OSError, RuntimeError):
                pass


def _close(writer: Optional[asyncio.StreamWriter]) -> None:
    if writer is None:
        return
    try:
        writer.close()
    except Exception:  # noqa: BLE001 — транспорт уже мог умереть, закрывать нечего
        pass


# ------------------------------------------------------------------ выходы


async def connect_direct(host: str, port: int, bind_ip: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Соединение с привязкой исходящего адреса; домен резолвится только в записи семейства адреса."""
    family = socket.AF_INET6 if ":" in bind_ip else socket.AF_INET
    return await asyncio.open_connection(host, port, family=family, local_addr=(bind_ip, 0))


async def connect_via_socks(
    host: str, port: int, proxy_host: str, proxy_port: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Соединение через чужой SOCKS5 (WARP); домен уходит прокси как есть — DNS внутри туннеля."""
    reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
    try:
        writer.write(bytes([SOCKS_VERSION, 1, METHOD_NO_AUTH]))
        await writer.drain()
        version, method = await reader.readexactly(2)
        if version != SOCKS_VERSION or method != METHOD_NO_AUTH:
            raise SocksUpstreamError("upstream socks rejected no-auth")
        writer.write(_connect_request(host, port))
        await writer.drain()
        version, reply, _reserved, address_type = await reader.readexactly(4)
        if version != SOCKS_VERSION or reply != REP_SUCCESS:
            raise SocksUpstreamError(f"upstream socks reply {reply}")
        await _skip_bound_address(reader, address_type)
    except asyncio.IncompleteReadError as exc:
        _close(writer)
        raise SocksUpstreamError("upstream socks closed during handshake") from exc
    except BaseException:
        _close(writer)
        raise
    return reader, writer


def _connect_request(host: str, port: int) -> bytes:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        name = host.encode("idna")
        if len(name) > MAX_DOMAIN_LENGTH:
            raise SocksUpstreamError("domain name too long")
        address = bytes([ATYP_DOMAIN, len(name)]) + name
    else:
        address = bytes([ATYP_IPV4 if ip.version == 4 else ATYP_IPV6]) + ip.packed
    return bytes([SOCKS_VERSION, CMD_CONNECT, 0x00]) + address + port.to_bytes(2, "big")


async def _skip_bound_address(reader: asyncio.StreamReader, address_type: int) -> None:
    if address_type == ATYP_IPV4:
        await reader.readexactly(4 + 2)
    elif address_type == ATYP_DOMAIN:
        length = (await reader.readexactly(1))[0]
        await reader.readexactly(length + 2)
    elif address_type == ATYP_IPV6:
        await reader.readexactly(16 + 2)
    else:
        raise SocksUpstreamError(f"upstream socks address type {address_type}")
