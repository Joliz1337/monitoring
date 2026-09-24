"""Разбор раскладки памяти по NUMA-узлам из sysfs."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.numa import count_cpus, read_numa_nodes  # noqa: E402


def write_node(sys_root: Path, name: str, cpulist: str, mem_kb: int) -> None:
    node_dir = sys_root / "devices/system/node" / name
    node_dir.mkdir(parents=True)
    (node_dir / "cpulist").write_text(cpulist + "\n")
    (node_dir / "meminfo").write_text(
        f"Node {name[4:]} MemTotal:       {mem_kb} kB\n"
        f"Node {name[4:]} MemFree:        1024 kB\n"
    )


class CountCpusTest(unittest.TestCase):
    def test_ranges_and_singles(self):
        self.assertEqual(count_cpus("0-7,16-23\n"), 16)
        self.assertEqual(count_cpus("3"), 1)
        self.assertEqual(count_cpus("0,2,4-5"), 4)

    def test_memory_only_node_has_no_cpus(self):
        self.assertEqual(count_cpus("\n"), 0)


class ReadNumaNodesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_two_sockets_sorted_with_bytes(self):
        write_node(self.root, "node1", "8-15", 0)
        write_node(self.root, "node0", "0-7", 65_856_148)
        (self.root / "devices/system/node/possible").write_text("0-1\n")

        self.assertEqual(read_numa_nodes(self.root), [
            {"node": 0, "cpus": 8, "memory_total": 65_856_148 * 1024},
            {"node": 1, "cpus": 8, "memory_total": 0},
        ])

    def test_missing_sysfs_gives_empty_list(self):
        self.assertEqual(read_numa_nodes(self.root), [])

    def test_unreadable_node_is_skipped(self):
        write_node(self.root, "node0", "0-3", 4096)
        (self.root / "devices/system/node/node1").mkdir()

        self.assertEqual([n["node"] for n in read_numa_nodes(self.root)], [0])


if __name__ == "__main__":
    unittest.main()
