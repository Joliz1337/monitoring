"""Тесты определения внешнего IP панели.

Свойство, ради которого тест существует: в whitelist нод уходит адрес, с
которого панель реально к ним ходит. Домен за прокси Cloudflare резолвится в
чужой IP, адрес интерфейса внутри docker-bridge приватный — оба источника
годятся только как запасные, и порядок опроса с фильтром публичности решает,
попадёт ли в whitelist правильный адрес.
"""

import os
import socket
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402

from app.services import net_utils  # noqa: E402
from app.services.net_utils import PanelIpSource, fetch_ip_from_services  # noqa: E402

PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "151.101.1.69"
DOCKER_BRIDGE_IP = "172.18.0.5"


def reset_cache() -> None:
    net_utils._cache.result = None
    net_utils._cache.expires_at = 0.0


def patch_detectors(external=None, interface=None, dns=None):
    return (
        patch.object(net_utils, "_ip_from_external_services", AsyncMock(return_value=external)),
        patch.object(net_utils, "_ip_from_interface", AsyncMock(return_value=interface)),
        patch.object(net_utils, "_ip_from_domain", AsyncMock(return_value=dns)),
    )


class DetectionOrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_cache()

    async def test_external_service_wins(self):
        external, interface, dns = patch_detectors(
            external=PUBLIC_IP, interface=OTHER_PUBLIC_IP, dns=OTHER_PUBLIC_IP
        )
        with external, interface as interface_mock, dns as dns_mock:
            found = await net_utils.panel_ip_info()
        self.assertEqual(found.ip, PUBLIC_IP)
        self.assertEqual(found.source, PanelIpSource.EXTERNAL)
        interface_mock.assert_not_awaited()
        dns_mock.assert_not_awaited()

    async def test_interface_when_services_unreachable(self):
        external, interface, dns = patch_detectors(interface=PUBLIC_IP, dns=OTHER_PUBLIC_IP)
        with external, interface, dns:
            found = await net_utils.panel_ip_info()
        self.assertEqual(found.ip, PUBLIC_IP)
        self.assertEqual(found.source, PanelIpSource.INTERFACE)

    async def test_domain_is_last_resort(self):
        external, interface, dns = patch_detectors(dns=PUBLIC_IP)
        with external, interface, dns:
            found = await net_utils.panel_ip_info()
        self.assertEqual(found.ip, PUBLIC_IP)
        self.assertEqual(found.source, PanelIpSource.DNS)

    async def test_nothing_found(self):
        external, interface, dns = patch_detectors()
        with external, interface, dns:
            self.assertIsNone(await net_utils.panel_ip_info())
            self.assertIsNone(await net_utils.resolve_panel_ip())

    async def test_resolve_panel_ip_returns_plain_address(self):
        external, interface, dns = patch_detectors(external=PUBLIC_IP)
        with external, interface, dns:
            self.assertEqual(await net_utils.resolve_panel_ip(), PUBLIC_IP)


class CacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_cache()

    async def test_success_is_cached(self):
        external, interface, dns = patch_detectors(external=PUBLIC_IP)
        with external as external_mock, interface, dns:
            await net_utils.panel_ip_info()
            external_mock.return_value = OTHER_PUBLIC_IP
            found = await net_utils.panel_ip_info()
        self.assertEqual(found.ip, PUBLIC_IP)
        self.assertEqual(external_mock.await_count, 1)

    async def test_failure_is_not_retried_immediately(self):
        external, interface, dns = patch_detectors()
        with external as external_mock, interface, dns:
            await net_utils.panel_ip_info()
            external_mock.return_value = PUBLIC_IP
            self.assertIsNone(await net_utils.panel_ip_info())
        self.assertEqual(external_mock.await_count, 1)

    async def test_failure_expires_sooner_than_success(self):
        self.assertLess(net_utils.PANEL_IP_RETRY_TTL, net_utils.PANEL_IP_CACHE_TTL)
        external, interface, dns = patch_detectors()
        with external, interface, dns:
            await net_utils.panel_ip_info()
        retry_deadline = net_utils._cache.expires_at
        reset_cache()
        external, interface, dns = patch_detectors(external=PUBLIC_IP)
        with external, interface, dns:
            await net_utils.panel_ip_info()
        self.assertGreater(net_utils._cache.expires_at, retry_deadline)


