"""Папка, выбранная в форме добавления сервера, доходит до записи сервера —
и при обычном добавлении, и при автоустановке, где запись создаётся только
после того, как нода встала."""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers.servers import clean_folder_name  # noqa: E402
from app.services.deploy_job_manager import (  # noqa: E402
    DeployJob,
    DeployJobManager,
    PostDeployOptions,
)


class CleanFolderNameTests(unittest.TestCase):
    def test_trims_spaces(self):
        self.assertEqual(clean_folder_name("  Нидерланды  "), "Нидерланды")

    def test_blank_means_no_folder(self):
        self.assertIsNone(clean_folder_name("   "))
        self.assertIsNone(clean_folder_name(""))
        self.assertIsNone(clean_folder_name(None))


def make_manager() -> tuple[DeployJobManager, dict]:
    """Менеджер, у которого создание сервера записывает аргументы, а постшаги пустые."""
    manager = DeployJobManager()
    created: dict = {}

    async def create_server(*args, **kwargs):
        created.update(kwargs)
        return 42

    async def noop(*args, **kwargs):
        return None

    manager._create_server = create_server
    manager._wait_node_ready = noop
    manager._post_install = noop
    manager._bind_profiles = noop
    manager._apply_wildcard_ssl = noop
    manager._bind_remnawave_nginx = noop
    return manager, created


class DeployFolderTests(unittest.IsolatedAsyncioTestCase):
    async def test_folder_reaches_server_record(self):
        manager, created = make_manager()
        job = DeployJob(
            id="job", name="node-1", host="203.0.113.10",
            server_url="https://203.0.113.10:9100", folder="Нидерланды",
        )

        await manager._on_install_done(job, 0, PostDeployOptions())

        self.assertEqual(created["folder"], "Нидерланды")

    async def test_start_keeps_folder_on_job(self):
        manager = DeployJobManager()

        async def skip_install(*args, **kwargs):
            return None

        manager._run = skip_install
        params = SimpleNamespace(host="203.0.113.10", socks5_proxy=None, ssh_port=22)

        job_id = manager.start(
            params, "node-1", "https://203.0.113.10:9100", PostDeployOptions(), folder="Релеи",
        )

        self.assertEqual(manager._jobs[job_id].folder, "Релеи")


if __name__ == "__main__":
    unittest.main()
