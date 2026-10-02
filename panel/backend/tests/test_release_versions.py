"""Последние версии с GitHub для страницы «Обновления» и значка в меню:
сравнение «новее» вместо «не равно» и общий кэш, привязанный к каналу.

Голый unittest, без PostgreSQL и без сети.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.routers import system as system_router
    from app.services import release_versions, update_channel
except ImportError as e:  # рантайм панели не установлен
    raise unittest.SkipTest(f"release_versions requires the panel runtime: {e}")


class IsNewerTest(unittest.TestCase):
    def test_compares_numerically(self):
        self.assertTrue(release_versions.is_newer("10.10.0", "10.9.3"))
        self.assertFalse(release_versions.is_newer("10.9.3", "10.10.0"))

    def test_same_version_is_not_newer(self):
        self.assertFalse(release_versions.is_newer("10.74.0", "10.74.0"))

    def test_older_release_after_channel_switch_is_not_an_update(self):
        self.assertFalse(release_versions.is_newer("10.73.0", "10.74.0"))

    def test_unknown_side_is_not_newer(self):
        self.assertFalse(release_versions.is_newer(None, "10.74.0"))
        self.assertFalse(release_versions.is_newer("10.74.0", None))

    def test_unknown_panel_version_never_offers_update(self):
        self.assertFalse(system_router._panel_update_available("unknown", "10.75.0"))
        self.assertTrue(system_router._panel_update_available("10.74.0", "10.75.0"))


class LatestVersionsCacheTest(unittest.TestCase):
    def setUp(self):
        release_versions._cached = None
        self.urls: list[str] = []
        self.answer: str | None = "10.75.0"
        self.clock = 1000.0

    def tearDown(self):
        release_versions._cached = None
        update_channel.set_current_branch(update_channel.STABLE_BRANCH)

    def fetch(self):
        async def fake_fetch(url):
            self.urls.append(url)
            return self.answer

        with patch.object(release_versions, "_fetch_version_file", fake_fetch), \
                patch.object(release_versions.time, "monotonic", lambda: self.clock):
            return asyncio.run(release_versions.latest_versions())

    def test_second_call_within_ttl_uses_cache(self):
        self.fetch()
        self.fetch()
        self.assertEqual(len(self.urls), 3)

    def test_refetches_after_ttl(self):
        self.fetch()
        self.clock += release_versions.LATEST_VERSIONS_TTL_SEC + 1
        self.fetch()
        self.assertEqual(len(self.urls), 6)

    def test_failed_fetch_retries_soon(self):
        self.answer = None
        self.fetch()
        self.clock += release_versions.LATEST_VERSIONS_FAILED_TTL_SEC + 1
        self.fetch()
        self.assertEqual(len(self.urls), 6)

    def test_channel_switch_drops_cache(self):
        self.fetch()
        update_channel.set_current_branch(update_channel.DEV_BRANCH)
        versions = self.fetch()
        self.assertEqual(len(self.urls), 6)
        self.assertTrue(all("/dev/" in url for url in self.urls[3:]))
        self.assertEqual(versions.panel, "10.75.0")


if __name__ == "__main__":
    unittest.main()
