"""Тесты набора antiddos_allow (app/services/antiddos_manager.py).

Своего белого списка у анти-DDoS нет. Аварийный режим не ограничивает входящую
часть общего белого списка панели — адреса нод и панели, ручные записи, списки
по URL; её собирает BlocklistManager.build_allow_policy — и точки GRE-туннелей
DDoS-Guard. Ручной список прежних версий при старте переезжает в общий.

Голый unittest, без PostgreSQL и без сети: сессия и менеджер блок-листа подменены.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.exc import OperationalError  # noqa: E402

from app.models import AntiDdosSettings  # noqa: E402
from app.services import antiddos_manager  # noqa: E402
from app.services.antiddos_manager import MOVED_RULE_COMMENT, AntiDdosManager  # noqa: E402
from app.services.blocklist_manager import BlocklistManager  # noqa: E402


class FakeResult:
    def __init__(self, entities: list) -> None:
        self._entities = entities

    def scalars(self) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: list(self._entities))

    def scalar_one_or_none(self):
        return self._entities[0] if self._entities else None


class FakeSession:
    """Сессия без базы: отдаёт заготовленные строки по сущности запроса и копит добавленное."""

    def __init__(self, rows: dict[type, list]) -> None:
        self._rows = rows
        self.added: list = []
        self.commits = 0

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def execute(self, statement) -> FakeResult:
        return FakeResult(self._rows.get(statement.column_descriptions[0]["entity"], []))

    def add_all(self, entities: list) -> None:
        self.added.extend(entities)

    async def commit(self) -> None:
        self.commits += 1


def blocklist_with(incoming: list[str], outgoing: list[str] = ()) -> BlocklistManager:
    """Настоящий менеджер блок-листа (нормализация адресов) с подменённой сборкой белого списка."""
    blocklist = BlocklistManager()
    blocklist.build_allow_policy = mock.AsyncMock(
        return_value=SimpleNamespace(ips={"in": list(incoming), "out": list(outgoing)})
    )
    blocklist.request_allowlist_push = mock.Mock()
    return blocklist


def patch_module(case: unittest.TestCase, **replacements) -> None:
    for name, replacement in replacements.items():
        patcher = mock.patch.object(antiddos_manager, name, replacement)
        patcher.start()
        case.addCleanup(patcher.stop)


class WhitelistBuildTest(unittest.IsolatedAsyncioTestCase):
    async def build(self, incoming: list[str], outgoing: list[str] = (), gre: set[str] = frozenset()) -> list[str]:
        patch_module(
            self,
            get_blocklist_manager=lambda: blocklist_with(incoming, outgoing),
            gre_whitelist_entries=mock.Mock(return_value=set(gre)),
        )
        return await AntiDdosManager().build_whitelist(FakeSession({}))

    async def test_incoming_part_of_the_shared_allowlist_is_not_limited(self):
        whitelist = await self.build(["5.9.0.10", "104.16.0.0/13", "203.0.113.5"])

        self.assertEqual(whitelist, ["104.16.0.0/13", "203.0.113.5", "5.9.0.10"])

    async def test_outgoing_entries_are_not_taken(self):
        whitelist = await self.build(["5.9.0.10"], outgoing=["8.8.8.8"])

        self.assertNotIn("8.8.8.8", whitelist)

    async def test_ipv6_is_left_out(self):
        # Набор antiddos_allow на ноде — семейство inet
        whitelist = await self.build(["2606:4700::/32", "104.16.0.0/13"])

        self.assertEqual(whitelist, ["104.16.0.0/13"])

    async def test_ddos_guard_tunnel_endpoints_are_added(self):
        whitelist = await self.build(["5.9.0.10"], gre={"186.2.160.1", "186.2.168.0/24"})

        self.assertEqual(whitelist, ["186.2.160.1", "186.2.168.0/24", "5.9.0.10"])


class ManualListMoveTest(unittest.IsolatedAsyncioTestCase):
    """Ручной белый список анти-DDoS прежних версий переезжает в общий."""

    def prepare(self, stored: object, already_shared: list[str] = ()) -> None:
        self.settings = AntiDdosSettings(user_cidrs=stored)
        self.session = FakeSession({AntiDdosSettings: [self.settings]})
        self.blocklist = blocklist_with([])
        self.blocklist.get_global_rules = mock.AsyncMock(return_value=list(already_shared))
        patch_module(self, async_session=lambda: self.session, get_blocklist_manager=lambda: self.blocklist)
        self.manager = AntiDdosManager()

    async def test_addresses_become_incoming_entries_of_the_shared_allowlist(self):
        self.prepare('["173.245.48.0/20", "5.9.0.10"]')

        await self.manager._move_manual_list_to_shared()

        self.assertEqual(
            [(r.ip_cidr, r.direction, r.list_type, r.server_id, r.comment) for r in self.session.added],
            [
                ("173.245.48.0/20", "in", "allow", None, MOVED_RULE_COMMENT),
                ("5.9.0.10", "in", "allow", None, MOVED_RULE_COMMENT),
            ],
        )
        self.blocklist.get_global_rules.assert_awaited_once_with(self.session, "in", list_type="allow")

    async def test_old_list_is_emptied_and_nodes_get_the_new_allowlist(self):
        self.prepare('["5.9.0.10"]')

        await self.manager._move_manual_list_to_shared()

        self.assertIsNone(self.settings.user_cidrs)
        self.assertEqual(self.session.commits, 1)
        self.blocklist.request_allowlist_push.assert_called_once_with()

    async def test_address_already_in_the_shared_allowlist_is_not_added_twice(self):
        self.prepare('["5.9.0.10", "173.245.48.0/20"]', already_shared=["5.9.0.10"])

        await self.manager._move_manual_list_to_shared()

        self.assertEqual([r.ip_cidr for r in self.session.added], ["173.245.48.0/20"])

    async def test_addresses_are_written_the_way_the_shared_allowlist_stores_them(self):
        self.prepare('["5.9.0.10/32", " 173.245.48.7/20 ", "5.9.0.10", "not-an-address"]')

        await self.manager._move_manual_list_to_shared()

        self.assertEqual([r.ip_cidr for r in self.session.added], ["5.9.0.10", "173.245.48.0/20"])

    async def test_empty_old_list_changes_nothing(self):
        for stored in (None, ""):
            with self.subTest(stored=stored):
                self.prepare(stored)

                await self.manager._move_manual_list_to_shared()

                self.assertEqual(self.session.added, [])
                self.assertEqual(self.session.commits, 0)
                self.blocklist.request_allowlist_push.assert_not_called()

    async def test_unreadable_old_list_is_dropped(self):
        for stored in ("{broken", '{"not": "a list"}'):
            with self.subTest(stored=stored):
                self.prepare(stored)

                await self.manager._move_manual_list_to_shared()

                self.assertEqual(self.session.added, [])
                self.assertIsNone(self.settings.user_cidrs)


class StartTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.blocklist = mock.Mock()
        patch_module(self, get_blocklist_manager=lambda: self.blocklist)
        self.manager = AntiDdosManager()
        for loop in ("_whitelist_loop", "_status_loop", "_requested_push_loop"):
            setattr(self.manager, loop, mock.AsyncMock())
        self.manager._move_manual_list_to_shared = mock.AsyncMock()
        self.addAsyncCleanup(self.manager.stop)

    async def test_old_manual_list_is_moved_on_every_start(self):
        await self.manager.start()

        self.manager._move_manual_list_to_shared.assert_awaited_once_with()

    async def test_allowlist_change_asks_for_a_push_of_the_antiddos_set(self):
        await self.manager.start()

        self.blocklist.add_allowlist_listener.assert_called_once_with(self.manager.request_whitelist_push)

    async def test_database_failure_during_the_move_does_not_stop_the_manager(self):
        self.manager._move_manual_list_to_shared.side_effect = OperationalError("select", {}, Exception("db down"))

        with self.assertLogs(antiddos_manager.logger, level="ERROR"):
            await self.manager.start()

        self.blocklist.add_allowlist_listener.assert_called_once()
        self.manager._whitelist_loop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
