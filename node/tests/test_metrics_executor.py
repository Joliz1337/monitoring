"""Секции /api/metrics считаются в своём пуле потоков, а не в общем пуле агента.

По этому ответу панель судит, жива ли нода. В общем пуле asyncio.to_thread
(пять потоков на 1 vCPU) секции стояли в очереди за долгими операциями хоста и
опросом адресов за NAT — nginx ноды отвечал 504, и живая нода выглядела
выключенной.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import metrics_collector  # noqa: E402
from app.services.metrics_collector import METRICS_SECTION_THREADS, MetricsCollector  # noqa: E402
from app.services.nat_addresses import NatAddressProbe  # noqa: E402

SECTIONS = (
    "get_cpu_info", "get_memory_info", "get_disk_info", "get_network_info",
    "get_processes_info", "get_system_info", "get_certificates_info", "get_antiddos_info",
)
# Предел ожидания: зависший ответ валит тест, а не подвешивает прогон
WAIT_SEC = 10.0


def worker_name(*_args) -> str:
    return threading.current_thread().name


class MetricsSectionPoolTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        host_dir = tempfile.TemporaryDirectory()
        self.addCleanup(host_dir.cleanup)
        settings = SimpleNamespace(host_proc=str(Path(host_dir.name) / "proc"), node_name="test")
        with mock.patch.object(metrics_collector, "get_settings", return_value=settings):
            self.collector = MetricsCollector(
                rate_sampler=SimpleNamespace(snapshot=lambda: None),
                loss_probe=SimpleNamespace(snapshot=lambda sources: None),
                nat_addresses=NatAddressProbe(),
            )
        self.addCleanup(self.collector._section_pool.shutdown, wait=False)
        for section in SECTIONS:
            setattr(self.collector, section, worker_name)

    async def test_every_section_runs_in_the_metrics_pool(self):
        metrics = await asyncio.wait_for(self.collector.get_all_metrics(), WAIT_SEC)

        workers = [metrics[key] for key in ("cpu", "memory", "disk", "network", "processes", "system", "certificates", "antiddos")]
        self.assertTrue(all(name.startswith("metrics") for name in workers), workers)

    async def test_metrics_answer_while_the_shared_pool_is_busy(self):
        release = threading.Event()
        shared_pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(shared_pool.shutdown, wait=False)
        self.addCleanup(release.set)
        asyncio.get_running_loop().set_default_executor(shared_pool)
        # Единственный поток общего пула занят долгой операцией хоста
        blocked = asyncio.ensure_future(asyncio.to_thread(release.wait, WAIT_SEC))

        metrics = await asyncio.wait_for(self.collector.get_all_metrics(), WAIT_SEC / 2)

        self.assertFalse(blocked.done())
        self.assertEqual(metrics["server_name"], "test")
        release.set()
        await blocked

    def test_one_request_never_queues_behind_itself(self):
        self.assertEqual(METRICS_SECTION_THREADS, len(SECTIONS))


if __name__ == "__main__":
    unittest.main()
