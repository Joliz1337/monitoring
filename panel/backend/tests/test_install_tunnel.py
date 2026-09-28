"""Прокси панели в SSH-туннеле для установки ноды за ТСПУ.

Сервер за туннелем — не доверенная сторона для внутренней сети панели: наружу
пускаем только на 80/443 и только на публичные адреса. Живые тесты поднимают
asyncssh-сервер в процессе и проверяют, что байты реально ходят через
remote-listener, а отказ sshd в пробросе превращается в понятную ошибку.
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncssh  # noqa: E402

from app.services import install_tunnel  # noqa: E402
from app.services.install_tunnel import (  # noqa: E402
    ProxyRequestError,
    TunnelForwardingDenied,
    TunnelStats,
    is_allowed_address,
    open_install_tunnel,
    parse_proxy_request,
    resolve_allowed_target,
)


def head(*lines: str) -> bytes:
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


class ParseProxyRequestTests(unittest.TestCase):
    def test_connect_target(self):
        request = parse_proxy_request(head("CONNECT github.com:443 HTTP/1.1", "Host: github.com:443"))
        self.assertTrue(request.is_connect)
        self.assertEqual((request.host, request.port), ("github.com", 443))

    def test_connect_ipv6_target(self):
        request = parse_proxy_request(head("CONNECT [2606:4700::1111]:443 HTTP/1.1"))
        self.assertEqual((request.host, request.port), ("2606:4700::1111", 443))

    def test_absolute_form_is_rewritten_to_origin_form(self):
        request = parse_proxy_request(head(
            "GET http://archive.ubuntu.com/ubuntu/dists/noble/InRelease?x=1 HTTP/1.1",
            "Host: archive.ubuntu.com",
            "Proxy-Connection: keep-alive",
            "Connection: keep-alive",
            "User-Agent: Debian APT-HTTP/1.3",
        ))
        self.assertFalse(request.is_connect)
        self.assertEqual((request.host, request.port), ("archive.ubuntu.com", 80))
        self.assertEqual(request.upstream_head, head(
            "GET /ubuntu/dists/noble/InRelease?x=1 HTTP/1.1",
            "Host: archive.ubuntu.com",
            "User-Agent: Debian APT-HTTP/1.3",
            "Connection: close",
        ))

    def test_missing_host_header_is_added(self):
        request = parse_proxy_request(head("GET http://example.com:8080 HTTP/1.1"))
        self.assertEqual(request.port, 8080)
        self.assertIn(b"GET / HTTP/1.1\r\nHost: example.com:8080\r\n", request.upstream_head)

    def test_malformed_requests_are_rejected(self):
        for raw in (
            head("GET https://github.com/ HTTP/1.1"),
            head("GET /relative HTTP/1.1"),
            head("CONNECT github.com HTTP/1.1"),
            head("CONNECT github.com:443 SSH-2.0"),
            head("garbage"),
        ):
            with self.subTest(raw=raw), self.assertRaises(ProxyRequestError) as ctx:
                parse_proxy_request(raw)
            self.assertEqual(ctx.exception.status, 400)


class _ChunkReader:
    def __init__(self, *chunks: bytes):
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class ReadHeadTests(unittest.TestCase):
    def read(self, *chunks: bytes):
        return asyncio.run(install_tunnel._read_head(_ChunkReader(*chunks)))

    def test_bytes_after_header_are_kept_for_upstream(self):
        head_bytes, rest = self.read(b"CONNECT a.b:443 HTTP/1.1\r\n", b"\r\n\x16\x03\x01")
        self.assertEqual(head_bytes, b"CONNECT a.b:443 HTTP/1.1\r\n\r\n")
        self.assertEqual(rest, b"\x16\x03\x01")

    def test_oversized_header_is_rejected(self):
        huge = b"GET http://a.b/ HTTP/1.1\r\nX: " + b"a" * install_tunnel.MAX_HEADER_BYTES + b"\r\n\r\n"
        with self.assertRaises(ProxyRequestError):
            self.read(huge)

    def test_connection_closed_before_header_end(self):
        with self.assertRaises(ProxyRequestError):
            self.read(b"CONNECT a.b:443 HTTP/1.1\r\n")


class TargetPolicyTests(unittest.TestCase):
    def test_public_addresses_are_allowed(self):
        for address in ("140.82.121.4", "2606:4700::1111"):
            with self.subTest(address=address):
                self.assertTrue(is_allowed_address(address))

    def test_internal_addresses_are_blocked(self):
        for address in (
            "127.0.0.1", "10.0.0.5", "172.17.0.1", "192.168.1.1",
            "100.64.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1",
            "::1", "fd00::1", "fe80::1%eth0", "::ffff:10.0.0.1", "not-an-ip",
        ):
            with self.subTest(address=address):
                self.assertFalse(is_allowed_address(address))

    def resolve(self, host: str, port: int, addresses: list[str]) -> list[str]:
        async def resolver(_host, _port):
            return addresses
        return asyncio.run(resolve_allowed_target(host, port, resolver))

    def test_only_web_ports_are_allowed(self):
        with self.assertRaises(ProxyRequestError) as ctx:
            self.resolve("github.com", 22, ["140.82.121.4"])
        self.assertEqual(ctx.exception.status, 403)

    def test_any_internal_address_blocks_the_name(self):
        # Имя, резолвящееся хоть в один внутренний адрес, не пускаем целиком —
        # иначе выбор адреса для подключения стал бы лотереей
        with self.assertRaises(ProxyRequestError) as ctx:
            self.resolve("evil.example", 443, ["140.82.121.4", "10.0.0.5"])
        self.assertEqual(ctx.exception.status, 403)

    def test_unresolvable_name_is_bad_gateway(self):
        with self.assertRaises(ProxyRequestError) as ctx:
            self.resolve("nowhere.example", 443, [])
        self.assertEqual(ctx.exception.status, 502)

    def test_resolved_addresses_are_returned_for_connect(self):
        self.assertEqual(self.resolve("github.com", 443, ["140.82.121.4"]), ["140.82.121.4"])


class TunnelStatsTests(unittest.TestCase):
    def test_problems_are_reported_once_and_drained(self):
        stats = TunnelStats()
        stats.problem("github.com:22", "порт закрыт")
        stats.problem("github.com:22", "порт закрыт")
        self.assertEqual(stats.drain_notes(), ["[panel] туннель: github.com:22 — порт закрыт"])
        self.assertEqual(stats.drain_notes(), [])

    def test_summary_in_megabytes(self):
        stats = TunnelStats(connections=3, bytes_transferred=5 * 1024 * 1024)
        self.assertEqual(stats.summary(), "[panel] Через панель передано 5.0 МБ, соединений: 3")


class _SshServer(asyncssh.SSHServer):
    allow_forwarding = True

    def begin_auth(self, username: str) -> bool:
        return False

    def server_requested(self, listen_host: str, listen_port: int) -> bool:
        return self.allow_forwarding


class _DenyingSshServer(_SshServer):
    allow_forwarding = False


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(1024):
        writer.write(data)
        await writer.drain()
    writer.close()


class LiveTunnelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")

    async def start_ssh(self, server_class) -> int:
        acceptor = await asyncssh.create_server(
            server_class, "127.0.0.1", 0, server_host_keys=[self.host_key]
        )
        self.addAsyncCleanup(acceptor.wait_closed)
        self.addCleanup(acceptor.close)
        return acceptor.sockets[0].getsockname()[1]

    async def connect(self, port: int) -> asyncssh.SSHClientConnection:
        conn = await asyncssh.connect(
            "127.0.0.1", port, username="installer", known_hosts=None,
            client_keys=None, agent_path=None, password=None,
        )
        self.addCleanup(conn.close)
        return conn

    async def ask_proxy(self, proxy_port: int, request: bytes) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(request)
        await writer.drain()
        status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        return reader, writer, status

    async def test_connect_passes_bytes_both_ways(self):
        upstream = await asyncio.start_server(_echo, "127.0.0.1", 0)
        self.addCleanup(upstream.close)
        upstream_port = upstream.sockets[0].getsockname()[1]
        conn = await self.connect(await self.start_ssh(_SshServer))

        # Проверки цели снимаем: эхо-сервер живёт на loopback и не на 80/443
        with mock.patch.object(install_tunnel, "ALLOWED_PORTS", {upstream_port}), \
                mock.patch.object(install_tunnel, "is_allowed_address", lambda _ip: True):
            async with open_install_tunnel(conn) as tunnel:
                self.assertEqual(tunnel.proxy_url, f"http://127.0.0.1:{tunnel.port}")
                reader, writer, status = await self.ask_proxy(
                    tunnel.port, f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\n\r\n".encode()
                )
                self.assertTrue(status.startswith(b"HTTP/1.1 200"))
                writer.write(b"ping-through-panel")
                await writer.drain()
                echoed = await asyncio.wait_for(reader.readexactly(18), timeout=10)
                writer.close()

        self.assertEqual(echoed, b"ping-through-panel")
        self.assertEqual(tunnel.stats.connections, 1)
        self.assertGreaterEqual(tunnel.stats.bytes_transferred, 36)

    async def test_internal_target_gets_forbidden(self):
        conn = await self.connect(await self.start_ssh(_SshServer))
        async with open_install_tunnel(conn) as tunnel:
            _reader, writer, status = await self.ask_proxy(
                tunnel.port, b"CONNECT 10.0.0.5:443 HTTP/1.1\r\n\r\n"
            )
            writer.close()

        self.assertTrue(status.startswith(b"HTTP/1.1 403"))
        self.assertEqual(tunnel.stats.connections, 0)
        self.assertEqual(
            tunnel.stats.drain_notes(),
            ["[panel] туннель: 10.0.0.5:443 — непубличный адрес 10.0.0.5"],
        )

    async def test_forwarding_denied_by_sshd(self):
        conn = await self.connect(await self.start_ssh(_DenyingSshServer))
        with self.assertRaises(TunnelForwardingDenied):
            async with open_install_tunnel(conn):
                pass


if __name__ == "__main__":
    unittest.main()
