"""Tests for the client-like probe of a backend address (services/client_probe).

Runnable with plain stdlib:  python -m unittest discover -s node/tests

По ответу на эту проверку панель решает, работает ли сервер для клиентов.
Ложное «молчит» — отключённый рабочий сервер, ложное «отвечает» — клиенты на
сервере, от которого не приходит ни байта.
"""

import asyncio
import os
import socket
import sys
import unittest
from contextlib import suppress
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.client_probe import ProxyHeader, client_hello, probe, proxy_header  # noqa: E402

TLS_HANDSHAKE_RECORD = 0x16
CLIENT_HELLO = 0x01
QUICK = 0.3


class ProxyHeaderTests(unittest.TestCase):
    def test_v1_names_both_ends_of_the_probe_connection(self):
        header = proxy_header(ProxyHeader.V1, ("10.0.0.2", 51000), ("10.0.0.5", 8443))
        self.assertEqual(header, b"PROXY TCP4 10.0.0.2 10.0.0.5 51000 8443\r\n")

    def test_v1_over_ipv6(self):
        header = proxy_header(ProxyHeader.V1, ("2001:db8::2", 51000), ("2001:db8::5", 8443))
        self.assertEqual(header, b"PROXY TCP6 2001:db8::2 2001:db8::5 51000 8443\r\n")

    def test_v2_is_the_binary_header(self):
        header = proxy_header(ProxyHeader.V2, ("10.0.0.2", 51000), ("10.0.0.5", 8443))
        self.assertEqual(header[:12], b"\r\n\r\n\x00\r\nQUIT\n")
        # Версия 2, команда PROXY; TCP поверх IPv4; длина адресного блока 12
        self.assertEqual(header[12:16], b"\x21\x11\x00\x0c")
        self.assertEqual(header[16:], socket.inet_aton("10.0.0.2") + socket.inet_aton("10.0.0.5")
                         + (51000).to_bytes(2, "big") + (8443).to_bytes(2, "big"))

    def test_v2_over_ipv6(self):
        header = proxy_header(ProxyHeader.V2, ("2001:db8::2", 51000), ("2001:db8::5", 8443))
        self.assertEqual(header[12:16], b"\x21\x21\x00\x24")
        self.assertEqual(len(header), 16 + 36)


class ClientHelloTests(unittest.TestCase):
    def test_it_is_a_tls_client_hello(self):
        hello = client_hello()
        self.assertEqual((hello[0], hello[5]), (TLS_HANDSHAKE_RECORD, CLIENT_HELLO))
        self.assertEqual(int.from_bytes(hello[3:5], "big"), len(hello) - 5)

    def test_every_probe_gets_its_own_hello(self):
        self.assertNotEqual(client_hello(), client_hello())


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    """Настоящие сокеты на loopback: что считать ответом сервера."""

    async def serve(self, handler) -> int:
        async def closing(reader, writer):
            # Без закрытия соединения остановка тестового сервера ждала бы его вечно
            try:
                await handler(reader, writer)
            finally:
                writer.close()

        server = await asyncio.start_server(closing, "127.0.0.1", 0)
        self.addAsyncCleanup(self.close, server)
        return server.sockets[0].getsockname()[1]

    @staticmethod
    async def close(server) -> None:
        server.close()
        await server.wait_closed()

    async def test_server_that_replies_answers(self):
        async def reply(reader, writer):
            await reader.read(1)
            writer.write(b"\x16\x03\x03\x00\x01\x02")
            await writer.drain()

        self.assertIs(await probe("127.0.0.1", await self.serve(reply)), True)

    async def test_any_reply_counts_even_a_refusal_of_the_handshake(self):
        # Алерт TLS от сайта-заглушки — тоже ответ: данные по пути ходят в обе стороны
        async def alert(reader, writer):
            await reader.read(1)
            writer.write(b"\x15\x03\x03\x00\x02\x02\x28")
            await writer.drain()
            writer.close()

        self.assertIs(await probe("127.0.0.1", await self.serve(alert)), True)

    async def test_server_that_takes_the_connection_and_says_nothing_is_silent(self):
        async def silent(reader, writer):
            # Читает до обрыва и молчит: так выглядит сервер, чьи ответы съедены на пути
            with suppress(OSError):
                await reader.read()
            writer.close()

        self.assertIs(await probe("127.0.0.1", await self.serve(silent), timeout=QUICK), False)

    async def test_server_that_hangs_up_without_a_byte_is_silent(self):
        async def hang_up(reader, writer):
            await reader.read(1)
            writer.close()

        self.assertIs(await probe("127.0.0.1", await self.serve(hang_up), timeout=QUICK), False)

    async def test_probe_leaves_from_the_given_address(self):
        # Выходной адрес из правила HAProxy: сервер видит проверку с него, а не с адреса по умолчанию
        peers = []

        async def reply(reader, writer):
            peers.append(writer.get_extra_info("peername")[0])
            await reader.read(1)
            writer.write(b"\x16\x03\x03\x00\x01\x02")
            await writer.drain()

        self.assertIs(await probe("127.0.0.1", await self.serve(reply), local_ip="127.0.0.2"), True)
        self.assertEqual(peers, ["127.0.0.2"])

    async def test_exit_address_missing_on_the_host_is_not_judged(self):
        async def reply(reader, writer):
            await reader.read(1)
            writer.write(b"\x16\x03\x03\x00\x01\x02")
            await writer.drain()

        self.assertIsNone(await probe("127.0.0.1", await self.serve(reply), local_ip="192.0.2.77"))

    async def test_address_that_cannot_be_reached_is_not_judged(self):
        # Не подключиться — это потери, их считает другой замер
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.assertIsNone(await probe("127.0.0.1", port, timeout=5.0))

    async def test_connection_lost_before_the_header_is_silence_not_a_crash(self):
        # Сервер оборвал соединение сразу после подключения: адресов для заголовка уже нет
        class GoneWriter:
            transport = mock.Mock()

            def get_extra_info(self, name: str):
                return None

            def write(self, data: bytes) -> None:
                raise AssertionError("nothing to write to")

        async def connected(ip: str, port: int, local_addr=None):
            return asyncio.StreamReader(), GoneWriter()

        with mock.patch("app.services.client_probe.asyncio.open_connection", connected):
            self.assertIs(await probe("10.0.0.5", 443, ProxyHeader.V1), False)

    async def test_server_gets_a_real_client_hello(self):
        got = asyncio.get_running_loop().create_future()

        async def capture(reader, writer):
            got.set_result(await reader.read(4096))
            writer.write(b"\x00")
            await writer.drain()

        await probe("127.0.0.1", await self.serve(capture))
        hello = await got
        self.assertEqual((hello[0], hello[5]), (TLS_HANDSHAKE_RECORD, CLIENT_HELLO))

    async def test_proxy_header_goes_before_the_hello(self):
        got = asyncio.get_running_loop().create_future()

        async def capture(reader, writer):
            line = await reader.readline()
            got.set_result((line, await reader.read(1)))
            writer.write(b"\x00")
            await writer.drain()

        port = await self.serve(capture)
        self.assertIs(await probe("127.0.0.1", port, ProxyHeader.V1), True)
        line, first_byte = await got
        self.assertRegex(line.decode(), rf"^PROXY TCP4 127\.0\.0\.1 127\.0\.0\.1 \d+ {port}\r\n$")
        self.assertEqual(first_byte[0], TLS_HANDSHAKE_RECORD)


if __name__ == "__main__":
    unittest.main()
