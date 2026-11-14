"""Сборка общего белого списка не держит event loop (app/services/blocklist_manager.py).

Сортировка и нормализация адресов всего парка — чистый CPU: на 2000 нод одна
сборка занимала десятую долю секунды, а при раскатке сторожа анти-DDoS список
собирался на каждую ноду, и API панели замирал на минуты. Слияние уходит в
поток; сам список при этом тот же.

Голый unittest, без PostgreSQL и без сети.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import blocklist_manager as bm  # noqa: E402
from app.services.ping_block import PingBlockScope  # noqa: E402


class FakeSession:
    def __init__(self, servers: list) -> None:
        self._servers = servers

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def execute(self, statement):
        return SimpleNamespace(all=lambda: list(self._servers))


class AllowPolicyMergeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        servers = [SimpleNamespace(id=1, url="https://5.9.0.10:9100", folder=None, dedicated_cert=False)]
        url_lists = SimpleNamespace(collect=mock.AsyncMock(return_value=["104.16.0.0/13"]))
        for target, replacement in (
            ("async_session", lambda: FakeSession(servers)),
            ("collect_node_ips", mock.AsyncMock(return_value={"5.9.0.10", "5.9.0.11"})),
            ("resolve_panel_ip", mock.AsyncMock(return_value="9.9.9.9")),
            ("load_scope", mock.AsyncMock(return_value=PingBlockScope())),
            ("get_allowlist_sources", lambda: url_lists),
        ):
            patcher = mock.patch.object(bm, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.manager = bm.BlocklistManager()
        self.manager.get_global_rules = mock.AsyncMock(
            side_effect=lambda db, direction, list_type: ["203.0.113.5/32"] if direction == "in" else []
        )
        self.merge_threads: list[int] = []
        deduplicate = self.manager.deduplicate_ips

        def recording_deduplicate(ips: list[str]) -> list[str]:
            self.merge_threads.append(threading.get_ident())
            return deduplicate(ips)

        self.manager.deduplicate_ips = recording_deduplicate

    async def test_addresses_are_merged_off_the_event_loop_thread(self):
        await self.manager.build_allow_policy()

        self.assertEqual(len(self.merge_threads), len(bm.DIRECTIONS))
        self.assertNotIn(threading.get_ident(), self.merge_threads)

    async def test_merged_list_is_sorted_normalised_and_split_by_direction(self):
        policy = await self.manager.build_allow_policy()

        self.assertEqual(policy.ips, {
            "in": ["104.16.0.0/13", "203.0.113.5", "5.9.0.10", "5.9.0.11", "9.9.9.9"],
            "out": ["5.9.0.10", "5.9.0.11", "9.9.9.9"],
        })


if __name__ == "__main__":
    unittest.main()
