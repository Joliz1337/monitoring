"""Тесты учёта эфемерных портов (services/ephemeral_ports).

Запуск без psutil и без Linux:  python -m unittest discover -s tests -p "test_*.py"
(из каталога node/).

Hex-адреса в дампах — ровно те, что печатает Linux: каждое 32-битное слово в
порядке байтов хоста, то есть на little-endian байты адреса идут задом наперёд
(127.0.0.1 → "0100007F"). Тесты держат их константами, чтобы разбор проверялся
против реального формата, а не против собственного кодировщика.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ephemeral_ports import (  # noqa: E402
    EphemeralPortsAggregator,
    KernelPortSettings,
    count_reserved_fast,
    count_reserved_in_range,
    decode_address,
    parse_port_range,
    parse_reserved_ports,
    read_kernel_settings,
    scan_listening_ports,
)

# 203.0.113.10 и 203.0.113.11 — адреса ноды; 142.250.185.78 — Google, 1.1.1.1 — второе направление
NODE_A = "0A7100CB"
NODE_B = "0B7100CB"
GOOGLE = "4EB9FA8E"
CLOUDFLARE = "01010101"
CLIENT = "0101A8C0"  # 192.168.1.1
MAPPED = "0000000000000000FFFF0000"
V6_NODE = "B80D0120000000000000000001000000"  # 2001:db8::1
V6_PEER = "B80D0120000000000000000002000000"  # 2001:db8::2

TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def tcp_row(local: str, remote: str, state: str) -> str:
    return f"   0: {local} {remote} {state} 00000000:00000000 00:00000000 00000000     0        0 1234 1 0000 20 0 0 10 -1\n"


# Слушатель на 0.0.0.0:443 плюс входящее соединение на него — входящее в учёт не идёт
TCP_DUMP = TCP_HEADER + "".join([
    tcp_row(f"00000000:01BB", "00000000:0000", "0A"),
    tcp_row(f"{NODE_A}:01BB", f"{CLIENT}:C350", "01"),
    tcp_row(f"{NODE_A}:9C40", f"{GOOGLE}:01BB", "01"),
    tcp_row(f"{NODE_A}:9C41", f"{GOOGLE}:01BB", "01"),
    tcp_row(f"{NODE_A}:9C42", f"{GOOGLE}:01BB", "06"),
    tcp_row(f"{NODE_A}:9C43", f"{CLOUDFLARE}:01BB", "01"),
    tcp_row(f"{NODE_B}:EA60", f"{GOOGLE}:01BB", "01"),
    tcp_row(f"{NODE_A}:0016", f"{CLIENT}:C350", "01"),
    tcp_row(f"{NODE_A}:9C50", "00000000:0000", "07"),
])

TCP6_DUMP = TCP_HEADER + "".join([
    tcp_row(f"{MAPPED}{NODE_A}:9C60", f"{MAPPED}{GOOGLE}:01BB", "01"),
    tcp_row(f"{V6_NODE}:9C61", f"{V6_PEER}:01BB", "01"),
])

SETTINGS = KernelPortSettings(low=1024, high=65535, reserved_in_range=12, reserved_fast=6, tw_reuse=1)
CAPACITY = 65535 - 1024 + 1 - 12
# Первый проход connect() — чётные порты 1024..65534
FAST_CAPACITY = 32256 - 6


def summarize(dumps, settings=SETTINGS):
    listening = set()
    for dump in dumps:
        listening |= scan_listening_ports(dump)
    aggregator = EphemeralPortsAggregator(settings, listening)
    for dump in dumps:
        for line in dump.strip().split("\n")[1:]:
            parts = line.split()
            aggregator.add(parts[1], parts[2], parts[3].upper())
    return aggregator.summarize()


def source_by_ip(summary: dict, ip: str) -> dict:
    return next(item for item in summary["sources"] if item["ip"] == ip)


class AddressDecodingTests(unittest.TestCase):
    def test_ipv4_word_is_host_byte_order(self):
        self.assertEqual(decode_address("0100007F"), "127.0.0.1")
        self.assertEqual(decode_address(NODE_A), "203.0.113.10")
        self.assertEqual(decode_address(GOOGLE), "142.250.185.78")

    def test_ipv6(self):
        self.assertEqual(decode_address(V6_NODE), "2001:db8::1")

    def test_garbage_rejected(self):
        with self.assertRaises(ValueError):
            decode_address("00FF")


class KernelSettingsTests(unittest.TestCase):
    def test_port_range_parsed(self):
        self.assertEqual(parse_port_range("1024\t65535\n"), (1024, 65535))

    def test_broken_range_rejected(self):
        self.assertIsNone(parse_port_range("65535 1024"))
        self.assertIsNone(parse_port_range("32768"))
        self.assertIsNone(parse_port_range("abc def"))

    def test_reserved_ports_counted_only_inside_range(self):
        # 7500 и 7501-7564 внутри, 80 ниже пола, 70000 — мусор
        reserved = parse_reserved_ports("80,7500,7501-7564,70000")
        self.assertEqual(count_reserved_in_range(reserved, 1024, 65535), 1 + 64)

    def test_reserved_range_clipped_by_window(self):
        self.assertEqual(count_reserved_in_range(parse_reserved_ports("1000-1030"), 1024, 65535), 7)

    def test_fast_reserved_counts_only_first_pass_parity(self):
        # Чётные 7500..7564 — 33 порта; 2223 нечётный, в первый проход не входит
        reserved = parse_reserved_ports("2223,7500,7501-7564")
        self.assertEqual(count_reserved_fast(reserved, 1024, 65535), 33)

    def test_fast_reserved_follows_parity_of_range_floor(self):
        # Нижняя граница нечётная — первым проходом идут нечётные порты
        self.assertEqual(count_reserved_fast(parse_reserved_ports("32769-32772"), 32769, 60999), 2)

    def test_read_from_proc(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysctl = Path(tmp) / "sys/net/ipv4"
            sysctl.mkdir(parents=True)
            (sysctl / "ip_local_port_range").write_text("1024\t65535\n")
            (sysctl / "ip_local_reserved_ports").write_text("7500,7501-7564\n")
            (sysctl / "tcp_tw_reuse").write_text("1\n")
            settings = read_kernel_settings(Path(tmp))
        self.assertEqual((settings.low, settings.high), (1024, 65535))
        self.assertEqual(settings.reserved_in_range, 65)
        self.assertEqual(settings.reserved_fast, 33)
        self.assertEqual(settings.fast_capacity, 32256 - 33)
        self.assertEqual(settings.capacity, 65535 - 1024 + 1 - 65)
        self.assertEqual(settings.tw_reuse, 1)

    def test_missing_proc_falls_back_to_kernel_defaults(self):
        settings = read_kernel_settings(Path(tempfile.gettempdir()) / "absent-proc-root")
        self.assertEqual((settings.low, settings.high), (32768, 60999))
        self.assertEqual(settings.reserved_in_range, 0)
        self.assertEqual(settings.tw_reuse, 2)


class ListeningPortsTests(unittest.TestCase):
    def test_listen_rows_collected(self):
        self.assertEqual(scan_listening_ports(TCP_DUMP), {"01BB"})

    def test_established_rows_ignored(self):
        self.assertEqual(scan_listening_ports(TCP6_DUMP), set())


class AggregationTests(unittest.TestCase):
    def test_outbound_counted_per_source(self):
        summary = summarize([TCP_DUMP])
        node_a = source_by_ip(summary, "203.0.113.10")
        self.assertEqual(node_a["used"], 3)
        self.assertEqual(node_a["time_wait"], 1)

    def test_inbound_on_listening_port_not_counted(self):
        # Строка NODE_A:01BB ← CLIENT — входящее на слушателя, порт не эфемерный
        summary = summarize([TCP_DUMP])
        node_a = source_by_ip(summary, "203.0.113.10")
        destinations = {(d["ip"], d["port"]) for d in node_a["destinations"]}
        self.assertNotIn(("192.168.1.1", 50000), destinations)

    def test_port_below_range_floor_not_counted(self):
        summary = summarize([TCP_DUMP])
        node_a = source_by_ip(summary, "203.0.113.10")
        self.assertEqual(node_a["used"] + node_a["time_wait"], 4)

    def test_unconnected_socket_not_counted(self):
        summary = summarize([TCP_DUMP])
        node_a = source_by_ip(summary, "203.0.113.10")
        self.assertEqual(node_a["destinations_total"], 2)

    def test_destination_is_the_real_ceiling(self):
        summary = summarize([TCP_DUMP])
        node_a = source_by_ip(summary, "203.0.113.10")
        google = next(d for d in node_a["destinations"] if d["ip"] == "142.250.185.78")
        self.assertEqual(google["port"], 443)
        self.assertEqual(google["used"], 2)
        self.assertEqual(google["time_wait"], 1)
        self.assertEqual(google["held"], 2)
        # Нечётный 9C41 выдан вторым проходом — быструю половину держит только 9C40
        self.assertEqual(google["fast_held"], 1)
        self.assertEqual(google["free"], FAST_CAPACITY - 1)

    def test_second_source_has_its_own_ceiling(self):
        summary = summarize([TCP_DUMP])
        node_b = source_by_ip(summary, "203.0.113.11")
        self.assertEqual(node_b["used"], 1)
        self.assertEqual(node_b["destinations"][0]["free"], FAST_CAPACITY - 1)

    def test_source_carries_no_free_of_its_own(self):
        # Остаток есть только у направления: сумма по адресу может превышать потолок
        summary = summarize([TCP_DUMP])
        self.assertNotIn("free", source_by_ip(summary, "203.0.113.10"))

    def test_destinations_sorted_by_occupancy(self):
        summary = summarize([TCP_DUMP])
        node_a = source_by_ip(summary, "203.0.113.10")
        self.assertEqual(node_a["destinations"][0]["ip"], "142.250.185.78")

    def test_time_wait_heavy_destination_ranks_below_when_reusable(self):
        # Направление с горой TIME_WAIT при tcp_tw_reuse=1 потолок не подпирает,
        # поэтому первым должно идти то, где больше живых соединений
        rows = [TCP_HEADER]
        for offset in range(5):
            rows.append(tcp_row(f"{NODE_A}:{0x9C40 + offset:04X}", f"{GOOGLE}:01BB", "06"))
        for offset in range(5, 8):
            rows.append(tcp_row(f"{NODE_A}:{0x9C40 + offset:04X}", f"{CLOUDFLARE}:01BB", "01"))
        summary = summarize(["".join(rows)])
        node_a = source_by_ip(summary, "203.0.113.10")
        self.assertEqual(node_a["destinations"][0]["ip"], "1.1.1.1")
        self.assertEqual(node_a["destinations"][0]["held"], 3)
        self.assertEqual(node_a["destinations"][1]["held"], 0)

    def test_mapped_ipv4_folds_into_the_same_source(self):
        summary = summarize([TCP_DUMP, TCP6_DUMP])
        node_a = source_by_ip(summary, "203.0.113.10")
        google = next(d for d in node_a["destinations"] if d["ip"] == "142.250.185.78")
        self.assertEqual(google["used"], 3)
        self.assertEqual([s["ip"] for s in summary["sources"]].count("203.0.113.10"), 1)

    def test_native_ipv6_is_its_own_source(self):
        summary = summarize([TCP_DUMP, TCP6_DUMP])
        node_v6 = source_by_ip(summary, "2001:db8::1")
        self.assertEqual(node_v6["used"], 1)
        self.assertEqual(node_v6["destinations"][0]["ip"], "2001:db8::2")

    def test_time_wait_holds_a_port_without_tw_reuse(self):
        no_reuse = KernelPortSettings(low=1024, high=65535, reserved_in_range=12, reserved_fast=6, tw_reuse=0)
        summary = summarize([TCP_DUMP], no_reuse)
        node_a = source_by_ip(summary, "203.0.113.10")
        google = next(d for d in node_a["destinations"] if d["ip"] == "142.250.185.78")
        self.assertEqual(google["free"], FAST_CAPACITY - 2)

    def test_loopback_only_tw_reuse_does_not_free_public_addresses(self):
        # tcp_tw_reuse=2 — дефолт ядра: переиспользование только для loopback
        loopback_only = KernelPortSettings(low=1024, high=65535, reserved_in_range=12, reserved_fast=6, tw_reuse=2)
        summary = summarize([TCP_DUMP], loopback_only)
        node_a = source_by_ip(summary, "203.0.113.10")
        google = next(d for d in node_a["destinations"] if d["ip"] == "142.250.185.78")
        self.assertEqual(google["free"], FAST_CAPACITY - 2)

    def test_capacity_subtracts_reserved_ports(self):
        summary = summarize([TCP_DUMP])
        self.assertEqual(summary["capacity"], CAPACITY)
        self.assertEqual(summary["fast_capacity"], FAST_CAPACITY)
        self.assertEqual(summary["reserved"], 12)
        self.assertEqual((summary["range_low"], summary["range_high"]), (1024, 65535))

    def test_free_never_goes_negative(self):
        # Диапазон 40000-40003 целиком зарезервирован: потолок 0, а сокеты в нём есть
        exhausted = KernelPortSettings(low=40000, high=40003, reserved_in_range=4, reserved_fast=2, tw_reuse=1)
        summary = summarize([TCP_DUMP], exhausted)
        node_a = source_by_ip(summary, "203.0.113.10")
        self.assertEqual(summary["capacity"], 0)
        self.assertEqual(summary["fast_capacity"], 0)
        self.assertTrue(all(d["free"] == 0 for d in node_a["destinations"]))

    def test_fast_held_and_free_always_add_up_to_fast_capacity(self):
        summary = summarize([TCP_DUMP])
        for source in summary["sources"]:
            for destination in source["destinations"]:
                self.assertEqual(destination["fast_held"] + destination["free"], summary["fast_capacity"])

    def test_ports_outside_a_narrow_range_are_ignored(self):
        narrow = KernelPortSettings(low=40000, high=40001, reserved_in_range=0, reserved_fast=0, tw_reuse=1)
        summary = summarize([TCP_DUMP], narrow)
        node_a = source_by_ip(summary, "203.0.113.10")
        self.assertEqual(node_a["used"], 2)
        self.assertEqual(node_a["destinations_total"], 1)

    def test_empty_dump_gives_no_sources(self):
        summary = summarize([TCP_HEADER])
        self.assertEqual(summary["sources"], [])
        self.assertEqual(summary["capacity"], CAPACITY)


class FastHalfTests(unittest.TestCase):
    """connect() первым проходом перебирает только порты чётности нижней границы;
    когда они кончаются, каждое соединение перебирает их целиком — остаток
    считается до конца этой половины, а не всего диапазона."""

    @staticmethod
    def _google_from(ports: list[int], settings: KernelPortSettings) -> dict:
        rows = [TCP_HEADER] + [tcp_row(f"{NODE_A}:{port:04X}", f"{GOOGLE}:01BB", "01") for port in ports]
        summary = summarize(["".join(rows)], settings)
        return source_by_ip(summary, "203.0.113.10")["destinations"][0], summary

    def test_fast_half_exhausted_while_range_is_half_free(self):
        # 8 портов: в первый проход входят 40000/40002/40004/40006 — все заняты
        settings = KernelPortSettings(low=40000, high=40007, reserved_in_range=0, reserved_fast=0, tw_reuse=1)
        google, summary = self._google_from([40000, 40002, 40004, 40006], settings)
        self.assertEqual(summary["capacity"], 8)
        self.assertEqual(summary["fast_capacity"], 4)
        self.assertEqual(google["held"], 4)
        self.assertEqual(google["fast_held"], 4)
        self.assertEqual(google["free"], 0)

    def test_second_pass_ports_do_not_eat_fast_half(self):
        # Порты другой чётности выдаёт bind() или второй проход — быстрый запас цел
        settings = KernelPortSettings(low=40000, high=40007, reserved_in_range=0, reserved_fast=0, tw_reuse=1)
        google, _ = self._google_from([40001, 40003], settings)
        self.assertEqual(google["held"], 2)
        self.assertEqual(google["fast_held"], 0)
        self.assertEqual(google["free"], 4)

    def test_odd_length_range_drops_top_port_from_fast_half(self):
        # Ядро округляет длину диапазона до чётной: 40004 в первый проход не входит
        settings = KernelPortSettings(low=40000, high=40004, reserved_in_range=0, reserved_fast=0, tw_reuse=1)
        self.assertEqual(settings.fast_high, 40002)
        self.assertEqual(settings.fast_capacity, 2)
        self.assertFalse(settings.is_fast_port(40004))

    def test_fast_parity_follows_range_floor(self):
        settings = KernelPortSettings(low=32769, high=60999, reserved_in_range=0, reserved_fast=0, tw_reuse=1)
        self.assertTrue(settings.is_fast_port(32769))
        self.assertFalse(settings.is_fast_port(32770))

    def test_destinations_ranked_by_fast_half(self):
        # Больше сокетов у Cloudflare, но все на второй половине — ближе к
        # полному перебору Google
        rows = [TCP_HEADER]
        rows += [tcp_row(f"{NODE_A}:{port:04X}", f"{GOOGLE}:01BB", "01") for port in (40000, 40002)]
        rows += [tcp_row(f"{NODE_A}:{port:04X}", f"{CLOUDFLARE}:01BB", "01") for port in (40001, 40003, 40005)]
        summary = summarize(["".join(rows)])
        node_a = source_by_ip(summary, "203.0.113.10")
        self.assertEqual(node_a["destinations"][0]["ip"], "142.250.185.78")


class LimitsTests(unittest.TestCase):
    def test_sources_and_destinations_are_capped(self):
        rows = [TCP_HEADER]
        # 40 адресов источника × 20 направлений — больше обоих потолков выдачи
        for source in range(40):
            for destination in range(20):
                local = f"{source:02X}7100CB:{0x9C40 + destination:04X}"
                remote = f"{destination:02X}B9FA8E:01BB"
                rows.append(tcp_row(local, remote, "01"))
        summary = summarize(["".join(rows)])
        self.assertEqual(len(summary["sources"]), 32)
        self.assertTrue(all(len(s["destinations"]) == 10 for s in summary["sources"]))
        self.assertTrue(all(s["destinations_total"] == 20 for s in summary["sources"]))


if __name__ == "__main__":
    unittest.main()
