"""Тесты выбора версии ядра и проверки целостности загрузки.

Голый unittest, без сети — ответ GitHub подменяется моком.

Бинарник ядра панель запускает у себя, поэтому подмена архива означает
выполнение чужого кода. Отсюда правило, которое здесь и проверяется: через
недоверенное зеркало качаются только версии с известным хэшем.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.xray_test import core_manager, core_registry  # noqa: E402
from app.services.xray_test.core_registry import LATEST, ReleaseInfo  # noqa: E402
from app.services.xray_test.errors import CoreDownloadError  # noqa: E402
from app.services.xray_test.models import Core  # noqa: E402


def _release(version, *, prerelease=False, digest=True, available=True, size=None):
    return ReleaseInfo(
        version=version,
        tag=f"v{version}",
        prerelease=prerelease,
        published_at="2026-08-01T00:00:00Z",
        asset_url=f"https://github.com/x/releases/download/v{version}/core.zip" if available else None,
        asset_name="core.zip" if available else None,
        asset_size=size,
        digest_url=f"https://github.com/x/releases/download/v{version}/core.zip.dgst" if digest else None,
    )


class ResolveVersionTest(unittest.IsolatedAsyncioTestCase):
    async def _resolve(self, selected, releases):
        with mock.patch.object(
            core_registry, "list_releases", new=mock.AsyncMock(return_value=releases)
        ):
            return await core_registry.resolve_version(Core.XRAY, selected)

    async def test_latest_takes_prerelease(self):
        """Пре-релиз — обычно единственное место, где уже есть новый транспорт."""
        releases = [_release("26.7.28", prerelease=True), _release("26.3.27")]
        self.assertEqual((await self._resolve(LATEST, releases)).version, "26.7.28")

    async def test_explicit_version_selected(self):
        releases = [_release("26.7.28", prerelease=True), _release("26.3.27")]
        self.assertEqual((await self._resolve("26.3.27", releases)).version, "26.3.27")

    async def test_version_with_v_prefix_accepted(self):
        releases = [_release("26.3.27")]
        self.assertEqual((await self._resolve("v26.3.27", releases)).version, "26.3.27")

    async def test_unknown_version_rejected(self):
        with self.assertRaises(CoreDownloadError):
            await self._resolve("1.2.3", [_release("26.3.27")])

    async def test_releases_without_asset_skipped(self):
        releases = [_release("26.7.28", available=False), _release("26.3.27")]
        self.assertEqual((await self._resolve(LATEST, releases)).version, "26.3.27")

    async def test_no_builds_at_all_rejected(self):
        with self.assertRaises(CoreDownloadError):
            await self._resolve(LATEST, [_release("26.7.28", available=False)])


class DigestParsingTest(unittest.IsolatedAsyncioTestCase):
    async def test_sha256_extracted_from_dgst(self):
        body = (
            "MD5= ee4e2ff74948a9b464624b1cabc44409\n"
            "SHA1= b55b06e74e89083b9cedfdecf0d68b579cd2af72\n"
            "SHA2-256= 23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae\n"
            "SHA2-512= e8bc40a0687cac184bbe4b5c1f047e69064ccedc\n"
        )
        response = mock.Mock(text=body)
        response.raise_for_status = mock.Mock()
        client = mock.Mock(get=mock.AsyncMock(return_value=response))

        with mock.patch.object(core_registry, "get_external_client", return_value=client):
            digest = await core_registry.fetch_digest(_release("26.3.27"))

        self.assertEqual(digest, "23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae")

    async def test_release_without_dgst_gives_none(self):
        self.assertIsNone(await core_registry.fetch_digest(_release("1.14.0", digest=False)))


class DownloadIntegrityTest(unittest.IsolatedAsyncioTestCase):
    """Зеркало допустимо только там, где есть с чем сверять."""

    async def test_pinned_version_may_use_mirror(self):
        pinned = core_manager.PINNED_RELEASES[Core.XRAY]
        release = _release(pinned.version)
        fetched: list[str] = []

        async def fake_fetch(url):
            fetched.append(url)
            raise CoreDownloadError("сеть недоступна")

        with mock.patch.object(core_manager, "_fetch", new=fake_fetch), \
             mock.patch.object(core_manager, "detect_arch", return_value="amd64"), \
             mock.patch.object(core_registry, "fetch_digest", new=mock.AsyncMock(return_value=None)):
            with self.assertRaises(CoreDownloadError):
                await core_manager._download(Core.XRAY, release)

        self.assertEqual(len(fetched), 2)
        self.assertTrue(any(core_manager.GITHUB_MIRROR in url for url in fetched))

    async def test_unpinned_version_without_digest_skips_mirror(self):
        release = _release("99.9.9", digest=False)
        fetched: list[str] = []

        async def fake_fetch(url):
            fetched.append(url)
            raise CoreDownloadError("сеть недоступна")

        with mock.patch.object(core_manager, "_fetch", new=fake_fetch), \
             mock.patch.object(core_manager, "detect_arch", return_value="amd64"), \
             mock.patch.object(core_registry, "fetch_digest", new=mock.AsyncMock(return_value=None)):
            with self.assertRaises(CoreDownloadError) as ctx:
                await core_manager._download(Core.XRAY, release)

        self.assertEqual(len(fetched), 1)
        self.assertNotIn(core_manager.GITHUB_MIRROR, fetched[0])
        self.assertIn("github.com", str(ctx.exception))

    async def test_wrong_digest_rejected(self):
        release = _release("99.9.9")
        expected = "b" * 64

        with mock.patch.object(core_manager, "_fetch", new=mock.AsyncMock(return_value=b"payload")), \
             mock.patch.object(core_manager, "detect_arch", return_value="amd64"), \
             mock.patch.object(core_registry, "fetch_digest", new=mock.AsyncMock(return_value=expected)):
            with self.assertRaises(CoreDownloadError) as ctx:
                await core_manager._download(Core.XRAY, release)

        self.assertIn("контрольная сумма", str(ctx.exception))

    async def test_matching_digest_accepted(self):
        from hashlib import sha256

        payload = b"real-core-binary"
        release = _release("99.9.9")

        with mock.patch.object(core_manager, "_fetch", new=mock.AsyncMock(return_value=payload)), \
             mock.patch.object(core_manager, "detect_arch", return_value="amd64"), \
             mock.patch.object(
                 core_registry, "fetch_digest",
                 new=mock.AsyncMock(return_value=sha256(payload).hexdigest()),
             ):
            data, digest = await core_manager._download(Core.XRAY, release)

        self.assertEqual(data, payload)
        self.assertEqual(digest, sha256(payload).hexdigest())

    async def test_size_mismatch_rejected(self):
        payload = b"short"
        release = _release("99.9.9", digest=False, size=9999)

        with mock.patch.object(core_manager, "_fetch", new=mock.AsyncMock(return_value=payload)), \
             mock.patch.object(core_manager, "detect_arch", return_value="amd64"), \
             mock.patch.object(core_registry, "fetch_digest", new=mock.AsyncMock(return_value=None)):
            with self.assertRaises(CoreDownloadError) as ctx:
                await core_manager._download(Core.XRAY, release)

        self.assertIn("размер", str(ctx.exception))


class SelectedVersionTest(unittest.TestCase):
    def setUp(self):
        self._saved = {core: core_manager.selected_version(core) for core in Core}

    def tearDown(self):
        for core, value in self._saved.items():
            core_manager.set_selected_version(core, value)

    def test_default_is_latest(self):
        core_manager.set_selected_version(Core.XRAY, "")
        self.assertEqual(core_manager.selected_version(Core.XRAY), LATEST)

    def test_loaded_from_settings(self):
        core_manager.load_selected({
            core_manager.SETTING_KEYS[Core.XRAY]: "26.3.27",
            core_manager.SETTING_KEYS[Core.SINGBOX]: "1.14.0-beta.17",
        })
        self.assertEqual(core_manager.selected_version(Core.XRAY), "26.3.27")
        self.assertEqual(core_manager.selected_version(Core.SINGBOX), "1.14.0-beta.17")

    def test_empty_settings_do_not_override(self):
        core_manager.set_selected_version(Core.XRAY, "26.3.27")
        core_manager.load_selected({core_manager.SETTING_KEYS[Core.XRAY]: ""})
        self.assertEqual(core_manager.selected_version(Core.XRAY), "26.3.27")

    def test_paths_are_version_scoped(self):
        first = core_manager.binary_path(Core.XRAY, "26.3.27", "amd64")
        second = core_manager.binary_path(Core.XRAY, "26.7.28", "amd64")
        self.assertNotEqual(first, second)
        self.assertIn("26.3.27", str(first))
        self.assertEqual(first.name, "xray")


if __name__ == "__main__":
    unittest.main()
