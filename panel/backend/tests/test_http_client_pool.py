"""Тесты клиентов панель → нода (app/services/http_client.py).

Голый unittest, без сети: проверяется, какой клиент достаётся какой ноде.

Один пул httpx на весь парк не переиспользует соединения дальше сотни нод
(каждый опрос — новое mTLS-рукопожатие) и тратит миллисекунды процессора на
запрос на перебор самого пула. Поэтому у каждой ноды свой клиент.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import ssl
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import http_client  # noqa: E402
from app.services.pki import PKIKeygenData, generate_ca, generate_client_cert  # noqa: E402

POLL_INTERVAL_SECONDS = 10


def node(url: str, *, mtls: bool = True, proxy: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(url=url, pki_enabled=mtls, proxy_url=proxy, api_key="legacy-key")


def make_keygen() -> PKIKeygenData:
    ca_cert, ca_key = generate_ca()
    client_cert, client_key = generate_client_cert(ca_cert, ca_key)
    return PKIKeygenData(
        ca_cert=ca_cert, ca_key=ca_key,
        client_cert=client_cert, client_key=client_key,
        shared_node_cert="", shared_node_key="",
    )


class NodeClientTestCase(unittest.IsolatedAsyncioTestCase):
    keygen: PKIKeygenData

    @classmethod
    def setUpClass(cls) -> None:
        cls.keygen = make_keygen()

    async def asyncSetUp(self) -> None:
        await http_client.init_http_clients(self.keygen)

    async def asyncTearDown(self) -> None:
        await http_client.close_http_clients()


class NodeClientIsolationTest(NodeClientTestCase):
    def test_two_nodes_do_not_share_a_client(self):
        first = http_client.get_node_client(node("https://10.0.0.1:9100"))
        second = http_client.get_node_client(node("https://10.0.0.2:9100"))
        self.assertIsNot(first, second)

    def test_same_node_gets_the_same_client_on_every_poll(self):
        first = http_client.get_node_client(node("https://10.0.0.1:9100"))
        again = http_client.get_node_client(node("https://10.0.0.1:9100"))
        self.assertIs(first, again)

    def test_port_distinguishes_nodes_on_one_address(self):
        first = http_client.get_node_client(node("https://10.0.0.1:9100"))
        second = http_client.get_node_client(node("https://10.0.0.1:9200"))
        self.assertIsNot(first, second)

    def test_trailing_path_does_not_split_one_node_into_two_clients(self):
        first = http_client.get_node_client(node("https://10.0.0.1:9100"))
        second = http_client.get_node_client(node("https://10.0.0.1:9100/"))
        self.assertIs(first, second)

    def test_mtls_and_legacy_auth_on_one_address_use_different_clients(self):
        mtls = http_client.get_node_client(node("https://10.0.0.1:9100", mtls=True))
        legacy = http_client.get_node_client(node("https://10.0.0.1:9100", mtls=False))
        self.assertIsNot(mtls, legacy)

    def test_long_apply_requests_do_not_use_the_polling_client(self):
        server = node("https://10.0.0.1:9100")
        polling = http_client.get_node_client(server)
        apply = http_client.get_node_apply_client(server)
        self.assertIsNot(polling, apply)
        self.assertEqual(apply.timeout.read, 300.0)
        self.assertEqual(polling.timeout.read, 10.0)

    def test_nodes_behind_one_proxy_share_the_proxy_client(self):
        first = http_client.get_node_client(node("https://10.0.0.1:9100", proxy="192.0.2.10:1080"))
        second = http_client.get_node_client(node("https://10.0.0.2:9100", proxy="192.0.2.10:1080"))
        self.assertIs(first, second)


class NodeClientKeepAliveTest(NodeClientTestCase):
    def test_connection_survives_until_the_next_poll(self):
        client = http_client.get_node_client(node("https://10.0.0.1:9100"))
        pool = client._transport._pool
        self.assertGreaterEqual(pool._max_keepalive_connections, 1)
        self.assertGreater(pool._keepalive_expiry, POLL_INTERVAL_SECONDS)

    def test_keepalive_expires_before_the_node_closes_the_connection(self):
        # nginx ноды держит keepalive_timeout 65 с: закрывать нужно раньше неё,
        # иначе запрос уходит в соединение, которое нода уже оборвала
        client = http_client.get_node_client(node("https://10.0.0.1:9100"))
        self.assertLess(client._transport._pool._keepalive_expiry, 65)


class NodeClientLifecycleTest(NodeClientTestCase):
    async def test_client_of_a_forgotten_node_is_closed(self):
        clock = mock.Mock(return_value=1000.0)
        with mock.patch.object(http_client, "_monotonic", clock):
            abandoned = http_client.get_node_client(node("https://10.0.0.1:9100"))
            clock.return_value += http_client.NODE_CLIENT_IDLE_TTL + http_client.PRUNE_INTERVAL + 1
            http_client.get_node_client(node("https://10.0.0.2:9100"))
            await asyncio.sleep(0.05)

            self.assertTrue(abandoned.is_closed)
            fresh = http_client.get_node_client(node("https://10.0.0.1:9100"))
            self.assertIsNot(fresh, abandoned)
            self.assertFalse(fresh.is_closed)

    async def test_client_in_regular_use_is_never_closed(self):
        clock = mock.Mock(return_value=1000.0)
        with mock.patch.object(http_client, "_monotonic", clock):
            server = node("https://10.0.0.1:9100")
            client = http_client.get_node_client(server)
            for _ in range(200):
                clock.return_value += POLL_INTERVAL_SECONDS
                self.assertIs(http_client.get_node_client(server), client)
            await asyncio.sleep(0.05)
            self.assertFalse(client.is_closed)

    async def test_shutdown_closes_every_node_client(self):
        clients = [
            http_client.get_node_client(node("https://10.0.0.1:9100")),
            http_client.get_node_client(node("https://10.0.0.2:9100", mtls=False)),
            http_client.get_node_apply_client(node("https://10.0.0.3:9100")),
        ]
        await http_client.close_http_clients()
        self.assertTrue(all(client.is_closed for client in clients))


class MetricsSessionTest(NodeClientTestCase):
    """Опрос метрик идёт мимо клиентов нод — одной сессией aiohttp на весь парк."""

    def test_fleet_shares_one_session_without_a_total_connection_limit(self):
        session = http_client.get_metrics_session()
        self.assertIs(http_client.get_metrics_session(), session)
        self.assertEqual(session.connector.limit, 0)
        self.assertEqual(session.connector.limit_per_host, 2)

    def test_connection_survives_until_the_next_poll_and_expires_before_the_node_closes_it(self):
        keepalive = http_client.get_metrics_session().connector._keepalive_timeout
        self.assertGreater(keepalive, POLL_INTERVAL_SECONDS)
        self.assertLess(keepalive, 65)

    def test_connection_gets_the_tcp_and_tls_time_of_the_node_client(self):
        # У httpx TCP и TLS-рукопожатие ждут по connect каждый, у aiohttp срок на оба один
        timeout = http_client.get_metrics_session().timeout
        polling = http_client.get_node_client(node("https://10.0.0.1:9100")).timeout
        self.assertEqual(timeout.connect, 2 * polling.connect)
        self.assertEqual(timeout.connect, 4.0)

    def test_tls_handshake_is_not_squeezed_into_the_tcp_connect_time(self):
        # sock_connect у aiohttp ограничивает TCP вместе с рукопожатием
        self.assertIsNone(http_client.get_metrics_session().timeout.sock_connect)

    def test_node_may_stay_silent_between_chunks_as_long_as_for_the_node_client(self):
        timeout = http_client.get_metrics_session().timeout
        polling = http_client.get_node_client(node("https://10.0.0.1:9100")).timeout
        self.assertEqual(timeout.sock_read, polling.read)
        self.assertEqual(timeout.sock_read, 10.0)
        self.assertIsNone(timeout.total)

    def test_node_gets_the_tls_context_of_its_auth(self):
        mtls = http_client.node_ssl_context(node("https://10.0.0.1:9100", mtls=True))
        legacy = http_client.node_ssl_context(node("https://10.0.0.1:9100", mtls=False))
        self.assertEqual(mtls.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(legacy.verify_mode, ssl.CERT_NONE)

    async def test_shutdown_closes_the_session(self):
        session = http_client.get_metrics_session()
        await http_client.close_http_clients()
        self.assertTrue(session.closed)
        with self.assertRaises(RuntimeError):
            http_client.get_metrics_session()

    async def test_pki_reload_replaces_the_session_and_the_tls_context(self):
        # Восстановление бэкапа: close + init с ключами из восстановленной базы
        server = node("https://10.0.0.1:9100")
        old_session = http_client.get_metrics_session()
        old_context = http_client.node_ssl_context(server)

        await http_client.close_http_clients()
        await http_client.init_http_clients(make_keygen())

        self.assertTrue(old_session.closed)
        self.assertFalse(http_client.get_metrics_session().closed)
        self.assertIsNot(http_client.node_ssl_context(server), old_context)


if __name__ == "__main__":
    unittest.main()
