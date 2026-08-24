"""Тесты очереди прогона: размер задачи не ограничен, рабочих — постоянное число.

Голый unittest, без сети: исполнитель подменяется заглушкой.

Ячеек в прогоне может быть сколько угодно, поэтому создавать задачу на каждую
нельзя. Рабочие разбирают очередь, и здесь проверяется главное следствие: в
любой момент времени одновременно выполняется не больше заданного числа
проверок на каждое место запуска.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.xray_test.job_manager import XrayTestJobManager  # noqa: E402
from app.services.xray_test.matrix import build_matrix  # noqa: E402
from app.services.xray_test.models import CellResult, TestCell, Verdict  # noqa: E402
from app.services.xray_test.parsers import parse_link  # noqa: E402
from app.services.xray_test.probes import ProbeOptions  # noqa: E402

UUID = "11111111-2222-3333-4444-555555555555"


class CountingRunner:
    """Считает, сколько проверок идёт одновременно, и запоминает пик."""

    def __init__(self, delay: float = 0.001) -> None:
        self.delay = delay
        self.active = 0
        self.peak = 0
        self.done = 0

    async def probe(self, cell: TestCell, options: ProbeOptions) -> CellResult:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            self.done += 1
            return CellResult(
                index=cell.index,
                remark=cell.endpoint.remark,
                protocol=cell.endpoint.protocol.value,
                address=cell.endpoint.address,
                port=cell.endpoint.port,
                sni=cell.sni_label,
                transport=cell.endpoint.transport.kind.value,
                security=cell.endpoint.tls.security.value,
                verdict=Verdict.OK,
                location=cell.location,
                location_name=cell.location_name,
            )
        finally:
            self.active -= 1


def _cells(count: int, locations=None):
    endpoints = [
        parse_link(f"vless://{UUID}@h{i}.io:443?security=tls#node-{i}")
        for i in range(count)
    ]
    return build_matrix(endpoints, locations=locations)


async def _drain(manager: XrayTestJobManager, job_id: str) -> None:
    job = manager.get(job_id)
    assert job is not None and job.task is not None
    await job.task


class QueueTest(unittest.IsolatedAsyncioTestCase):
    async def test_large_run_completes(self):
        """Прогон намного больше прежнего потолка в 200 проверок доходит до конца."""
        manager = XrayTestJobManager()
        runner = CountingRunner()
        cells = _cells(500)

        job_id = manager.start(cells, ProbeOptions(), {"panel": runner},
                               location="panel", concurrency=4)
        await _drain(manager, job_id)
        job = manager.get(job_id)

        self.assertEqual(job.status, "success")
        self.assertEqual(len(job.results), 500)
        self.assertEqual(runner.done, 500)

    async def test_concurrency_respected(self):
        manager = XrayTestJobManager()
        runner = CountingRunner(delay=0.005)

        job_id = manager.start(_cells(60), ProbeOptions(), {"panel": runner},
                               location="panel", concurrency=3)
        await _drain(manager, job_id)

        self.assertLessEqual(runner.peak, 3)
        self.assertGreater(runner.peak, 1)

    async def test_each_location_has_own_quota(self):
        """Медленная точка не должна занимать слоты быстрой."""
        manager = XrayTestJobManager()
        panel = CountingRunner(delay=0.001)
        node = CountingRunner(delay=0.005)
        cells = _cells(30, locations=[("panel", ""), ("node:1", "Нода")])

        job_id = manager.start(cells, ProbeOptions(), {"panel": panel, "node:1": node},
                               location="panel, Нода", concurrency=2)
        await _drain(manager, job_id)

        self.assertEqual(panel.done, 30)
        self.assertEqual(node.done, 30)
        self.assertLessEqual(panel.peak, 2)
        self.assertLessEqual(node.peak, 2)

    async def test_results_carry_location(self):
        manager = XrayTestJobManager()
        runners = {"panel": CountingRunner(), "node:7": CountingRunner()}
        cells = _cells(5, locations=[("panel", ""), ("node:7", "Амстердам")])

        job_id = manager.start(cells, ProbeOptions(), runners,
                               location="panel, Амстердам", concurrency=2)
        await _drain(manager, job_id)
        job = manager.get(job_id)

        locations = {item["location"] for item in job.results}
        self.assertEqual(locations, {"panel", "node:7"})

    async def test_missing_runner_marks_cells_failed(self):
        manager = XrayTestJobManager()
        cells = _cells(3, locations=[("node:9", "Нет исполнителя")])

        job_id = manager.start(cells, ProbeOptions(), {}, location="node:9", concurrency=2)
        await _drain(manager, job_id)
        job = manager.get(job_id)

        self.assertEqual(len(job.results), 3)
        self.assertTrue(all(item["verdict"] == "fail" for item in job.results))

    async def test_progress_logged_on_long_run(self):
        manager = XrayTestJobManager()
        job_id = manager.start(_cells(60), ProbeOptions(), {"panel": CountingRunner()},
                               location="panel", concurrency=4)
        await _drain(manager, job_id)
        job = manager.get(job_id)

        self.assertTrue(any("Проверено" in line for line in job.log))


if __name__ == "__main__":
    unittest.main()
