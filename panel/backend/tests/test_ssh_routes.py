"""Пути SSH-подключения: основной и запасной через SSH-прокси.

У сервера может быть отдельный прокси только для SSH. В запасном режиме панель
идёт основным путём и берёт прокси, только если тот не подключился. Смена пути —
ответ на сетевую ошибку: неверный пароль и чужой ключ хоста вторым путём не
повторяются, иначе на сервере копились бы неудачные входы, а пароль уходил бы
подменённому серверу. Путь, которым подключились, доставка запоминает до конца
задачи — переподключения идут сразу им.
"""

import asyncio
import os
import socket
import sys
import unittest
from unittest import mock

import asyncssh

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ssh_fakes import LiveSshCase, Socks5Proxy, public_text  # noqa: E402

from app.services import deploy_service, node_image_delivery  # noqa: E402
from app.services.ssh_target import (  # noqa: E402
    HostKeyChangedError,
    SshRoutesFailed,
    SSHTarget,
    connect_error_text,
    open_ssh_routes,
    prefer_route,
    route_label,
    ssh_connect_kwargs,
)


def dead_port() -> int:
    """Порт на 127.0.0.1, который никто не слушает."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class OpenRoutesTests(LiveSshCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.dead = dead_port()

    async def proxy(self, redirect_to: int | None = None) -> tuple[Socks5Proxy, str]:
        proxy = Socks5Proxy(redirect_to=redirect_to)
        port = await proxy.start()
        self.addAsyncCleanup(proxy.stop)
        return proxy, f"127.0.0.1:{port}"

    def kwargs(self, port: int, **fields) -> dict:
        return ssh_connect_kwargs(self.target(port=port, **fields))

    async def close(self, conn: asyncssh.SSHClientConnection) -> None:
        conn.close()
        await conn.wait_closed()

    async def test_single_route_error_passes_through_unchanged(self):
        with self.assertRaises(OSError) as caught:
            await open_ssh_routes(self.kwargs(self.dead), (None,))

        self.assertNotIsInstance(caught.exception, SshRoutesFailed)

    async def test_dead_direct_route_falls_over_to_proxy(self):
        proxy, address = await self.proxy(redirect_to=self.port)

        opened = await open_ssh_routes(self.kwargs(self.dead), (None, address))
        await self.close(opened.conn)

        self.assertEqual(opened.route, address)
        self.assertEqual([route for route, _reason in opened.skipped], [None])
        self.assertTrue(opened.skipped[0][1])
        self.assertEqual(proxy.destinations, [("127.0.0.1", self.dead)])

    async def test_working_first_route_leaves_the_proxy_alone(self):
        proxy, address = await self.proxy()

        opened = await open_ssh_routes(self.kwargs(self.port), (None, address))
        await self.close(opened.conn)

        self.assertEqual((opened.route, opened.skipped), (None, []))
        self.assertEqual(proxy.destinations, [])

    async def test_wrong_password_is_not_retried_over_proxy(self):
        proxy, address = await self.proxy()

        with self.assertRaises(asyncssh.PermissionDenied):
            await open_ssh_routes(self.kwargs(self.port, password="wrong"), (None, address))

        self.assertEqual(self.seen.count("password"), 1)
        self.assertEqual(proxy.destinations, [])

    async def test_changed_host_key_is_not_retried_over_proxy(self):
        proxy, address = await self.proxy()
        stranger = public_text(asyncssh.generate_private_key("ssh-ed25519"))

        with self.assertRaises(HostKeyChangedError):
            await open_ssh_routes(self.kwargs(self.port, host_key=stranger), (None, address))

        self.assertNotIn("auth", self.seen)
        self.assertEqual(proxy.destinations, [])

    async def test_dead_proxy_is_named_in_the_error(self):
        with self.assertRaises(OSError) as caught:
            await open_ssh_routes(self.kwargs(self.port), (f"127.0.0.1:{self.dead}",))

        self.assertIn(f"SOCKS5-прокси 127.0.0.1:{self.dead}", str(caught.exception))

    async def test_all_routes_failed_names_each_without_proxy_password(self):
        routes = (None, f"127.0.0.1:{dead_port()}@login:proxy-secret")

        with self.assertRaises(SshRoutesFailed) as caught:
            await open_ssh_routes(self.kwargs(self.dead), routes)

        text = str(caught.exception)
        self.assertIn("напрямую: ", text)
        self.assertIn("; через SOCKS5 127.0.0.1:", text)
        self.assertNotIn("proxy-secret", text)

    async def test_first_connection_over_a_route_pins_the_host_key(self):
        opened = await open_ssh_routes(self.kwargs(self.port), (None,), 7)
        await self.close(opened.conn)

        self.remember.assert_awaited_once_with(7, public_text(self.host_key))

    async def test_install_through_the_panel_falls_over_to_the_ssh_proxy(self):
        _proxy, address = await self.proxy(redirect_to=self.port)
        target = SSHTarget(host="127.0.0.1", port=self.dead, password="pw", routes=(None, address))

        events = [event async for event in deploy_service.install_via_panel(target, lambda proxy: "install")]

        self.assertEqual(events[-1], {"type": "done", "exit_code": 0})
        self.assertTrue(any(f"установлено через SOCKS5 {address}" in event.get("line", "") for event in events), events)

    async def test_delivery_remembers_the_route_that_worked(self):
        _proxy, address = await self.proxy(redirect_to=self.port)
        target = SSHTarget(host="127.0.0.1", port=self.dead, password="pw", routes=(None, address))

        with mock.patch.object(node_image_delivery, "SSH_CONNECT_RETRY_DELAY", 0):
            items = [item async for item in node_image_delivery._connect(target, ssh_connect_kwargs(target))]
        await self.close(items[-1])

        lines = [item["line"] for item in items[:-1]]
        self.assertEqual(target.routes, (address, None))
        self.assertTrue(lines[0].startswith("[panel] SSH напрямую не подключился: "), lines)
        self.assertEqual(lines[1], f"[panel] SSH к 127.0.0.1:{self.dead} установлен через SOCKS5 {address}")


class RouteTextTests(unittest.TestCase):
    def test_prefer_route_puts_the_working_one_first(self):
        self.assertEqual(prefer_route((None, "p:1"), "p:1"), ("p:1", None))
        self.assertEqual(prefer_route((None, "p:1"), None), (None, "p:1"))

    def test_route_label_hides_proxy_credentials(self):
        self.assertEqual(route_label(None), "напрямую")
        self.assertEqual(route_label("1.2.3.4:1080@login:secret"), "через SOCKS5 1.2.3.4:1080")

    def test_timeout_text_names_the_address_and_the_real_timeout(self):
        kwargs = {"host": "10.0.0.5", "port": 2222, "connect_timeout": 15}

        self.assertEqual(connect_error_text(kwargs, asyncio.TimeoutError()), "10.0.0.5:2222 не ответил за 15 с")

    def test_error_without_text_names_its_type(self):
        kwargs = {"host": "10.0.0.5", "port": 22, "connect_timeout": 30}

        self.assertEqual(connect_error_text(kwargs, asyncssh.ConnectionLost("")), "ConnectionLost")


if __name__ == "__main__":
    unittest.main()
