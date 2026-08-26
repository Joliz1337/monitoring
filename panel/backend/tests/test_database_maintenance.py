"""Шлагбаум на соединения с базой во время восстановления из бэкапа.

Без PostgreSQL: проверяется сам хук do_connect и то, что контекст снимает
запрет при любом выходе — иначе после неудачного restore панель осталась бы
без базы до перезапуска.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import database  # noqa: E402


def try_connect():
    return database._refuse_connections_during_maintenance(None, None, (), {})


class DatabaseMaintenanceTests(unittest.TestCase):
    def test_connections_pass_outside_maintenance(self):
        self.assertIsNone(try_connect())

    def test_connections_refused_inside_maintenance(self):
        async def scenario():
            async with database.database_maintenance("restore"):
                with self.assertRaises(database.DatabaseMaintenanceError) as ctx:
                    try_connect()
                self.assertEqual(str(ctx.exception), "restore")
            self.assertIsNone(try_connect())

        asyncio.run(scenario())

    def test_gate_lifted_after_failure_inside(self):
        async def scenario():
            with self.assertRaises(ValueError):
                async with database.database_maintenance("restore"):
                    raise ValueError("pg_restore blew up")
            self.assertIsNone(try_connect())

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
