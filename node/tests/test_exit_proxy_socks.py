"""Exit-прокси: SOCKS5-сервер против локального echo-сервера.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"

Проверяется сам протокол (рукопожатие, CONNECT по IPv4 и домену, отказ UDP,
отсутствие выхода), цепочка через чужой socks (клиентская часть для WARP) и
главное — сброс соединений только «чужого» выхода при переключении.
"""

import asyncio
import os
import socket
import sys
import unittest
from functools import partial

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.exit_proxy.socks_server import (  # noqa: E402
    REP_COMMAND_NOT_SUPPORTED,
    REP_GENERAL_FAILURE,
    REP_SUCCESS,
    SocksServer,
    connect_direct,
    connect_via_socks,
)

LOOPBACK = "127.0.0.1"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


async def start_echo() -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(handle, LOOPBACK, 0)


def server_port(server: asyncio.AbstractServer) -> int:
    return server.sockets[0].getsockname()[1]


async def socks_connect(port: int, host: str, target_port: int, command: int = 1) -> tuple[int, asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection(LOOPBACK, port)
    writer.write(bytes([5, 1, 0]))
    await writer.drain()
    assert await reader.readexactly(2) == bytes([5, 0])
    try:
        packed = socket.inet_aton(host)
        address = bytes([1]) + packed
    except OSError:
        name = host.encode()
        address = bytes([3, len(name)]) + name
    writer.write(bytes([5, command, 0]) + address + target_port.to_bytes(2, "big"))
    await writer.drain()
    reply = await reader.readexactly(10)
    return reply[1], reader, writer


class SocksServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.echo = await start_echo()
        self.echo_port = server_port(self.echo)
        self.exit_id = "ip:127.0.0.1"
        self.route_value = (self.exit_id, partial(connect_direct, bind_ip=LOOPBACK))
        self.server = SocksServer(free_port(), lambda: self.route_value)
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        self.echo.close()
        await self.echo.wait_closed()

    async def test_connect_by_ipv4_relays_data(self):
        reply, reader, writer = await socks_connect(self.server.port, LOOPBACK, self.echo_port)
        self.assertEqual(reply, REP_SUCCESS)
        writer.write(b"ping")
        await writer.drain()
        self.assertEqual(await reader.readexactly(4), b"ping")
        self.assertEqual(self.server.active_connections, 1)
        writer.close()
        await writer.wait_closed()

    async def test_connect_by_domain_resolves_only_ipv4(self):
        reply, reader, writer = await socks_connect(self.server.port, "localhost", self.echo_port)
        self.assertEqual(reply, REP_SUCCESS)
        writer.write(b"x" * 70000)
        await writer.drain()
        self.assertEqual(len(await reader.readexactly(70000)), 70000)
        writer.close()

    async def test_udp_associate_is_rejected(self):
        reply, _, writer = await socks_connect(self.server.port, LOOPBACK, self.echo_port, command=3)
        self.assertEqual(reply, REP_COMMAND_NOT_SUPPORTED)
        writer.close()

    async def test_no_route_fails_connection(self):
        self.route_value = None
        reply, _, writer = await socks_connect(self.server.port, LOOPBACK, self.echo_port)
        self.assertEqual(reply, REP_GENERAL_FAILURE)
        self.assertEqual(self.server.failed_connections, 1)
        writer.close()

    async def test_refused_target_reports_failure_without_counting_success(self):
        reply, _, writer = await socks_connect(self.server.port, LOOPBACK, free_port())
        self.assertNotEqual(reply, REP_SUCCESS)
        self.assertEqual(self.server.total_connections, 0)
        writer.close()

    async def test_auth_required_client_is_refused(self):
        reader, writer = await asyncio.open_connection(LOOPBACK, self.server.port)
        writer.write(bytes([5, 1, 2]))
        await writer.drain()
        self.assertEqual(await reader.readexactly(2), bytes([5, 0xFF]))
        writer.close()

    async def test_drop_connections_spares_the_new_exit(self):
        _, old_reader, old_writer = await socks_connect(self.server.port, LOOPBACK, self.echo_port)
        self.route_value = ("ip:new", partial(connect_direct, bind_ip=LOOPBACK))
        _, new_reader, new_writer = await socks_connect(self.server.port, LOOPBACK, self.echo_port)
        self.assertEqual(self.server.active_connections, 2)

        self.assertEqual(self.server.drop_connections(except_exit="ip:new"), 1)
        self.assertEqual(await old_reader.read(), b"")
        self.assertEqual(self.server.active_connections, 1)

        new_writer.write(b"still here")
        await new_writer.drain()
        self.assertEqual(await new_reader.readexactly(10), b"still here")
        old_writer.close()
        new_writer.close()

    async def test_stop_drops_everything(self):
        _, reader, writer = await socks_connect(self.server.port, LOOPBACK, self.echo_port)
        await self.server.stop()
        self.assertEqual(await reader.read(), b"")
        self.assertFalse(self.server.listening)
        writer.close()


class SocksChainTest(unittest.IsolatedAsyncioTestCase):
    """Клиентская часть (как к WARP) проверяется на собственном сервере в роли upstream."""

    async def asyncSetUp(self):
        self.echo = await start_echo()
        self.upstream = SocksServer(free_port(), lambda: ("direct", partial(connect_direct, bind_ip=LOOPBACK)))
        await self.upstream.start()
        chained = partial(connect_via_socks, proxy_host=LOOPBACK, proxy_port=self.upstream.port)
        self.front = SocksServer(free_port(), lambda: ("warp", chained))
        await self.front.start()

    async def asyncTearDown(self):
        await self.front.stop()
        await self.upstream.stop()
        self.echo.close()
        await self.echo.wait_closed()

    async def test_domain_travels_through_upstream_socks(self):
        reply, reader, writer = await socks_connect(self.front.port, "localhost", server_port(self.echo))
        self.assertEqual(reply, REP_SUCCESS)
        writer.write(b"chain")
        await writer.drain()
        self.assertEqual(await reader.readexactly(5), b"chain")
        self.assertEqual(self.upstream.total_connections, 1)
        writer.close()

    async def test_upstream_refusal_maps_to_socks_error(self):
        reply, _, writer = await socks_connect(self.front.port, LOOPBACK, free_port())
        self.assertNotEqual(reply, REP_SUCCESS)
        self.assertEqual(self.front.failed_connections, 1)
        writer.close()


if __name__ == "__main__":
    unittest.main()
