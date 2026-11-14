"""Перевод парка на общий сертификат (POST /servers/migrate-all).

Обход большого парка идёт дольше, чем PostgreSQL держит открытую транзакцию:
коммит в конце падал бы, а ноды к этому моменту уже получили сертификат.
Таблица servers поднята в SQLite в памяти, запросы роутера выполняются
по-настоящему; ноды подменены.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import Server  # noqa: E402
from app.routers import servers as servers_router  # noqa: E402

REQUEST = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pki=object())))


def node(server_id: int, **flags) -> Server:
    fields = {"pki_enabled": True, "uses_shared_cert": False, "dedicated_cert": False, **flags}
    return Server(id=server_id, name=f"node-{server_id}", url=f"https://10.0.0.{server_id}:9100", **fields)


class FleetDb:
    def __init__(self, servers: list[Server]):
        engine = create_engine("sqlite://")
        Server.__table__.create(engine)
        self.session = Session(engine, expire_on_commit=False)
        self.session.add_all(servers)
        self.session.commit()

    async def execute(self, statement):
        return self.session.execute(statement)

    async def commit(self) -> None:
        self.session.commit()

    def migrated_ids(self) -> list[int]:
        rows = self.session.execute(text("SELECT id FROM servers WHERE uses_shared_cert ORDER BY id"))
        return [row[0] for row in rows]


class MigrateAllTests(unittest.IsolatedAsyncioTestCase):
    def use_nodes(self, db: FleetDb, unreachable: tuple[int, ...] = ()) -> list[bool]:
        """Подменяет ноды; возвращает, была ли у сессии открытая транзакция в момент каждого обращения."""
        transaction_open: list[bool] = []

        async def push(server: Server, keygen) -> None:
            transaction_open.append(db.session.in_transaction())
            if server.id in unreachable:
                raise ConnectionError("node unreachable")

        patcher = mock.patch.object(servers_router, "push_shared_cert_to_node", push)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(db.session.close)
        return transaction_open

    async def migrate(self, db: FleetDb) -> dict:
        return await servers_router.migrate_all_servers(REQUEST, db, {})

    async def test_no_transaction_is_held_while_nodes_are_contacted(self):
        db = FleetDb([node(1), node(2), node(3)])
        transaction_open = self.use_nodes(db)

        await self.migrate(db)

        self.assertEqual(transaction_open, [False, False, False])

    async def test_flags_of_migrated_nodes_are_saved(self):
        db = FleetDb([node(1), node(2)])
        self.use_nodes(db)

        response = await self.migrate(db)

        self.assertFalse(db.session.in_transaction())
        self.assertEqual(db.migrated_ids(), [1, 2])
        self.assertEqual(sorted(item["id"] for item in response["auto_migrated"]), [1, 2])

    async def test_failed_nodes_do_not_lose_the_flags_of_the_rest(self):
        db = FleetDb([node(1), node(2), node(3)])
        self.use_nodes(db, unreachable=(2,))

        response = await self.migrate(db)

        self.assertEqual(db.migrated_ids(), [1, 3])
        self.assertEqual([item["id"] for item in response["failed"]], [2])
        self.assertIn("node unreachable", response["failed"][0]["error"])

    async def test_nothing_is_written_when_every_node_failed(self):
        db = FleetDb([node(1), node(2)])
        self.use_nodes(db, unreachable=(1, 2))

        response = await self.migrate(db)

        self.assertFalse(db.session.in_transaction())
        self.assertEqual(db.migrated_ids(), [])
        self.assertEqual(response["auto_migrated"], [])

    async def test_shared_and_dedicated_nodes_are_left_alone(self):
        db = FleetDb([node(1), node(2, uses_shared_cert=True), node(3, dedicated_cert=True)])
        transaction_open = self.use_nodes(db)

        await self.migrate(db)

        self.assertEqual(len(transaction_open), 1)
        self.assertEqual(db.migrated_ids(), [1, 2])


if __name__ == "__main__":
    unittest.main()
