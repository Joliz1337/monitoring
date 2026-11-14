"""Таймауты PostgreSQL: простой в транзакции — на каждом соединении пула,
потолок одного запроса — только у сессий API.

Без PostgreSQL: параметры подключения снимаются в хуке do_connect до самого
соединения, лимит запроса — по событию начала транзакции у сессии из get_db.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import event  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app import database  # noqa: E402


class ConnectionIntercepted(Exception):
    """Соединение остановлено до похода в сеть — параметры уже сняты."""


def begin_transaction(session: AsyncSession) -> mock.MagicMock:
    """Начало транзакции сессии без базы: возвращает соединение, которое получили слушатели."""
    connection = mock.MagicMock()
    session.sync_session.dispatch.after_begin(session.sync_session, None, connection)
    return connection


def dropped_connection_error(invalidated: bool) -> DBAPIError:
    return DBAPIError("ROLLBACK", None, Exception("the underlying connection is closed"),
                      connection_invalidated=invalidated)


class PoolConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_pool_connection_limits_idle_transactions(self):
        seen: dict = {}

        def capture(dialect, conn_rec, cargs, cparams):
            seen.update(cparams)
            raise ConnectionIntercepted()

        event.listen(database.engine.sync_engine, "do_connect", capture)
        self.addCleanup(event.remove, database.engine.sync_engine, "do_connect", capture)

        with self.assertRaises(ConnectionIntercepted):
            async with database.engine.connect():
                pass

        self.assertEqual(seen["server_settings"], {"idle_in_transaction_session_timeout": "600000"})


class StatementTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_session_limits_every_transaction(self):
        """Обработчик коммитит и продолжает работать: вторая транзакция той же
        сессии тоже должна получить лимит."""
        sessions = database.get_db()
        session = await sessions.__anext__()

        first = begin_transaction(session)
        second = begin_transaction(session)
        await sessions.aclose()

        for connection in (first, second):
            connection.exec_driver_sql.assert_called_once_with("SET LOCAL statement_timeout = 120000")

    async def test_background_session_is_not_limited(self):
        async with database.async_session() as session:
            connection = begin_transaction(session)

        connection.exec_driver_sql.assert_not_called()

    async def test_api_session_is_closed_after_the_request(self):
        sessions = database.get_db()
        session = await sessions.__anext__()

        with mock.patch.object(database.PanelSession, "close", mock.AsyncMock()) as close:
            await sessions.aclose()

        close.assert_awaited()
        self.assertIsInstance(session, database.PanelSession)


class DroppedConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_closing_on_a_dropped_connection_is_not_an_error(self):
        """PostgreSQL сам оборвал простаивающую транзакцию: выход из блока не
        должен бросать исключение и пропускать код после него."""
        reached_code_after_block = False

        with mock.patch.object(AsyncSession, "close", mock.AsyncMock(side_effect=dropped_connection_error(True))), \
                self.assertLogs(database.logger, level="WARNING"):
            async with database.async_session():
                pass
            reached_code_after_block = True

        self.assertTrue(reached_code_after_block)

    async def test_other_close_errors_still_surface(self):
        with mock.patch.object(AsyncSession, "close", mock.AsyncMock(side_effect=dropped_connection_error(False))):
            with self.assertRaises(DBAPIError):
                async with database.async_session():
                    pass


if __name__ == "__main__":
    unittest.main()
