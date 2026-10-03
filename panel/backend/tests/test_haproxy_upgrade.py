"""Обновление HAProxy: до какой версии предложить обновить ноду и команда запуска install.sh.

Версия берётся из индекса пакетов репозитория сборщика под релиз ноды: новейшая
ветка (3.4 собрана для Ubuntu 26.04, но не для 24.04 — там 3.2) и последняя
версия в ней, чтобы значок появлялся и на исправления внутри ветки (3.4.4 → 3.4.6).
Проверка репозитория не должна тормозить страницу «Обновления», если сеть панели
не достаёт Launchpad.
"""

import asyncio
import gzip
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402

from app.services import haproxy_upgrade, update_channel  # noqa: E402
from app.services.deploy_service import build_haproxy_upgrade_command  # noqa: E402

PPA_INDEX = """Package: haproxy
Architecture: amd64
Version: 3.4.6-1ppa1~resolute
Description: fast and reliable load balancing reverse proxy
 HAProxy is a TCP/HTTP reverse proxy.

Package: haproxy-doc
Version: 3.4.6-1ppa1~resolute

Package: vim-haproxy
Version: 3.4.6-1ppa1~resolute
"""

DEBIAN_INDEX = """Package: haproxy
Version: 3.4.3-1~bpo13+1

Package: haproxy
Version: 3.4.4-1~bpo13+1

Package: haproxy-doc
Version: 3.4.9-1~bpo13+1
"""


def index_response(text: str):
    return mock.Mock(status_code=200, content=gzip.compress(text.encode()))


class FakeExternalClient:
    def __init__(self, indexes: dict[str, str], error: Exception | None = None, delay: float = 0):
        self.indexes = indexes
        self.error = error
        self.delay = delay
        self.requested: list[str] = []

    async def get(self, url: str, follow_redirects: bool = False):
        self.requested.append(url)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        if url in self.indexes:
            return index_response(self.indexes[url])
        return mock.Mock(status_code=404, content=b"")


def ubuntu_url(codename: str, branch: str, arch: str = "amd64") -> str:
    return haproxy_upgrade.packages_url("ubuntu", codename, branch, arch)


class VersionParsingTests(unittest.TestCase):
    def test_package_versions(self):
        self.assertEqual(haproxy_upgrade.parse_version("2.8.16-0ubuntu0.24.04.3"), (2, 8, 16))
        self.assertEqual(haproxy_upgrade.parse_version("3.2.25-1ppa1~noble"), (3, 2, 25))
        self.assertEqual(haproxy_upgrade.parse_version("1:3.10.2-1"), (3, 10, 2))
        self.assertEqual(haproxy_upgrade.parse_version("3.4.6"), (3, 4, 6))

    def test_unknown(self):
        self.assertIsNone(haproxy_upgrade.parse_version(None))
        self.assertIsNone(haproxy_upgrade.parse_version("garbage"))

    def test_upstream_part(self):
        self.assertEqual(haproxy_upgrade.upstream_version("3.4.6-1ppa1~resolute"), "3.4.6")
        self.assertEqual(haproxy_upgrade.upstream_version("1:3.2.9-1ubuntu2.2"), "3.2.9")

    def test_index_urls(self):
        self.assertEqual(
            ubuntu_url("noble", "3.2", "arm64"),
            "https://ppa.launchpadcontent.net/vbernat/haproxy-3.2/ubuntu/dists/noble/main/binary-arm64/Packages.gz",
        )
        self.assertEqual(
            haproxy_upgrade.packages_url("debian", "trixie", "3.4", "amd64"),
            "https://haproxy.debian.net/dists/trixie-backports-3.4/main/binary-amd64/Packages.gz",
        )
        self.assertIsNone(haproxy_upgrade.packages_url("centos", "stream9", "3.4", "amd64"))


class PackagesIndexTests(unittest.TestCase):
    def test_ppa_index(self):
        self.assertEqual(haproxy_upgrade.newest_haproxy_version(PPA_INDEX), "3.4.6-1ppa1~resolute")

    def test_newest_of_several_and_other_packages_ignored(self):
        self.assertEqual(haproxy_upgrade.newest_haproxy_version(DEBIAN_INDEX), "3.4.4-1~bpo13+1")

    def test_no_haproxy(self):
        self.assertIsNone(haproxy_upgrade.newest_haproxy_version("Package: vim-haproxy\nVersion: 3.4.6\n"))


class NewestVersionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        haproxy_upgrade._release_cache.clear()
        haproxy_upgrade._probes.clear()

    def use_client(self, client: FakeExternalClient):
        patcher = mock.patch.object(haproxy_upgrade, "get_external_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_newest_built_branch_and_its_last_release_are_cached(self):
        client = FakeExternalClient({ubuntu_url("noble", "3.2"): PPA_INDEX.replace("3.4.6-1ppa1~resolute", "3.2.25-1ppa1~noble")})
        self.use_client(client)
        self.assertEqual(await haproxy_upgrade.newest_version("ubuntu", "noble", "amd64"), "3.2.25")
        self.assertEqual(await haproxy_upgrade.newest_version("ubuntu", "noble", "amd64"), "3.2.25")
        self.assertEqual(client.requested, [ubuntu_url("noble", "3.4"), ubuntu_url("noble", "3.2")])

    async def test_index_of_node_arch(self):
        client = FakeExternalClient({ubuntu_url("resolute", "3.4", "arm64"): PPA_INDEX})
        self.use_client(client)
        self.assertEqual(await haproxy_upgrade.newest_version("ubuntu", "resolute", "arm64"), "3.4.6")
        self.assertEqual(client.requested, [ubuntu_url("resolute", "3.4", "arm64")])

    async def test_no_build_for_release(self):
        self.use_client(FakeExternalClient({}))
        self.assertIsNone(await haproxy_upgrade.newest_version("ubuntu", "noble", "amd64"))

    async def test_unsupported_distro_makes_no_requests(self):
        client = FakeExternalClient({})
        self.use_client(client)
        self.assertIsNone(await haproxy_upgrade.newest_version("centos", "stream9", "amd64"))
        self.assertEqual(client.requested, [])

    async def test_network_error_is_retried_later_not_forever(self):
        self.use_client(FakeExternalClient({}, error=httpx.ConnectError("blocked")))
        self.assertIsNone(await haproxy_upgrade.newest_version("ubuntu", "noble", "amd64"))
        expires, _ = haproxy_upgrade._release_cache[("ubuntu", "noble", "amd64")]
        self.assertLessEqual(expires - asyncio.get_running_loop().time(), haproxy_upgrade.PROBE_RETRY_SECONDS + 1)

    async def test_broken_index_counts_as_probe_error(self):
        client = FakeExternalClient({})
        client.get = mock.AsyncMock(return_value=mock.Mock(status_code=200, content=b"not gzip"))
        self.use_client(client)
        self.assertIsNone(await haproxy_upgrade.newest_version("ubuntu", "noble", "amd64"))
        expires, _ = haproxy_upgrade._release_cache[("ubuntu", "noble", "amd64")]
        self.assertLessEqual(expires - asyncio.get_running_loop().time(), haproxy_upgrade.PROBE_RETRY_SECONDS + 1)

    async def test_slow_network_does_not_block_the_page(self):
        self.use_client(FakeExternalClient({ubuntu_url("resolute", "3.4"): PPA_INDEX}, delay=0.3))
        with mock.patch.object(haproxy_upgrade, "PROBE_WAIT_SECONDS", 0.05):
            self.assertIsNone(await haproxy_upgrade.newest_version("ubuntu", "resolute", "amd64"))
        await asyncio.sleep(0.8)
        self.assertEqual(await haproxy_upgrade.newest_version("ubuntu", "resolute", "amd64"), "3.4.6")


class DescribeHaproxyTests(unittest.IsolatedAsyncioTestCase):
    async def describe(self, info, newest="3.4.6"):
        probe = mock.AsyncMock(return_value=newest)
        with mock.patch.object(haproxy_upgrade, "newest_version", probe):
            return await haproxy_upgrade.describe_haproxy(info), probe

    def info(self, version, arch="amd64"):
        return {"version": version, "os_id": "ubuntu", "os_codename": "resolute", "arch": arch}

    async def test_fix_inside_branch_is_offered(self):
        result, _ = await self.describe(self.info("3.4.4-1ppa1~resolute"))
        self.assertEqual(result, {"version": "3.4.4-1ppa1~resolute", "target_version": "3.4.6"})

    async def test_newer_branch_is_offered(self):
        result, _ = await self.describe(self.info("3.2.9-1ubuntu2.2"))
        self.assertEqual(result["target_version"], "3.4.6")

    async def test_up_to_date_or_newer_has_no_target(self):
        for version in ("3.4.6-1ppa1~resolute", "3.5.0-1"):
            result, _ = await self.describe(self.info(version))
            self.assertIsNone(result["target_version"])

    async def test_node_without_arch_falls_back_to_amd64(self):
        _, probe = await self.describe(self.info("3.4.4", arch=None))
        probe.assert_awaited_once_with("ubuntu", "resolute", "amd64")

    async def test_node_without_haproxy_info(self):
        result, _ = await self.describe(None)
        self.assertIsNone(result)

    async def test_unknown_release_has_no_target(self):
        result, _ = await self.describe({"version": "2.8.16", "os_id": None, "os_codename": None})
        self.assertIsNone(result["target_version"])


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
