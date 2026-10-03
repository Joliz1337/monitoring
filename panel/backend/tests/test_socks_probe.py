"""Алерт «сервер недоступен» называет виновником SOCKS5-прокси, только когда сбоит сам прокси.

Прокси поднимаются в тесте на localhost и ведут себя как мёртвый, зависший,
чужой или требовательный к паролю прокси.
"""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services import server_alerter
    from app.services.socks_probe import ProxyFault, find_proxy_fault
except ImportError as e:  # pragma: no cover
    raise unittest.SkipTest(f"alerter requires the panel runtime: {e}")

TIMEOUT = 0.5


async def _socks5(reader, writer, *, login=None, password=None):
    _, count = await reader.readexactly(2)
    offered = await reader.readexactly(count)
    wanted = 0x00 if login is None else 0x02
    if wanted not in offered:
        writer.write(b"\x05\xff")
        return
    writer.write(bytes([0x05, wanted]))
    if login is None:
        return
    await writer.drain()
    _, user_len = await reader.readexactly(2)
    user = await reader.readexactly(user_len)
    (secret_len,) = await reader.readexactly(1)
    secret = await reader.readexactly(secret_len)
    ok = user == login.encode() and secret == password.encode()
    writer.write(b"\x01\x00" if ok else b"\x01\x01")


async def _silent(reader, _writer):
    await reader.read()


async def _hang_up(_reader, _writer):
    pass


async def _http(reader, writer):
    await reader.read(3)
    writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")


class _Proxy:
    def __init__(self, behaviour):
        self._behaviour = behaviour
        self._server = None

    async def __aenter__(self) -> int:
        async def handle(reader, writer):
            try:
                await self._behaviour(reader, writer)
                await writer.drain()
            finally:
                writer.close()

        self._server = await asyncio.start_server(handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def __aexit__(self, *_exc):
        self._server.close()


async def _free_port() -> int:
    server = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


class FindProxyFaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_proxy_without_auth(self):
        async with _Proxy(_socks5) as port:
            self.assertIsNone(await find_proxy_fault(f"127.0.0.1:{port}", TIMEOUT))

    async def test_healthy_proxy_with_auth(self):
        behaviour = lambda r, w: _socks5(r, w, login="user", password="p@ss:word")  # noqa: E731
        async with _Proxy(behaviour) as port:
            self.assertIsNone(await find_proxy_fault(f"127.0.0.1:{port}@user:p@ss:word", TIMEOUT))

    async def test_closed_port_is_unreachable(self):
        port = await _free_port()
        self.assertEqual(await find_proxy_fault(f"127.0.0.1:{port}", TIMEOUT), ProxyFault.UNREACHABLE)

    async def test_proxy_that_never_answers_is_silent(self):
        async with _Proxy(_silent) as port:
            self.assertEqual(await find_proxy_fault(f"127.0.0.1:{port}", TIMEOUT), ProxyFault.SILENT)

    async def test_proxy_that_hangs_up_is_silent(self):
        async with _Proxy(_hang_up) as port:
            self.assertEqual(await find_proxy_fault(f"127.0.0.1:{port}", TIMEOUT), ProxyFault.SILENT)

    async def test_wrong_password_is_rejected(self):
        behaviour = lambda r, w: _socks5(r, w, login="user", password="right")  # noqa: E731
        async with _Proxy(behaviour) as port:
            fault = await find_proxy_fault(f"127.0.0.1:{port}@user:wrong", TIMEOUT)
        self.assertEqual(fault, ProxyFault.AUTH_REJECTED)

    async def test_missing_credentials_are_rejected(self):
        behaviour = lambda r, w: _socks5(r, w, login="user", password="right")  # noqa: E731
        async with _Proxy(behaviour) as port:
            self.assertEqual(await find_proxy_fault(f"127.0.0.1:{port}", TIMEOUT), ProxyFault.AUTH_REJECTED)

    async def test_http_server_is_not_socks5(self):
        async with _Proxy(_http) as port:
            self.assertEqual(await find_proxy_fault(f"127.0.0.1:{port}", TIMEOUT), ProxyFault.NOT_SOCKS5)


def _settings(language: str):
    return SimpleNamespace(language=language, alert_cooldown=1800)


def _server(proxy_url):
    return SimpleNamespace(id=1, name="tspu-node", url="https://203.0.113.1:9100", proxy_url=proxy_url)


class OfflineAlertTests(unittest.IsolatedAsyncioTestCase):
    async def _offline_alert(self, srv, settings, fault):
        alerter = server_alerter.ServerAlerter()
        sent = mock.AsyncMock()
        with (
            mock.patch.object(alerter, "_active_probe_sequence", mock.AsyncMock(return_value=(True, True))),
            mock.patch.object(server_alerter, "find_proxy_fault", mock.AsyncMock(return_value=fault)),
            mock.patch.object(alerter, "_send_and_save", sent),
        ):
            await alerter._check_offline(srv, server_alerter.ServerAlertState(), settings, False, 0.0)
        sent.assert_awaited_once()
        _srv, _settings_arg, alert_type, _severity, message, details = sent.await_args.args
        self.assertEqual(alert_type, "offline")
        return message, details

    async def test_dead_proxy_is_named_without_credentials(self):
        message, details = await self._offline_alert(
            _server("192.0.2.7:1080@user:secret"), _settings("ru"), ProxyFault.UNREACHABLE
        )
        self.assertIn("из-за прокси", message)
        self.assertIn("192.0.2.7:1080", message)
        self.assertNotIn("secret", message)
        self.assertEqual(details["proxy_fault"], "unreachable")

    async def test_healthy_proxy_keeps_node_message(self):
        message, details = await self._offline_alert(_server("192.0.2.7:1080"), _settings("en"), None)
        self.assertEqual(message, "Server tspu-node is offline (API unreachable, ICMP reachable)")
        self.assertNotIn("proxy_fault", details)

    async def test_server_without_proxy_skips_proxy_check(self):
        with mock.patch.object(server_alerter, "find_proxy_fault", mock.AsyncMock()) as check:
            alerter = server_alerter.ServerAlerter()
            with (
                mock.patch.object(alerter, "_active_probe_sequence", mock.AsyncMock(return_value=(True, False))),
                mock.patch.object(alerter, "_send_and_save", mock.AsyncMock()),
            ):
                await alerter._check_offline(_server(None), server_alerter.ServerAlertState(), _settings("en"), False, 0.0)
        check.assert_not_awaited()


class ApiProbeHardTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_hanging_proxy_does_not_freeze_probe(self):
        class HangingClient:
            async def get(self, *_args, **_kwargs):
                await asyncio.sleep(3600)

        with (
            mock.patch.object(server_alerter, "get_node_client", return_value=HangingClient()),
            mock.patch.object(server_alerter, "node_auth_headers", return_value={}),
            mock.patch.object(server_alerter, "API_PROBE_HARD_TIMEOUT", 0.05),
        ):
            result = await asyncio.wait_for(server_alerter.ServerAlerter._api_probe(_server("192.0.2.7:1080")), 2)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
