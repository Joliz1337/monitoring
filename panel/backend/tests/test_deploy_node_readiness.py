"""Готовность ноды перед постустановочными шагами автоустановки.

Установщик считает ноду готовой по внутреннему API (127.0.0.1:7500), а панель
ходит к ней через nginx на 9100, который compose поднимает только после
healthcheck агента. На момент выхода установщика этот порт ещё закрыт, поэтому
постшаги обязаны дождаться ответа ноды — раскатка wildcard-сертификата уходит
одним запросом без повторов и при закрытом порте теряется навсегда.
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.deploy_job_manager import (  # noqa: E402
    DeployJob,
    DeployJobManager,
    PostDeployOptions,
)


def make_job() -> DeployJob:
    return DeployJob(
        id="job", name="node-1", host="203.0.113.10",
        server_url="https://203.0.113.10:9100",
    )


def make_manager() -> tuple[DeployJobManager, list[str]]:
    """Менеджер с записанным порядком шагов вместо реальных походов в БД и ноду."""
    manager = DeployJobManager()
    calls: list[str] = []

    async def create_server(*args, **kwargs):
        calls.append("create_server")
        return 42

    async def wait_ready(*args, **kwargs):
        calls.append("wait_ready")

    def step(name):
        async def run(*args, **kwargs):
            calls.append(name)
        return run

    manager._create_server = create_server
    manager._wait_node_ready = wait_ready
    manager._post_install = step("post_install")
    manager._bind_profiles = step("bind_profiles")
    manager._apply_wildcard_ssl = step("wildcard_ssl")
    manager._bind_remnawave_nginx = step("remnawave_nginx")
    return manager, calls


class NodeReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_node_before_deploying_certificate(self):
        manager, calls = make_manager()
        job = make_job()

        await manager._on_install_done(job, 0, PostDeployOptions(wildcard_ssl_enabled=True))

        self.assertIn("wait_ready", calls)
        self.assertLess(calls.index("wait_ready"), calls.index("wildcard_ssl"))
        self.assertEqual(job.status, "success")

    async def test_wait_goes_after_server_record_is_created(self):
        manager, calls = make_manager()

        await manager._on_install_done(make_job(), 0, PostDeployOptions())

        self.assertLess(calls.index("create_server"), calls.index("wait_ready"))

    async def test_confirmed_online_node_is_not_polled_again(self):
        """Полуавтомат и rescue уже дождались ноды — второй раз не ждём."""
        manager, calls = make_manager()

        await manager._on_install_done(make_job(), 0, PostDeployOptions(), node_online=True)

        self.assertNotIn("wait_ready", calls)
        self.assertIn("wildcard_ssl", calls)

    async def test_failed_install_skips_everything(self):
        manager, calls = make_manager()
        job = make_job()

        await manager._on_install_done(job, 1, PostDeployOptions(wildcard_ssl_enabled=True))

        self.assertEqual(calls, [])
        self.assertEqual(job.status, "error")


class WaitNodeOnlineSignatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_window_is_overridable(self):
        """Короткое окно готовности и долгое ожидание установки — один цикл."""
        manager = DeployJobManager()
        job = make_job()
        server = SimpleNamespace(id=42, url=job.server_url, proxy_url=None, pki_enabled=True)

        online = await manager._wait_node_online(job, server, timeout=0, interval=0)

        self.assertFalse(online)


if __name__ == "__main__":
    unittest.main()
