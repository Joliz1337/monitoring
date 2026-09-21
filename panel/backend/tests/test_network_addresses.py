"""Грамматика ввода дополнительных IP-адресов.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"

Проверяется, что каждая форма записи разворачивается ровно в то, что ждёт
оператор: одиночный адрес получает host-маску, `1.2.3.4/24` остаётся одним
адресом, `1.2.3.0/29` — вся подсеть, диапазон — по адресу на каждый номер;
IPv6-подсети не разворачиваются, служебные адреса отвергаются, дубли
схлопываются, а тексты ошибок начинаются с записи-виновника.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services.network_addresses import (
        MAX_ADDRESSES,
        AddressInputError,
        AddressSpec,
        expand_entries,
        expand_entry,
        normalize_ref,
        preview,
        split_entries,
    )
except ImportError as e:  # pragma: no cover
    raise unittest.SkipTest(f"network_addresses requires the panel runtime: {e}")


def cidrs(specs: list[AddressSpec]) -> list[str]:
    return [spec.cidr for spec in specs]


class SingleAddressTests(unittest.TestCase):
    def test_host_prefix_by_family(self):
        self.assertEqual(cidrs(expand_entry("1.2.3.4")), ["1.2.3.4/32"])
        self.assertEqual(cidrs(expand_entry("2001:db8::2")), ["2001:db8::2/128"])

    def test_ipv6_is_canonicalised(self):
        self.assertEqual(cidrs(expand_entry("2001:DB8:0:0::2")), ["2001:db8::2/128"])

    def test_explicit_prefix_with_host_bits_is_single_address(self):
        self.assertEqual(cidrs(expand_entry("1.2.3.4/24")), ["1.2.3.4/24"])
        self.assertEqual(cidrs(expand_entry("2001:db8::2/64")), ["2001:db8::2/64"])

    def test_garbage(self):
        for entry in ("1.2.3", "1.2.3.4/33", "2001:db8::/129", "hello", "1.2.3.4/x"):
            with self.subTest(entry=entry):
                with self.assertRaises(AddressInputError):
                    expand_entry(entry)


class RangeTests(unittest.TestCase):
    def test_ipv4_range(self):
        self.assertEqual(cidrs(expand_entry("1.2.3.10-1.2.3.15")), [f"1.2.3.{n}/32" for n in range(10, 16)])

    def test_short_end(self):
        self.assertEqual(cidrs(expand_entry("1.2.3.10-15")), [f"1.2.3.{n}/32" for n in range(10, 16)])

    def test_ipv6_range(self):
        self.assertEqual(cidrs(expand_entry("2001:db8::10-2001:db8::12")),
                         ["2001:db8::10/128", "2001:db8::11/128", "2001:db8::12/128"])

    def test_bad_ranges(self):
        for entry in ("1.2.3.15-1.2.3.10", "1.2.3.10-2001:db8::1", "1.2.0.0-1.2.1.255"):
            with self.subTest(entry=entry):
                with self.assertRaises(AddressInputError):
                    expand_entry(entry)


class SubnetTests(unittest.TestCase):
    def test_whole_subnet_keeps_prefix_and_skips_network_broadcast(self):
        self.assertEqual(cidrs(expand_entry("1.2.3.0/29")), [f"1.2.3.{n}/29" for n in range(1, 7)])
        self.assertEqual(cidrs(expand_entry("1.2.3.0/30")), ["1.2.3.1/30", "1.2.3.2/30"])

    def test_31_gives_both_and_32_gives_one(self):
        self.assertEqual(cidrs(expand_entry("1.2.3.0/31")), ["1.2.3.0/31", "1.2.3.1/31"])
        self.assertEqual(cidrs(expand_entry("1.2.3.0/32")), ["1.2.3.0/32"])

    def test_24_fits_and_22_does_not(self):
        self.assertEqual(len(expand_entry("1.2.3.0/24")), 254)
        with self.assertRaises(AddressInputError) as ctx:
            expand_entry("1.2.0.0/22")
        self.assertIn("1022", str(ctx.exception))

    def test_ipv6_subnet_is_refused_with_hint(self):
        with self.assertRaises(AddressInputError) as ctx:
            expand_entry("2001:db8::/64")
        self.assertIn("2001:db8::2/64", str(ctx.exception))
        self.assertEqual(cidrs(expand_entry("2001:db8::/128")), ["2001:db8::/128"])


class UnassignableTests(unittest.TestCase):
    def test_refused(self):
        for entry in ("127.0.0.1", "169.254.1.1", "224.0.0.1", "0.0.0.0", "255.255.255.255", "::1", "fe80::1", "ff02::1"):
            with self.subTest(entry=entry):
                with self.assertRaises(AddressInputError):
                    expand_entry(entry)

    def test_private_ranges_are_allowed(self):
        for entry in ("10.0.0.5", "100.64.0.1", "172.16.0.2/24", "192.168.1.0/29", "fd00::1"):
            with self.subTest(entry=entry):
                self.assertTrue(expand_entry(entry))


class DedupAndLimitsTests(unittest.TestCase):
    def test_separators_and_order(self):
        specs = expand_entries("1.2.3.4\n1.2.3.5, 1.2.3.6;1.2.3.7 1.2.3.8")
        self.assertEqual(cidrs(specs), [f"1.2.3.{n}/32" for n in range(4, 9)])

    def test_duplicates_collapse(self):
        self.assertEqual(cidrs(expand_entries("1.2.3.4 1.2.3.4-1.2.3.5")), ["1.2.3.4/32", "1.2.3.5/32"])

    def test_same_address_two_masks(self):
        with self.assertRaises(AddressInputError):
            expand_entries("1.2.3.4/24 1.2.3.4")

    def test_total_limit(self):
        with self.assertRaises(AddressInputError) as ctx:
            expand_entries("1.2.3.0/24 1.2.4.0/25")
        self.assertIn(str(MAX_ADDRESSES), str(ctx.exception))

    def test_empty(self):
        with self.assertRaises(AddressInputError):
            expand_entries(" \n ")
        self.assertEqual(split_entries(""), [])


class PreviewAndRefTests(unittest.TestCase):
    def test_preview_counts(self):
        result = preview("1.2.3.0/30 2001:db8::2")
        self.assertEqual((result["count"], result["ipv4"], result["ipv6"]), (3, 2, 1))
        self.assertEqual(result["addresses"][2], {"address": "2001:db8::2", "prefix": 128, "family": "ipv6"})

    def test_normalize_ref(self):
        self.assertEqual(normalize_ref("2001:DB8::0:2", 64).cidr, "2001:db8::2/64")
        with self.assertRaises(AddressInputError):
            normalize_ref("1.2.3.4", 40)

    def test_error_text_starts_with_entry(self):
        with self.assertRaises(AddressInputError) as ctx:
            expand_entry("1.2.3.4/33")
        self.assertTrue(str(ctx.exception).startswith("«1.2.3.4/33»"))


if __name__ == "__main__":
    unittest.main()
