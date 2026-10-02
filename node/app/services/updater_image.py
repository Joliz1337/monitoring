"""Образ апдейтера ноды (docker:cli): с Docker Hub, а если он не отвечает — с зеркал.

Без этого образа обновление не начинается вовсе, а Docker Hub из части сетей
(VK Cloud, серверы за ТСПУ) рвёт TLS-рукопожатие — причём через раз, повторный
запуск обычно проходит. Поэтому каждая попытка перебирает Docker Hub и зеркала
с тем же официальным образом, а попыток несколько. Образ с зеркала получает
привычное имя: следующие обновления найдут его на диске и в сеть не пойдут.
"""

import asyncio
import logging

from docker import DockerClient
from docker.errors import DockerException, ImageNotFound
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

UPDATER_REPOSITORY = "docker"
UPDATER_TAG = "cli"
UPDATER_IMAGE = f"{UPDATER_REPOSITORY}:{UPDATER_TAG}"
# Только реестры Google и AWS: апдейтер работает privileged, с сокетом докера и
# PID хоста, и образ из случайного зеркала был бы root-доступом к серверу
UPDATER_IMAGE_SOURCES = (
    UPDATER_IMAGE,
    "mirror.gcr.io/library/docker:cli",
    "public.ecr.aws/docker/library/docker:cli",
)
PULL_ATTEMPTS = 3
PULL_RETRY_DELAY = 10


class UpdaterImageUnavailable(Exception):
    def __init__(self, failures: dict[str, str]):
        self.failures = failures
        details = "; ".join(f"{source}: {error}" for source, error in failures.items())
        super().__init__(
            f"Updater image {UPDATER_IMAGE} was not pulled from Docker Hub or its mirrors "
            f"in {PULL_ATTEMPTS} attempts: {details}"
        )


def _error_text(exc: Exception) -> str:
    # У ошибки демона суть — в explanation, str() добавляет к ней URL сокета
    return getattr(exc, "explanation", None) or str(exc) or type(exc).__name__


async def _pull(client: DockerClient, source: str) -> None:
    image = await asyncio.to_thread(client.images.pull, source)
    if source != UPDATER_IMAGE:
        await asyncio.to_thread(image.tag, UPDATER_REPOSITORY, UPDATER_TAG)


async def ensure_updater_image(client: DockerClient, retry_delay: float = PULL_RETRY_DELAY) -> None:
    try:
        await asyncio.to_thread(client.images.get, UPDATER_IMAGE)
        return
    except ImageNotFound:
        pass

    failures: dict[str, str] = {}
    for attempt in range(1, PULL_ATTEMPTS + 1):
        if attempt > 1:
            await asyncio.sleep(retry_delay)
        for source in UPDATER_IMAGE_SOURCES:
            try:
                await _pull(client, source)
            except (DockerException, RequestException) as e:
                failures[source] = _error_text(e)
                logger.warning(
                    f"Updater image pull failed (attempt {attempt}/{PULL_ATTEMPTS}, {source}): {failures[source]}"
                )
                continue
            logger.info(f"Updater image pulled from {source}")
            return
    raise UpdaterImageUnavailable(failures)
