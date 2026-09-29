"""Обновление HAProxy: какую ветку предложить ноде и команда запуска install.sh.

Ветка определяется по наличию репозитория сборщика под релиз ноды: 3.4 собрана
для Ubuntu 26.04, но не для 24.04 — там новейшая 3.2. Проверка репозитория не
должна тормозить страницу «Обновления», если сеть панели не достаёт Launchpad.
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402

from app.services import haproxy_upgrade, update_channel  # noqa: E402
from app.services.deploy_service import build_haproxy_upgrade_command  # noqa: E402


class FakeExternalClient:
    def __init__(self, existing: set[str], error: Exception | None = None, delay: float = 0):
        self.existing = existing
        self.error = error
        self.delay = delay
        self.requested: list[str] = []

    async def head(self, url: str, follow_redirects: bool = False):
        self.requested.append(url)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return mock.Mock(status_code=200 if url in self.existing else 404)


def noble_url(branch: str) -> str:
    return haproxy_upgrade.release_url("ubuntu", "noble", branch)


class BranchParsingTests(unittest.TestCase):
    def test_package_versions(self):
        self.assertEqual(haproxy_upgrade.parse_branch("2.8.16-0ubuntu0.24.04.3"), (2, 8))
        self.assertEqual(haproxy_upgrade.parse_branch("3.2.24-1ppa1~noble"), (3, 2))
        self.assertEqual(haproxy_upgrade.parse_branch("1:3.10.2-1"), (3, 10))

    def test_unknown(self):
        self.assertIsNone(haproxy_upgrade.parse_branch(None))
        self.assertIsNone(haproxy_upgrade.parse_branch("garbage"))

    def test_release_urls(self):
        self.assertEqual(
            noble_url("3.2"),
            "https://ppa.launchpadcontent.net/vbernat/haproxy-3.2/ubuntu/dists/noble/Release",
        )
        self.assertEqual(
            haproxy_upgrade.release_url("debian", "trixie", "3.4"),
            "https://haproxy.debian.net/dists/trixie-backports-3.4/Release",
        )
        self.assertIsNone(haproxy_upgrade.release_url("centos", "stream9", "3.4"))


class NewestBranchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        haproxy_upgrade._branch_cache.clear()
        haproxy_upgrade._probes.clear()

    def use_client(self, client: FakeExternalClient):
        patcher = mock.patch.object(haproxy_upgrade, "get_external_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_picks_newest_built_branch_and_caches_it(self):
        client = FakeExternalClient({noble_url("3.2"), noble_url("3.0")})
        self.use_client(client)
        self.assertEqual(await haproxy_upgrade.newest_branch("ubuntu", "noble"), "3.2")
        self.assertEqual(await haproxy_upgrade.newest_branch("ubuntu", "noble"), "3.2")
        self.assertEqual(client.requested, [noble_url("3.4"), noble_url("3.2")])

    async def test_no_build_for_release(self):
        self.use_client(FakeExternalClient(set()))
        self.assertIsNone(await haproxy_upgrade.newest_branch("ubuntu", "noble"))

    async def test_unsupported_distro_makes_no_requests(self):
        client = FakeExternalClient(set())
        self.use_client(client)
        self.assertIsNone(await haproxy_upgrade.newest_branch("centos", "stream9"))
        self.assertEqual(client.requested, [])

    async def test_network_error_is_retried_later_not_forever(self):
        self.use_client(FakeExternalClient(set(), error=httpx.ConnectError("blocked")))
        self.assertIsNone(await haproxy_upgrade.newest_branch("ubuntu", "noble"))
        expires, _ = haproxy_upgrade._branch_cache[("ubuntu", "noble")]
        self.assertLessEqual(expires - asyncio.get_running_loop().time(), haproxy_upgrade.PROBE_RETRY_SECONDS + 1)

    async def test_slow_network_does_not_block_the_page(self):
        self.use_client(FakeExternalClient({noble_url("3.2")}, delay=0.3))
        with mock.patch.object(haproxy_upgrade, "PROBE_WAIT_SECONDS", 0.05):
            self.assertIsNone(await haproxy_upgrade.newest_branch("ubuntu", "noble"))
        await asyncio.sleep(0.8)
        self.assertEqual(await haproxy_upgrade.newest_branch("ubuntu", "noble"), "3.2")


class DescribeHaproxyTests(unittest.IsolatedAsyncioTestCase):
    async def describe(self, info, newest="3.2"):
        with mock.patch.object(haproxy_upgrade, "newest_branch", mock.AsyncMock(return_value=newest)):
            return await haproxy_upgrade.describe_haproxy(info)

    async def test_older_branch_gets_target(self):
        info = {"version": "2.8.16-0ubuntu0.24.04.3", "os_id": "ubuntu", "os_codename": "noble"}
        self.assertEqual(await self.describe(info), {"version": "2.8.16-0ubuntu0.24.04.3", "target_branch": "3.2"})

    async def test_same_or_newer_branch_has_no_target(self):
        for version in ("3.2.24-1ppa1~noble", "3.3.1-1ppa1~noble"):
            info = {"version": version, "os_id": "ubuntu", "os_codename": "noble"}
            self.assertIsNone((await self.describe(info))["target_branch"])

    async def test_node_without_haproxy_info(self):
        self.assertIsNone(await self.describe(None))

    async def test_unknown_release_has_no_target(self):
        info = {"version": "2.8.16", "os_id": None, "os_codename": None}
        self.assertIsNone((await self.describe(info))["target_branch"])


class UpgradeCommandTests(unittest.TestCase):
    def tearDown(self):
        update_channel.set_current_branch(update_channel.STABLE_BRANCH)

    def test_runs_only_haproxy_upgrade(self):
        command = build_haproxy_upgrade_command()
        self.assertIn("MON_INSTALL_HAPROXY=1", command)
        self.assertNotIn("MON_INSTALL_NODE", command)
        self.assertTrue(command.endswith("--unattended"))
        self.assertIn("/main/install.sh", command)

    def test_dev_channel_is_propagated(self):
        update_channel.set_current_branch(update_channel.DEV_BRANCH)
        command = build_haproxy_upgrade_command()
        self.assertIn("MON_BRANCH=dev", command)
        self.assertIn("/dev/install.sh", command)


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_job_says_it_is_queued(self):
        async def events():
            yield {"type": "done", "exit_code": 0}

        slots = asyncio.Semaphore(1)
        with mock.patch.object(haproxy_upgrade, "_upgrade_slots", slots):
            await slots.acquire()
            queued = haproxy_upgrade._queued(events())
            first = await queued.__anext__()
            self.assertIn("В очереди", first["line"])
            slots.release()
            self.assertEqual(await queued.__anext__(), {"type": "done", "exit_code": 0})


if __name__ == "__main__":
    unittest.main()
