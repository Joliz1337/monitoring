"""Тесты образа апдейтера (app/services/updater_image.py).

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Docker Hub из части сетей рвёт TLS-рукопожатие через раз: обновление не должно
падать с первой осечки — сначала зеркала и повторные попытки, потом ошибка.
"""

import os
import sys
import unittest

from docker.errors import APIError, ImageNotFound

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.updater_image import (  # noqa: E402
    PULL_ATTEMPTS,
    UPDATER_IMAGE,
    UPDATER_IMAGE_SOURCES,
    UpdaterImageUnavailable,
    ensure_updater_image,
)

DOCKER_HUB, GCR_MIRROR, ECR_MIRROR = UPDATER_IMAGE_SOURCES
TLS_TIMEOUT = "net/http: TLS handshake timeout"


class FakeImage:
    def __init__(self, images: "FakeImages"):
        self._images = images

    def tag(self, repository: str, tag: str) -> bool:
        self._images.tags.append(f"{repository}:{tag}")
        return True


class FakeImages:
    """Реестры: из reachable образ скачивается, но первые failing_pulls скачиваний рвутся."""

    def __init__(self, reachable: set[str], failing_pulls: int = 0, local: bool = False):
        self.reachable = reachable
        self.failing_pulls = failing_pulls
        self.local = local
        self.pulls: list[str] = []
        self.tags: list[str] = []

    def get(self, name: str) -> FakeImage:
        if not self.local:
            raise ImageNotFound(f"No such image: {name}")
        return FakeImage(self)

    def pull(self, source: str) -> FakeImage:
        self.pulls.append(source)
        if source not in self.reachable or len(self.pulls) <= self.failing_pulls:
            raise APIError("500 Server Error", explanation=TLS_TIMEOUT)
        return FakeImage(self)


class FakeClient:
    def __init__(self, images: FakeImages):
        self.images = images


async def ensure(images: FakeImages) -> None:
    await ensure_updater_image(FakeClient(images), retry_delay=0)


class EnsureUpdaterImageTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_image_needs_no_network(self):
        images = FakeImages(reachable=set(), local=True)
        await ensure(images)
        self.assertEqual(images.pulls, [])

    async def test_docker_hub_image_keeps_its_name(self):
        images = FakeImages(reachable={DOCKER_HUB})
        await ensure(images)
        self.assertEqual(images.pulls, [DOCKER_HUB])
        self.assertEqual(images.tags, [])

    async def test_mirror_image_is_tagged_as_docker_cli(self):
        images = FakeImages(reachable={GCR_MIRROR})
        await ensure(images)
        self.assertEqual(images.pulls, [DOCKER_HUB, GCR_MIRROR])
        self.assertEqual(images.tags, [UPDATER_IMAGE])

    async def test_second_attempt_after_all_sources_failed(self):
        images = FakeImages(reachable={DOCKER_HUB}, failing_pulls=len(UPDATER_IMAGE_SOURCES))
        await ensure(images)
        self.assertEqual(images.pulls, [*UPDATER_IMAGE_SOURCES, DOCKER_HUB])

    async def test_gives_up_after_all_attempts(self):
        images = FakeImages(reachable=set())
        with self.assertRaises(UpdaterImageUnavailable) as raised:
            await ensure(images)
        self.assertEqual(len(images.pulls), PULL_ATTEMPTS * len(UPDATER_IMAGE_SOURCES))
        self.assertEqual(set(raised.exception.failures), set(UPDATER_IMAGE_SOURCES))
        # В итоге попытки — суть ошибки демона, а не URL его сокета
        self.assertIn(f"{DOCKER_HUB}: {TLS_TIMEOUT}", str(raised.exception))
        self.assertNotIn("500 Server Error", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
