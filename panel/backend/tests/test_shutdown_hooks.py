"""Тесты хуков, которые панель прогоняет перед остановкой на обновление.

Скрипт обновления ждёт ответа и только потом останавливает контейнеры, поэтому
сбой одного хука не должен ни отменять остальные, ни ронять сам вызов.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import shutdown_hooks  # noqa: E402


class ShutdownHooksTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved_hooks = list(shutdown_hooks._hooks)
        shutdown_hooks._hooks.clear()

    def tearDown(self):
        shutdown_hooks._hooks[:] = self._saved_hooks

    async def test_hooks_run_concurrently(self):
        """Хуки ждут каждый своё — последовательный прогон сложил бы их ожидания."""
        both_started = asyncio.Event()
        started = 0

        async def hook():
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)

        shutdown_hooks.register_shutdown_hook(hook)
        shutdown_hooks.register_shutdown_hook(hook)
        with self.assertNoLogs(shutdown_hooks.logger, level="ERROR"):
            await shutdown_hooks.run_shutdown_hooks()

    async def test_failing_hook_does_not_stop_others(self):
        finished = []

        async def failing():
            raise RuntimeError("boom")

        async def working():
            finished.append(True)

        shutdown_hooks.register_shutdown_hook(failing)
        shutdown_hooks.register_shutdown_hook(working)
        with self.assertLogs(shutdown_hooks.logger, level="ERROR"):
            await shutdown_hooks.run_shutdown_hooks()
        self.assertEqual(finished, [True])

    async def test_no_hooks(self):
        await shutdown_hooks.run_shutdown_hooks()


if __name__ == "__main__":
    unittest.main()
