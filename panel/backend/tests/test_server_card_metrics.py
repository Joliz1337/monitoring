"""Срез ответа ноды для списка серверов (services/server_card_metrics).

Голый unittest, без PostgreSQL и без сети.

Список серверов отдавал полный ответ каждой ноды: ~15 КБ на сервер, 30 МБ JSON на
2000 нод при каждом опросе дашборда. Карточкам из него нужны два десятка чисел.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import copy
import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.server_card_metrics import card_metrics  # noqa: E402

SNAPSHOT = SimpleNamespace(net_rx_bytes_per_sec=800.0, net_tx_bytes_per_sec=200.0)


def interface(name: str, addresses: list[str], virtual: bool = False) -> dict:
    return {
        "name": name,
        "addresses": [
            {"type": "ipv6" if ":" in ip else "ipv4", "address": ip, "netmask": "255.255.255.0", "public_address": None}
            for ip in addresses
        ],
        "mac": "52:54:00:12:34:56", "mtu": 1500, "speed_mbps": 1000, "is_up": True, "is_virtual": virtual,
        "rx_bytes": 10_000_000_000, "tx_bytes": 20_000_000_000, "rx_packets": 11_000_000, "tx_packets": 14_000_000,
        "rx_errors": 0, "tx_errors": 0, "rx_drops": 3, "tx_drops": 0,
        "rx_bytes_per_sec": 1_500_000.0, "tx_bytes_per_sec": 2_500_000.0,
    }


def process(pid: int) -> dict:
    return {"pid": pid, "name": "xray", "cpu_percent": 12.5, "memory_percent": 3.25, "status": "sleeping"}


def node_answer() -> dict:
    """Ответ агента с посекундными скоростями, по форме node/app/models/metrics.py."""
    return {
        "timestamp": "2026-11-12T11:40:00.123456+00:00",
        "server_name": "nl-1",
        "timezone": {"name": "UTC", "offset": "+00:00", "offset_seconds": 0},
        "cpu": {
            "cores_physical": 2, "cores_logical": 4, "model": "AMD EPYC 7763 64-Core Processor",
            "usage_percent": 37.5, "per_cpu_percent": [30.0, 45.0, 35.0, 40.0], "steal_percent": 0.4,
            "load_avg_1": 1.25, "load_avg_5": 1.1, "load_avg_15": 0.9,
            "frequency": {"current": 2450.0, "min": 1500.0, "max": 3500.0}, "temperatures": {},
        },
        "memory": {
            "ram": {"total": 8_340_000_000, "used": 3_100_000_000, "free": 500_000_000,
                    "available": 4_900_000_000, "percent": 41.2, "buffers": 120_000_000, "cached": 3_900_000_000},
            "swap": {"total": 2_147_479_552, "used": 10_485_760, "free": 2_136_993_792, "percent": 0.5},
            "numa_nodes": [],
        },
        "disk": {
            "partitions": [
                {"device": "/dev/sda1", "mountpoint": "/", "fstype": "ext4",
                 "total": 80_000_000_000, "used": 21_000_000_000, "free": 59_000_000_000, "percent": 26.3},
                {"device": "/dev/sdb1", "mountpoint": "/mnt/data", "fstype": "ext4",
                 "total": 500_000_000_000, "used": 400_000_000_000, "free": 100_000_000_000, "percent": 80.0},
            ],
            "io": {"sda": {"read_bytes": 10, "write_bytes": 30, "read_count": 1, "write_count": 2,
                           "read_time_ms": 5, "write_time_ms": 7,
                           "read_bytes_per_sec": 1.0, "write_bytes_per_sec": 2.0}},
            "io_total": {"read_bytes_per_sec": 1.0, "write_bytes_per_sec": 2.0},
        },
        "network": {
            "interfaces": [
                interface("lo", ["127.0.0.1"]),
                interface("eth0", ["203.0.113.7", "fe80::5054:ff:fe12:3456"]),
                interface("docker0", ["172.17.0.1"], virtual=True),
                interface("veth1a2b3c4", [], virtual=True),
            ],
            "total": {"rx_bytes": 10_000_000_000, "tx_bytes": 20_000_000_000,
                      "rx_packets": 11_000_000, "tx_packets": 14_000_000,
                      "rx_bytes_per_sec": 1_500_000.0, "tx_bytes_per_sec": 2_500_000.0},
            "ports": [{"port": 443, "rx_bytes": 1, "tx_bytes": 2}], "ports_available": True,
            "ports_sampled_at": 1.0, "ips": [], "ips_available": False, "ips_sampled_at": None,
        },
        "processes": {"total": 180, "running": 2, "sleeping": 178,
                      "top_by_cpu": [process(pid) for pid in range(10)],
                      "top_by_memory": [process(pid) for pid in range(10, 20)]},
        "system": {
            "hostname": "nl-1.example.net", "os": "Ubuntu 24.04.1 LTS", "kernel": "6.8.0-45-generic",
            "architecture": "x86_64", "boot_time": "2026-11-01T00:00:00+00:00", "uptime_seconds": 1_000_000,
            "uptime_human": "11d 13h", "open_files": 4200,
            "connections": {"established": 900, "listen": 20, "time_wait": 50, "other": 3},
            "connections_detailed": {"tcp": {"total": 973, "established": 900}, "udp": {"total": 40}},
            "ephemeral_ports": {"range_low": 1024, "range_high": 65535, "reserved": 0, "capacity": 64511,
                                "fast_capacity": 32000, "tw_reuse": 1, "sources": []},
            "server_name": "nl-1", "timezone": None, "boot_id": "b00t",
        },
        "certificates": {"count": 2, "closest_expiry": {
            "domain": "example.com", "days_left": 42, "expiry_date": "2026-12-24", "expired": False}},
        "antiddos": {"mode": "off", "source": "none", "since": 0, "watchdog": "on"},
        "live_rates": {"window_sec": 1.0, "sampled_at": 1.0},
        "window": {"window_sec": 10.0, "samples": 10, "cpu_avg": 33.0, "cpu_max": 61.0,
                   "per_cpu_avg": [31.0, 34.0, 33.0, 34.0], "steal_avg": 0.3,
                   "net_rx_avg": 1_400_000.0, "net_tx_avg": 2_300_000.0,
                   "net_rx_max": 2_000_000.0, "net_tx_max": 3_000_000.0,
                   "disk_read_avg": 1.0, "disk_write_avg": 2.0},
        "agent_version": "10.35.1",
        "capabilities": None,
        "loss_probe": [],
    }


def old_agent_answer() -> dict:
    """Агент без посекундных скоростей: их панель берёт из своего последнего снапшота."""
    answer = node_answer()
    for block in ("live_rates", "window"):
        del answer[block]
    answer["network"]["total"].update(rx_bytes_per_sec=0.0, tx_bytes_per_sec=0.0)
    return answer


class CardMetricsTest(unittest.TestCase):
    def test_slice_is_exactly_what_the_cards_read(self):
        self.assertEqual(card_metrics(node_answer()), {
            "cpu": {"usage_percent": 37.5, "per_cpu_percent": [30.0, 45.0, 35.0, 40.0], "cores_logical": 4,
                    "load_avg_1": 1.25, "load_avg_5": 1.1, "load_avg_15": 0.9},
            "memory": {"ram": {"percent": 41.2, "used": 3_100_000_000, "total": 8_340_000_000}},
            "disk": {"partitions": [{"percent": 26.3, "used": 21_000_000_000, "total": 80_000_000_000}]},
            "network": {
                "total": {"rx_bytes_per_sec": 1_500_000.0, "tx_bytes_per_sec": 2_500_000.0},
                "interfaces": [{"addresses": [{"address": "203.0.113.7"}]}],
            },
            "system": {"hostname": "nl-1.example.net", "os": "Ubuntu 24.04.1 LTS", "uptime_seconds": 1_000_000},
            "certificates": {"closest_expiry": {"domain": "example.com", "days_left": 42, "expired": False}},
            "window": {"cpu_avg": 33.0, "per_cpu_avg": [31.0, 34.0, 33.0, 34.0],
                       "net_rx_avg": 1_400_000.0, "net_tx_avg": 2_300_000.0},
        })

    def test_slice_is_many_times_smaller_than_the_answer(self):
        full = len(json.dumps(node_answer()))
        sliced = len(json.dumps(card_metrics(node_answer())))

        self.assertLess(sliced, 1024)
        self.assertLess(sliced * 5, full)

    def test_only_addresses_worth_searching_by_are_sent(self):
        # По IP ищут сервер на дашборде. Петля и link-local есть у каждой ноды, адреса
        # docker и veth повторяются на многих — по ним поиск находил бы весь парк
        answer = node_answer()
        answer["network"]["interfaces"] = [
            interface("lo", ["127.0.0.1", "::1"]),
            interface("eth0", ["203.0.113.7", "169.254.169.254", "FE80::1", "2001:db8::7"]),
            interface("eth1", ["10.0.0.7"]),
            interface("docker0", ["172.17.0.1"], virtual=True),
        ]

        interfaces = card_metrics(answer)["network"]["interfaces"]

        self.assertEqual(interfaces, [
            {"addresses": [{"address": "203.0.113.7"}, {"address": "2001:db8::7"}]},
            {"addresses": [{"address": "10.0.0.7"}]},
        ])

    def test_old_agent_gets_the_total_speed_from_the_snapshot(self):
        total = card_metrics(old_agent_answer(), SNAPSHOT)["network"]["total"]

        self.assertEqual(total, {"rx_bytes_per_sec": 800.0, "tx_bytes_per_sec": 200.0})

    def test_node_that_measures_its_own_speed_keeps_it(self):
        total = card_metrics(node_answer(), SNAPSHOT)["network"]["total"]

        self.assertEqual(total, {"rx_bytes_per_sec": 1_500_000.0, "tx_bytes_per_sec": 2_500_000.0})

    def test_old_agent_without_a_snapshot_keeps_what_it_sent(self):
        total = card_metrics(old_agent_answer())["network"]["total"]

        self.assertEqual(total, {"rx_bytes_per_sec": 0.0, "tx_bytes_per_sec": 0.0})

    def test_shared_answer_stays_untouched(self):
        # Ответ в памяти один на всех читателей — алерты, белые списки, веса HAProxy
        answer = old_agent_answer()
        before = copy.deepcopy(answer)

        card_metrics(answer, SNAPSHOT)

        self.assertEqual(answer, before)

    def test_node_without_certificates_and_window_has_no_such_blocks(self):
        answer = old_agent_answer()
        answer["certificates"] = None

        card = card_metrics(answer)

        self.assertNotIn("certificates", card)
        self.assertNotIn("window", card)

    def test_certificates_without_an_expiry_are_left_out(self):
        answer = node_answer()
        answer["certificates"] = {"count": 0, "closest_expiry": None}

        self.assertNotIn("certificates", card_metrics(answer))

    def test_answer_cut_short_still_gives_every_section_the_card_opens(self):
        card = card_metrics({"cpu": {"usage_percent": 5.0}, "network": {"total": None}})

        self.assertEqual(card, {
            "cpu": {"usage_percent": 5.0},
            "memory": {"ram": {}},
            "disk": {"partitions": []},
            "network": {"total": None, "interfaces": []},
            "system": {},
        })


if __name__ == "__main__":
    unittest.main()
