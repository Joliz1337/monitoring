import gzip
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import node_image_cache as nic  # noqa: E402


class FakeImage:
    def __init__(self, image_id, chunks):
        self.id = image_id
        self._chunks = chunks
        self.save_calls = 0

    def save(self, named=False):
        assert named is True  # тег обязан попасть в архив
        self.save_calls += 1
        return iter(self._chunks)


def _fake_client(image):
    client = mock.MagicMock()
    client.images.pull.return_value = image
    return client


class NodeImageCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._orig = nic.CACHE_DIR
        nic.CACHE_DIR = Path(self._dir.name)

    def tearDown(self):
        nic.CACHE_DIR = self._orig
        self._dir.cleanup()

    async def test_pull_and_save_produces_gzip_tar(self):
        image = FakeImage("sha256:abc", [b"tar-part-1", b"tar-part-2"])
        with mock.patch.object(nic.docker, "from_env", return_value=_fake_client(image)):
            path = await nic.ensure_image("latest")
        self.assertTrue(path.exists())
        with gzip.open(path, "rb") as gz:
            self.assertEqual(gz.read(), b"tar-part-1tar-part-2")
        self.assertEqual(image.save_calls, 1)

    async def test_second_call_same_digest_skips_save(self):
        image = FakeImage("sha256:same", [b"x"])
        with mock.patch.object(nic.docker, "from_env", return_value=_fake_client(image)):
            await nic.ensure_image("dev")
            await nic.ensure_image("dev")
        self.assertEqual(image.save_calls, 1)  # второй раз digest совпал — не пересохраняем

    async def test_changed_digest_resaves(self):
        first = FakeImage("sha256:v1", [b"one"])
        with mock.patch.object(nic.docker, "from_env", return_value=_fake_client(first)):
            await nic.ensure_image("latest")
        second = FakeImage("sha256:v2", [b"two"])
        with mock.patch.object(nic.docker, "from_env", return_value=_fake_client(second)):
            path = await nic.ensure_image("latest")
        with gzip.open(path, "rb") as gz:
            self.assertEqual(gz.read(), b"two")
        self.assertEqual(second.save_calls, 1)


if __name__ == "__main__":
    unittest.main()
