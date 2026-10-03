"""Tests for the address → panel server map used to label HAProxy rule targets.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.server_ip_owners import (  # noqa: E402
    IpOwner,
    ServerAddressSource,
    build_ip_owners,
)


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


def server(server_id: int, name: str, url: str, last_metrics: str | None = None) -> ServerAddressSource:
    return ServerAddressSource(id=server_id, name=name, url=url, last_metrics=last_metrics)


class BuildIpOwnersTest(unittest.TestCase):
    def test_url_ip_is_primary_and_other_ips_are_numbered_in_interface_order(self):
        owners = build_ip_owners([
            server(1, "DE", "https://5.9.0.11:9100", metrics_with(
                [ipv4("5.9.0.10"), ipv4("5.9.0.11")],
                [ipv4("95.216.0.7")],
            )),
        ])

        self.assertEqual(owners["5.9.0.11"], IpOwner(1, "DE"))
        self.assertEqual(owners["5.9.0.10"], IpOwner(1, "DE", 1))
        self.assertEqual(owners["95.216.0.7"], IpOwner(1, "DE", 2))

    def test_domain_url_is_primary_key_and_first_interface_ip_is_primary(self):
        owners = build_ip_owners([
            server(1, "FI", "https://Node.Example.com:9100", metrics_with(
                [ipv4("95.216.0.7"), ipv4("95.216.0.8")],
            )),
        ])

        self.assertEqual(owners["node.example.com"], IpOwner(1, "FI"))
        self.assertEqual(owners["95.216.0.7"], IpOwner(1, "FI"))
        self.assertEqual(owners["95.216.0.8"], IpOwner(1, "FI", 1))

    def test_url_ip_missing_on_interfaces_leaves_all_interface_ips_extra(self):
        owners = build_ip_owners([
            server(1, "NAT", "https://5.9.0.10:9100", metrics_with([ipv4("95.216.0.7"), ipv4("95.216.0.8")])),
        ])

        self.assertEqual(owners["5.9.0.10"], IpOwner(1, "NAT"))
        self.assertEqual(owners["95.216.0.7"], IpOwner(1, "NAT", 1))
        self.assertEqual(owners["95.216.0.8"], IpOwner(1, "NAT", 2))

    def test_ip_on_two_interfaces_gets_one_number(self):
        owners = build_ip_owners([
            server(1, "DE", "https://5.9.0.10:9100", metrics_with(
                [ipv4("5.9.0.10"), ipv4("5.9.0.11")],
                [ipv4("5.9.0.11"), ipv4("5.9.0.12")],
            )),
        ])

        self.assertEqual(owners["5.9.0.11"], IpOwner(1, "DE", 1))
        self.assertEqual(owners["5.9.0.12"], IpOwner(1, "DE", 2))

    def test_private_and_service_interface_ips_are_skipped(self):
        owners = build_ip_owners([
            server(1, "DE", "https://node.example.com", metrics_with([
                ipv4("127.0.0.1"), ipv4("172.17.0.1"), ipv4("10.0.0.1"),
                ipv4("192.168.1.5"), ipv4("100.64.0.1"),
                {"type": "ipv6", "address": "2001:db8::1"},
            ])),
        ])

        self.assertEqual(set(owners), {"node.example.com"})

    def test_private_url_host_is_kept(self):
        owners = build_ip_owners([server(1, "LAN", "https://10.0.0.5:9100")])

        self.assertEqual(owners["10.0.0.5"], IpOwner(1, "LAN"))

    def test_url_host_wins_over_interface_of_other_server(self):
        owners = build_ip_owners([
            server(1, "old", "https://old.example.com", metrics_with([ipv4("5.9.0.10")])),
            server(2, "new", "https://5.9.0.10:9100"),
        ])

        self.assertEqual(owners["5.9.0.10"], IpOwner(2, "new"))

    def test_first_server_keeps_shared_interface_ip(self):
        owners = build_ip_owners([
            server(1, "first", "https://a.example.com", metrics_with([ipv4("5.9.0.10")])),
            server(2, "second", "https://b.example.com", metrics_with([ipv4("5.9.0.10")])),
        ])

        self.assertEqual(owners["5.9.0.10"], IpOwner(1, "first"))

    def test_broken_or_missing_metrics_leave_only_url_host(self):
        owners = build_ip_owners([
            server(1, "a", "https://a.example.com", "{not json"),
            server(2, "b", "https://b.example.com", json.dumps(["unexpected"])),
            server(3, "c", "https://c.example.com", json.dumps({"network": None})),
            server(4, "d", "https://d.example.com"),
        ])

        self.assertEqual(set(owners), {"a.example.com", "b.example.com", "c.example.com", "d.example.com"})

    def test_malformed_url_is_skipped(self):
        owners = build_ip_owners([server(1, "bad", "https://[::1:9100")])

        self.assertEqual(owners, {})


if __name__ == "__main__":
    unittest.main()
