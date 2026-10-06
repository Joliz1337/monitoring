"""Что панель доделывает перед остановкой на обновление.

Скрипт обновления вызывает POST /system/prepare-shutdown до `docker compose down`,
пока панель ещё целиком работает. Подпроцессы бэкенда при остановке контейнера
умирают по SIGKILL, поэтому модуль, которому нельзя оборваться посреди шага,
регистрирует здесь корутину, доводящую начатое до конца.
"""
import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

ShutdownHook = Callable[[], Awaitable[None]]

_hooks: list[ShutdownHook] = []


def register_shutdown_hook(hook: ShutdownHook) -> None:
    _hooks.append(hook)


async def run_shutdown_hooks() -> None:
    hooks = list(_hooks)
    results = await asyncio.gather(*(hook() for hook in hooks), return_exceptions=True)
    for hook, result in zip(hooks, results):
        if isinstance(result, Exception):
            logger.error(f"Shutdown hook {hook.__qualname__} failed: {result}")
