"""Фоновые задачи доставки образа по SSH.

Окно лога на фронте можно закрыть — задача живёт сама, а страница обновлений
узнаёт статусы из list_jobs по server_id. Массовый запуск упирается в общий
аплинк панели, поэтому одновременно заливается не больше N нод, остальные ждут
в очереди; повторный запуск по той же ноде не плодит вторую заливку.
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import node_image_delivery as module  # noqa: E402
from app.services.node_image_delivery import ImageDeliveryJobManager  # noqa: E402
from app.services.ssh_target import SSHTarget  # noqa: E402


def target(host: str) -> SSHTarget:
    return SSHTarget(host=host, password="secret")


class GatedDelivery:
    """Подмена deliver_image: каждая доставка ждёт своего сигнала на завершение."""

    def __init__(self):
        self.gates: dict[str, asyncio.Event] = {}
        self.outcome: dict[str, dict] = {}

    async def __call__(self, t: SSHTarget, tag: str):
        gate = self.gates.setdefault(t.host, asyncio.Event())
        yield {"type": "log", "line": f"start {t.host}"}
        await gate.wait()
        yield self.outcome.get(t.host, {"type": "done", "message": "ok"})

    def release(self, host: str, outcome: dict | None = None):
        if outcome is not None:
            self.outcome[host] = outcome
        self.gates.setdefault(host, asyncio.Event()).set()


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


class DeliveryJobManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.delivery = GatedDelivery()
        patcher = mock.patch.object(module, "deliver_image", self.delivery)
        patcher.start()
        self.addCleanup(patcher.stop)

    def statuses(self, manager: ImageDeliveryJobManager) -> dict[int, str]:
        return {j["server_id"]: j["status"] for j in manager.list_jobs()}

    async def test_concurrency_limit_queues_extra_jobs(self):
        manager = ImageDeliveryJobManager(concurrency=2)
        for sid in (1, 2, 3):
            manager.start(sid, f"node-{sid}", target(f"h{sid}"), "latest")
        await settle()

        self.assertEqual(self.statuses(manager), {1: "running", 2: "running", 3: "queued"})

        self.delivery.release("h1")
        await settle()
        self.assertEqual(self.statuses(manager), {1: "success", 2: "running", 3: "running"})

        self.delivery.release("h2")
        self.delivery.release("h3")
        await settle()
        self.assertEqual(self.statuses(manager), {1: "success", 2: "success", 3: "success"})

    async def test_queued_job_logs_why_it_waits(self):
        manager = ImageDeliveryJobManager(concurrency=1)
        manager.start(1, "node-1", target("h1"), "latest")
        queued_id = manager.start(2, "node-2", target("h2"), "latest")
        await settle()

        self.assertIn("В очереди", manager.get(queued_id).log[0])
        self.delivery.release("h1")
        self.delivery.release("h2")
        await settle()

    async def test_second_start_for_same_server_reuses_active_job(self):
        manager = ImageDeliveryJobManager()
        first = manager.start(7, "node-7", target("h7"), "latest")
        second = manager.start(7, "node-7", target("h7"), "latest")
        self.assertEqual(first, second)
        self.assertEqual(len(manager.list_jobs()), 1)

        self.delivery.release("h7")
        await settle()
        third = manager.start(7, "node-7", target("h7"), "latest")
        self.assertNotEqual(first, third)  # прошлая завершилась — новая доставка законна
        self.delivery.release("h7")
        await settle()

    async def test_error_marks_job_and_reaches_subscriber(self):
        manager = ImageDeliveryJobManager()
        job_id = manager.start(1, "node-1", target("h1"), "latest")
        await settle()

        events = []

        async def collect():
            async for ev in manager.subscribe(job_id):
                events.append(ev)

        reader = asyncio.create_task(collect())
        await settle()
        self.delivery.release("h1", {"type": "error", "message": "SSH: неверный логин"})
        await asyncio.wait_for(reader, 1)

        job = manager.get(job_id)
        self.assertEqual(job.status, "error")
        self.assertEqual(job.error, "SSH: неверный логин")
        self.assertIn({"type": "error", "message": "SSH: неверный логин"}, events)
        self.assertEqual(events[-1], {"type": "done", "status": "error"})

    async def test_finished_job_replays_log_for_late_subscriber(self):
        manager = ImageDeliveryJobManager()
        job_id = manager.start(1, "node-1", target("h1"), "latest")
        self.delivery.release("h1")
        await settle()

        events = [ev async for ev in manager.subscribe(job_id)]
        lines = [ev["line"] for ev in events if ev["type"] == "log"]
        self.assertEqual(lines, ["start h1", "[panel] ok"])
        self.assertEqual(events[-1], {"type": "done", "status": "success"})


if __name__ == "__main__":
    unittest.main()
