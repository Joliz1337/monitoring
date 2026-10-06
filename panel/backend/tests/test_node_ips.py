"""Tests for node addresses collected into the blocklist and anti-DDoS whitelists.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests
"""

import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.node_ips import collect_node_ips, public_interface_ipv4  # noqa: E402


def metrics_with(*interfaces: list[dict]) -> str:
    return json.dumps({
        "network": {
            "interfaces": [
                {"name": f"eth{i}", "addresses": addresses}
                for i, addresses in enumerate(interfaces)
            ],
        },
    })


def ipv4(address: str) -> dict:
    return {"type": "ipv4", "address": address, "netmask": "255.255.255.0"}


class PublicInterfaceIpv4Test(unittest.TestCase):
    def test_public_addresses_of_all_interfaces_in_order_without_repeats(self):
        metrics = metrics_with(
            [ipv4("5.9.0.10"), ipv4("5.9.0.11")],
            [ipv4("5.9.0.11"), ipv4("95.216.0.7")],
        )

        self.assertEqual(public_interface_ipv4(metrics), ["5.9.0.10", "5.9.0.11", "95.216.0.7"])

    def test_private_service_and_ipv6_addresses_are_skipped(self):
        metrics = metrics_with([
            ipv4("127.0.0.1"), ipv4("172.17.0.1"), ipv4("10.0.0.5"), ipv4("192.168.1.5"),
            ipv4("100.64.0.1"), ipv4("169.254.1.1"), {"type": "ipv6", "address": "2001:db8::1"},
        ])

        self.assertEqual(public_interface_ipv4(metrics), [])

    def test_broken_metrics_give_nothing(self):
        for raw in (None, "", "{not json", json.dumps(["x"]), json.dumps({"network": None}),
                    json.dumps({"network": {"interfaces": ["eth0", {"addresses": ["5.9.0.10"]}]}})):
            with self.subTest(raw=raw):
                self.assertEqual(public_interface_ipv4(raw), [])


class CollectNodeIpsTest(unittest.IsolatedAsyncioTestCase):
    async def test_url_host_and_every_public_interface_address(self):
        ips = await collect_node_ips([
            ("https://5.9.0.10:9100", metrics_with([ipv4("5.9.0.10"), ipv4("5.9.0.11")], [ipv4("95.216.0.7")])),
            ("https://10.0.0.5:9100", metrics_with([ipv4("10.0.0.5")])),
            ("https://1.2.3.4:9100", None),
        ])

        self.assertEqual(ips, {"5.9.0.10", "5.9.0.11", "95.216.0.7", "10.0.0.5", "1.2.3.4"})

    async def test_floating_ip_in_url_with_only_private_address_on_interface(self):
        ips = await collect_node_ips([
            ("https://89.208.0.10:9100", metrics_with([ipv4("10.0.1.15")], [ipv4("172.17.0.1")])),
        ])

        self.assertEqual(ips, {"89.208.0.10"})

    async def test_domain_is_resolved_and_unresolved_one_is_dropped(self):
        resolve = AsyncMock(side_effect=lambda host: {"node.example.com": "95.216.0.7"}.get(host))
        with patch("app.services.node_ips.host_to_ip", resolve):
            ips = await collect_node_ips([
                ("https://node.example.com:9100", metrics_with([ipv4("95.216.0.8")])),
                ("https://gone.example.com:9100", None),
            ])

        self.assertEqual(ips, {"95.216.0.7", "95.216.0.8"})

    async def test_malformed_url_keeps_interface_addresses(self):
        ips = await collect_node_ips([("https://[::1:9100", metrics_with([ipv4("5.9.0.10")]))])

        self.assertEqual(ips, {"5.9.0.10"})


if __name__ == "__main__":
    unittest.main()
