"""Тесты рассылки набора antiddos_allow по запросу (app/services/antiddos_manager.py).

Белый список в панели один — общий, со страницы «Блокировка IP». Его смена
должна дойти до набора antiddos_allow на нодах сразу, а не через час: одной
рассылкой на пачку правок и без рассылки, когда набор от правки не изменился.
О смене менеджер узнаёт от менеджера блок-листа (см. test_blocklist_allowlist_push.py).

Голый unittest, без PostgreSQL и без сети: сессия и обход нод подменены.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from typing import Callable
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import AntiDdosSettings, Server  # noqa: E402
from app.services import antiddos_manager  # noqa: E402
from app.services.antiddos_manager import AntiDdosManager  # noqa: E402

DEBOUNCE = 0.01


async def eventually(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("условие не наступило за отведённое время")
        await asyncio.sleep(0.002)


class RequestedPushLoopTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        patcher = mock.patch.object(antiddos_manager, "WHITELIST_PUSH_DEBOUNCE", DEBOUNCE)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = AntiDdosManager()
        self.manager._running = True
        self.push = mock.AsyncMock(return_value={})
        self.manager.push_whitelist_all = self.push
        self.loop_task = asyncio.create_task(self.manager._requested_push_loop())

    async def asyncTearDown(self) -> None:
        self.loop_task.cancel()
        await asyncio.gather(self.loop_task, return_exceptions=True)

    async def test_nothing_is_pushed_until_asked(self):
        await asyncio.sleep(DEBOUNCE * 5)

        self.push.assert_not_awaited()

    async def test_burst_of_edits_gives_one_push_of_changed_list_only(self):
        for _ in range(5):
            self.manager.request_whitelist_push()

        await eventually(lambda: self.push.await_count >= 1)
        await asyncio.sleep(DEBOUNCE * 5)

        self.push.assert_awaited_once_with(only_if_changed=True)

    async def test_edit_made_during_a_push_gets_its_own_push(self):
        release = asyncio.Event()

        async def slow_push(only_if_changed: bool = False) -> dict:
            await release.wait()
            return {}

        self.push.side_effect = slow_push
        self.manager.request_whitelist_push()
        await eventually(lambda: self.push.await_count == 1)

        self.manager.request_whitelist_push()
        release.set()

        await eventually(lambda: self.push.await_count == 2)

    async def test_failed_push_does_not_stop_later_ones(self):
        self.push.side_effect = [RuntimeError("db gone"), {}]

        self.manager.request_whitelist_push()
        await eventually(lambda: self.push.await_count == 1)
        self.manager.request_whitelist_push()

        await eventually(lambda: self.push.await_count == 2)


class FakeResult:
    def __init__(self, entities: list) -> None:
        self._entities = entities

    def scalars(self) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: list(self._entities))

    def scalar_one_or_none(self):
        return self._entities[0] if self._entities else None


class FakeSession:
    def __init__(self, rows: dict[type, list]) -> None:
        self._rows = rows

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def execute(self, statement) -> FakeResult:
        return FakeResult(self._rows.get(statement.column_descriptions[0]["entity"], []))

    async def commit(self) -> None:
        return None


class PushOnlyIfChangedTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        rows = {
            Server: [Server(id=1, name="nl-1", url="https://5.9.0.10:9100")],
            AntiDdosSettings: [AntiDdosSettings()],
        }
        for name, replacement in (
            ("async_session", lambda: FakeSession(rows)),
            ("enqueue", mock.AsyncMock()),
            ("is_server_online", mock.Mock(return_value=True)),
            ("server_allows", mock.Mock(return_value=True)),
        ):
            patcher = mock.patch.object(antiddos_manager, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manager = AntiDdosManager()
        self.whitelist = ["104.16.0.0/13", "5.9.0.10"]
        self.manager.build_whitelist = mock.AsyncMock(side_effect=lambda db: list(self.whitelist))
        self.fan_out = mock.AsyncMock(return_value={1: None})
        self.manager._push_to_servers = self.fan_out

    async def test_same_list_is_not_sent_to_the_fleet_again(self):
        await self.manager.push_whitelist_all(only_if_changed=True)
        skipped = await self.manager.push_whitelist_all(only_if_changed=True)

        self.fan_out.assert_awaited_once()
        self.assertEqual(skipped["nodes"], 0)

    async def test_changed_list_is_sent(self):
        await self.manager.push_whitelist_all(only_if_changed=True)
        self.whitelist.append("198.51.100.7")

        await self.manager.push_whitelist_all(only_if_changed=True)

        self.assertEqual(self.fan_out.await_count, 2)
        self.assertEqual(self.fan_out.await_args.args[1], ["104.16.0.0/13", "5.9.0.10", "198.51.100.7"])

    async def test_hourly_and_button_push_goes_out_even_if_nothing_changed(self):
        await self.manager.push_whitelist_all()
        await self.manager.push_whitelist_all()

        self.assertEqual(self.fan_out.await_count, 2)

    async def test_list_sent_by_the_hourly_push_is_not_repeated_on_request(self):
        await self.manager.push_whitelist_all()

        await self.manager.push_whitelist_all(only_if_changed=True)

        self.fan_out.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
