"""Разбор /etc/os-release: по ID и кодовому имени панель ищет сборку HAProxy."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import haproxy_info  # noqa: E402
from app.services.haproxy_info import parse_os_release  # noqa: E402

UBUNTU_NOBLE = """PRETTY_NAME="Ubuntu 24.04.3 LTS"
NAME="Ubuntu"
VERSION_ID="24.04"
VERSION_CODENAME=noble
ID=ubuntu
ID_LIKE=debian
"""


class ParseOsReleaseTests(unittest.TestCase):
    def test_ubuntu(self):
        fields = parse_os_release(UBUNTU_NOBLE)
        self.assertEqual(fields["ID"], "ubuntu")
        self.assertEqual(fields["VERSION_CODENAME"], "noble")
        self.assertEqual(fields["PRETTY_NAME"], "Ubuntu 24.04.3 LTS")

    def test_single_quotes_and_garbage_lines(self):
        fields = parse_os_release("ID='debian'\n\n# comment\nbroken line\nVERSION_CODENAME=trixie\n")
        self.assertEqual(fields, {"ID": "debian", "VERSION_CODENAME": "trixie"})

    def test_empty(self):
        self.assertEqual(parse_os_release(""), {})


class ReadHaproxyInfoTests(unittest.IsolatedAsyncioTestCase):
    """Архитектура нужна панели, чтобы смотреть индекс пакетов своей сборки (arm64 и amd64)."""

    async def read(self, dpkg_result):
        executor = mock.Mock(execute=mock.AsyncMock(return_value=dpkg_result))
        manager = mock.Mock(installed_version=mock.Mock(return_value="3.4.4-1ppa1~resolute"))
        with mock.patch.object(haproxy_info, "get_host_executor", return_value=executor), \
                mock.patch.object(haproxy_info, "get_haproxy_manager", return_value=manager), \
                mock.patch.object(haproxy_info, "read_host_file", mock.AsyncMock(return_value=UBUNTU_NOBLE)):
            return await haproxy_info.read_haproxy_info()

    async def test_full_info(self):
        info = await self.read(mock.Mock(success=True, stdout="arm64\n"))
        self.assertEqual(info, {
            "version": "3.4.4-1ppa1~resolute",
            "os_id": "ubuntu",
            "os_codename": "noble",
            "arch": "arm64",
        })

    async def test_arch_unknown_when_dpkg_fails(self):
        info = await self.read(mock.Mock(success=False, stdout=""))
        self.assertIsNone(info["arch"])


if __name__ == "__main__":
    unittest.main()