class FakeUdpSocket:
    def __init__(self, local_ip: str):
        self.local_ip = local_ip

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, address):
        self.connected_to = address

    def getsockname(self):
        return (self.local_ip, 0)


def fake_socket_module(factory):
    return SimpleNamespace(AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM, socket=factory)


class InterfaceProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_interface_address(self):
        fake = fake_socket_module(lambda *args: FakeUdpSocket(PUBLIC_IP))
        with patch.object(net_utils, "socket", fake):
            self.assertEqual(await net_utils._ip_from_interface(), PUBLIC_IP)

    async def test_docker_bridge_address_is_rejected(self):
        """Внутри docker-bridge адрес интерфейса — 172.18.x.x, в whitelist он бесполезен."""
        fake = fake_socket_module(lambda *args: FakeUdpSocket(DOCKER_BRIDGE_IP))
        with patch.object(net_utils, "socket", fake):
            self.assertIsNone(await net_utils._ip_from_interface())

    async def test_no_route_gives_none(self):
        def raise_no_route(*args):
            raise OSError("no route")

        with patch.object(net_utils, "socket", fake_socket_module(raise_no_route)):
            self.assertIsNone(await net_utils._ip_from_interface())


class ParsePublicIpv4Tests(unittest.TestCase):
    def test_trims_whitespace(self):
        self.assertEqual(net_utils._parse_public_ipv4(f"  {PUBLIC_IP}\n"), PUBLIC_IP)

    def test_rejects_ipv6(self):
        self.assertIsNone(net_utils._parse_public_ipv4("2a00:1450:4001::1"))

    def test_rejects_private(self):
        self.assertIsNone(net_utils._parse_public_ipv4("10.0.0.1"))
        self.assertIsNone(net_utils._parse_public_ipv4(DOCKER_BRIDGE_IP))

    def test_rejects_garbage(self):
        self.assertIsNone(net_utils._parse_public_ipv4("<html>rate limited</html>"))
        self.assertIsNone(net_utils._parse_public_ipv4(""))


class ExternalServicesTests(unittest.IsolatedAsyncioTestCase):
    async def fetch_with(self, responses: dict[str, httpx.Response]) -> tuple[str | None, list[str]]:
        asked: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(str(request.url))
            return responses.get(str(request.url), httpx.Response(500))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_ip_from_services(client), asked

    async def test_first_healthy_service_answers(self):
        first, second = net_utils.PUBLIC_IP_SERVICES[:2]
        ip, asked = await self.fetch_with({first: httpx.Response(200, text=f"{PUBLIC_IP}\n")})
        self.assertEqual(ip, PUBLIC_IP)
        self.assertEqual(asked, [first])
        self.assertNotIn(second, asked)

    async def test_skips_errors_and_junk_until_valid(self):
        first, second, third = net_utils.PUBLIC_IP_SERVICES[:3]
        ip, asked = await self.fetch_with({
            first: httpx.Response(429, text="Too Many Requests"),
            second: httpx.Response(200, text="2a00:1450:4001::1"),
            third: httpx.Response(200, text=PUBLIC_IP),
        })
        self.assertEqual(ip, PUBLIC_IP)
        self.assertEqual(asked, [first, second, third])

    async def test_all_services_down(self):
        ip, asked = await self.fetch_with({})
        self.assertIsNone(ip)
        self.assertEqual(len(asked), len(net_utils.PUBLIC_IP_SERVICES))


if __name__ == "__main__":
    unittest.main()
