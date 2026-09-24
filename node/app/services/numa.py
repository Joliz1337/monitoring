"""Раскладка памяти по NUMA-узлам (процессорным сокетам).

На многосокетном сервере память, воткнутая только в слоты одного процессора,
делает каждое обращение ядер второго процессора удалённым — через межсокетную
шину, с заметно большей задержкой. Снаружи сервер при этом выглядит исправным,
поэтому панель сравнивает объём памяти узлов и предупреждает о перекосе.
"""

import re
from pathlib import Path

NODE_SUBDIR = Path("devices/system/node")
NODE_DIR_PATTERN = re.compile(r"^node(\d+)$")
MEM_TOTAL_PATTERN = re.compile(r"MemTotal:\s+(\d+)\s*kB")
KIB = 1024


def count_cpus(cpulist: str) -> int:
    """Число CPU в формате sysfs-списка: `0-7,16-23`, пустая строка — ноль."""
    total = 0
    for chunk in cpulist.strip().split(","):
        if not chunk:
            continue
        low, _, high = chunk.partition("-")
        total += int(high or low) - int(low) + 1
    return total


def read_node_memory(meminfo: str) -> int:
    match = MEM_TOTAL_PATTERN.search(meminfo)
    return int(match.group(1)) * KIB if match else 0


def read_numa_nodes(sys_root: Path) -> list[dict]:
    """Узлы с числом CPU и объёмом памяти в байтах; пусто, если ядро узлов не отдаёт."""
    nodes_dir = sys_root / NODE_SUBDIR
    if not nodes_dir.is_dir():
        return []

    nodes = []
    for entry in nodes_dir.iterdir():
        match = NODE_DIR_PATTERN.match(entry.name)
        if not match:
            continue
        try:
            cpus = count_cpus((entry / "cpulist").read_text())
            memory_total = read_node_memory((entry / "meminfo").read_text())
        except (OSError, ValueError):
            continue
        nodes.append({"node": int(match.group(1)), "cpus": cpus, "memory_total": memory_total})
    return sorted(nodes, key=lambda node: node["node"])
