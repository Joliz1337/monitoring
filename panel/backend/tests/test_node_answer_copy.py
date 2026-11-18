"""Копии ответов ноды для страницы сервера (app/services/node_answer_copy.py).

Голый unittest, без PostgreSQL: сессия записывает запросы вместо их выполнения.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.services import node_answer_copy  # noqa: E402
from app.services.node_answer_copy import AnswerKind, keep_copy, load_copy  # noqa: E402

SAVED_AT = datetime(2026, 8, 23, 9, 30, tzinfo=timezone.utc)


class RecordingSession:
    def __init__(self, row=None, failure: Exception | None = None) -> None:
        self._row = row
        self._failure = failure
        self.statements: list = []
        self.commit = mock.AsyncMock()
        self.rollback = mock.AsyncMock()

    async def execute(self, statement):
        if self._failure is not None:
            raise self._failure
        self.statements.append(statement)
        return SimpleNamespace(one_or_none=lambda: self._row)


def stored(data: str) -> SimpleNamespace:
    return SimpleNamespace(data=data, saved_at=SAVED_AT)


class KeepCopyTest(unittest.IsolatedAsyncioTestCase):
    async def test_answer_replaces_the_previous_copy_of_the_same_kind(self):
        db = RecordingSession()

        await keep_copy(db, 7, AnswerKind.BANDWIDTH_LIMIT, {"enabled": True, "mbit": 500})

        compiled = db.statements[0].compile(dialect=postgresql.dialect())
        self.assertIn("ON CONFLICT (server_id, kind) DO UPDATE", str(compiled))
        self.assertEqual(compiled.params["server_id"], 7)
        self.assertEqual(compiled.params["kind"], "bandwidth_limit")
        self.assertEqual(json.loads(compiled.params["data"]), {"enabled": True, "mbit": 500})
        db.commit.assert_awaited_once()

    async def test_copy_is_stamped_with_a_timezone(self):
        # saved_at — timestamptz: наивное время драйвер прочёл бы как местное
        db = RecordingSession()

        await keep_copy(db, 7, AnswerKind.HOSTER_SCAN, {"findings": []})

        self.assertIsNotNone(db.statements[0].compile(dialect=postgresql.dialect()).params["saved_at"].tzinfo)

    async def test_failed_write_does_not_cost_the_user_the_answer(self):
        db = RecordingSession(failure=OperationalError("INSERT", {}, Exception("connection lost")))

        with self.assertLogs("app.services.node_answer_copy", level="WARNING"):
            await keep_copy(db, 7, AnswerKind.NETWORK_STATE, {"interfaces": []})

        db.rollback.assert_awaited_once()


class KeepCopyIntervalTest(unittest.IsolatedAsyncioTestCase):
    """Страница с опросом раз в секунды не должна переписывать копию на каждый ответ."""

    async def asyncSetUp(self) -> None:
        self.db = RecordingSession()
        self.clock = mock.Mock(return_value=1000.0)
        patcher = mock.patch.object(node_answer_copy, "monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        node_answer_copy._last_kept.clear()
        self.addCleanup(node_answer_copy._last_kept.clear)

    async def keep(self, server_id: int = 7, kind: AnswerKind = AnswerKind.DNAT_STATE, interval: float = 60) -> None:
        await keep_copy(self.db, server_id, kind, {"rules": []}, min_interval_sec=interval)

    async def test_answer_inside_the_interval_does_not_rewrite_the_copy(self):
        await self.keep()
        self.clock.return_value += 59

        await self.keep()

        self.assertEqual(len(self.db.statements), 1)

    async def test_copy_is_rewritten_once_the_interval_has_passed(self):
        await self.keep()
        self.clock.return_value += 60

        await self.keep()

        self.assertEqual(len(self.db.statements), 2)

    async def test_other_servers_and_kinds_are_not_held_back(self):
        await self.keep()

        await self.keep(server_id=8)
        await self.keep(kind=AnswerKind.HAPROXY_CONFIG)

        self.assertEqual(len(self.db.statements), 3)

    async def test_without_an_interval_every_answer_is_kept(self):
        await self.keep(interval=0)
        await self.keep(interval=0)

        self.assertEqual(len(self.db.statements), 2)

    async def test_change_made_on_the_node_lets_the_next_answer_through(self):
        # Правила только что переприменили: копия не должна минуту показывать прежние
        await self.keep()

        node_answer_copy.expect_change(7, AnswerKind.DNAT_STATE)
        await self.keep()

        self.assertEqual(len(self.db.statements), 2)

    async def test_failed_write_does_not_start_the_interval(self):
        failing = RecordingSession(failure=OperationalError("INSERT", {}, Exception("connection lost")))
        with self.assertLogs("app.services.node_answer_copy", level="WARNING"):
            await keep_copy(failing, 7, AnswerKind.DNAT_STATE, {"rules": []}, min_interval_sec=60)

        await self.keep()

        self.assertEqual(len(self.db.statements), 1)


class LoadCopyTest(unittest.IsolatedAsyncioTestCase):
    async def test_copy_comes_back_with_the_moment_it_was_taken(self):
        db = RecordingSession(stored('{"enabled": true, "mbit": 500}'))

        answer_copy = await load_copy(db, 7, AnswerKind.BANDWIDTH_LIMIT)

        self.assertEqual(answer_copy.data, {"enabled": True, "mbit": 500})
        self.assertEqual(answer_copy.saved_at, SAVED_AT)

    async def test_lookup_is_by_server_and_kind(self):
        db = RecordingSession()

        await load_copy(db, 7, AnswerKind.NETWORK_STATE)

        compiled = db.statements[0].compile(dialect=postgresql.dialect())
        self.assertEqual(set(compiled.params.values()), {7, "network_state"})

    async def test_server_without_a_copy_has_none(self):
        self.assertIsNone(await load_copy(RecordingSession(), 7, AnswerKind.HOSTER_SCAN))

    async def test_broken_copy_counts_as_missing(self):
        for data in ("{not json", "[1, 2]", '"text"'):
            with self.subTest(data=data):
                self.assertIsNone(await load_copy(RecordingSession(stored(data)), 7, AnswerKind.HOSTER_SCAN))


if __name__ == "__main__":
    unittest.main()
