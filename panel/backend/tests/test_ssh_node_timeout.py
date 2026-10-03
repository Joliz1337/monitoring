"""Запрос к ноде из SSH-безопасности не висит дольше своего потолка.

Нода за ТСПУ/SOCKS5 может не ответить вовсе, а рукопожатие SOCKS5 не входит
в таймауты httpx. Панель обязана вернуть «нода не ответила» раньше, чем браузер
оборвёт запрос сам, — иначе страница остаётся пустой без объяснений.
"""

import asyncio
import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.routers import ssh_security
    from app.services import ssh_manager
except ImportError as e:  # pragma: no cover
    raise unittest.SkipTest(f"ssh-security requires the panel runtime: {e}")

ROOT = Path(__file__).resolve().parents[3]
FRONTEND_CLIENT = ROOT / "panel" / "frontend" / "src" / "api" / "client.ts"


class _HangingClient:
    async def request(self, **_kwargs):
        await asyncio.sleep(3600)


def _server():
    return SimpleNamespace(id=1, name="tspu-node", url="https://203.0.113.1:9100")


class ProxyHardTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_hanging_node_raises_timeout_instead_of_hanging(self):
        with (
            mock.patch.object(ssh_manager, "server_allows_path", return_value=(True, None, False)),
            mock.patch.object(ssh_manager, "get_node_client", return_value=_HangingClient()),
            mock.patch.object(ssh_manager, "node_auth_headers", return_value={}),
            mock.patch.object(ssh_manager, "HARD_TIMEOUT_MARGIN", 0.05),
        ):
            task = asyncio.create_task(
                ssh_manager.proxy_to_node(_server(), "GET", "/api/ssh/config", timeout=0.05)
            )
            done, _ = await asyncio.wait({task}, timeout=2)
            if task not in done:
                task.cancel()
                self.fail("proxy_to_node завис на неотвечающей ноде")
            with self.assertRaises(TimeoutError):
                task.result()


class ReadDeadlineTests(unittest.TestCase):
    def test_panel_answers_before_browser_gives_up(self):
        match = re.search(r"DEFAULT_TIMEOUT_MS\s*=\s*(\d+)", FRONTEND_CLIENT.read_text(encoding="utf-8"))
        self.assertIsNotNone(match, "не найден DEFAULT_TIMEOUT_MS во фронтенде")
        browser_timeout = int(match.group(1)) / 1000
        panel_worst_case = ssh_security._READ_TIMEOUT + ssh_manager.HARD_TIMEOUT_MARGIN
        self.assertLess(panel_worst_case, browser_timeout)


if __name__ == "__main__":
    unittest.main()
