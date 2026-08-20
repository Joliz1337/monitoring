"""Тесты резервации портов от эфемерной выдачи (services/reserved_ports)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.reserved_ports import (
    MAX_ENTRIES,
    base_ports,
    effective_reserved,
    normalize_entries,
    read_extra_entries,
    render_extra_file,
)


class NormalizeEntriesTests(unittest.TestCase):
    def test_single_ports_sorted_and_deduped(self):
        self.assertEqual(
            normalize_entries(["8443", "5201", "8443", " 5201 "]),
            ["5201", "8443"],
        )

    def test_range_kept_and_single_range_collapsed(self):
        self.assertEqual(
            normalize_entries(["8443-8450", "5201-5201"]),
            ["5201", "8443-8450"],
        )

    def test_rejects_garbage(self):
        for bad in ["musor", "80a", "-5", "10-", "1-2-3", "", "  "]:
            with self.assertRaises(ValueError, msg=bad):
                normalize_entries([bad])

    def test_rejects_out_of_bounds(self):
        for bad in ["0", "65536", "70000", "1-70000"]:
            with self.assertRaises(ValueError, msg=bad):
                normalize_entries([bad])

    def test_rejects_inverted_range(self):
        with self.assertRaises(ValueError):
            normalize_entries(["8450-8443"])

    def test_rejects_too_many_entries(self):
        entries = [str(1000 + i) for i in range(MAX_ENTRIES + 1)]
        with self.assertRaises(ValueError):
            normalize_entries(entries)

    def test_rejects_range_eating_ephemeral_space(self):
        with self.assertRaises(ValueError):
            normalize_entries(["1024-65535"])

    def test_total_cap_counts_ranges_not_entries(self):
        # 4096 портов ровно — проходит, 4097 — нет.
        normalize_entries(["10000-14095"])
        with self.assertRaises(ValueError):
            normalize_entries(["10000-14096"])

    def test_empty_list_ok(self):
        self.assertEqual(normalize_entries([]), [])


class ExtraFileTests(unittest.TestCase):
    def _write(self, content: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".conf", delete=False, encoding="utf-8"
        )
        tmp.write(content)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_round_trip(self):
        entries = normalize_entries(["5201", "8443-8450"])
        path = self._write(render_extra_file(entries))
        self.assertEqual(read_extra_entries(path), entries)

    def test_ignores_comments_junk_and_splits_commas(self):
        path = self._write(
            "# comment\n5201, 8443-8450; 9200\nmusor 70000\n2222 # inline\n"
        )
        self.assertEqual(
            read_extra_entries(path),
            ["5201", "8443-8450", "9200", "2222"],
        )

    def test_missing_file_is_empty(self):
        self.assertEqual(read_extra_entries(Path("/no/such/file")), [])


class BasePortsTests(unittest.TestCase):
    def test_contains_internal_api_and_remnawave(self):
        self.assertEqual(base_ports(api_port=9100), [2222, 7500, 9100])

    def test_custom_api_port(self):
        self.assertEqual(base_ports(api_port=20000), [2222, 7500, 20000])

    def test_deduplicates_api_port_collision(self):
        self.assertEqual(base_ports(api_port=7500), [2222, 7500])


class EffectiveReservedTests(unittest.TestCase):
    def test_reads_and_strips(self):
        tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        tmp.write("2222,7500,9100\n")
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        self.assertEqual(effective_reserved(Path(tmp.name)), "2222,7500,9100")

    def test_unreadable_is_none(self):
        self.assertIsNone(effective_reserved(Path("/no/such/proc")))


if __name__ == "__main__":
    unittest.main()
