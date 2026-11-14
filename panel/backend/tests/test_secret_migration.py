"""Шифрование секретов, которые до этого лежали в базе открытым текстом.

Без PostgreSQL: соединение подменено таблицами в памяти и понимает ровно те
запросы, что шлют миграции, — выборку незашифрованных значений, их UPDATE,
список VARCHAR-колонок и ALTER COLUMN.
"""
import base64
import os
import re
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import Boolean, String, Text  # noqa: E402

from app import crypto, database  # noqa: E402
from app.database import Base, needs_encryption  # noqa: E402
from app.models import RemnawaveSettings, Server  # noqa: E402

CF_TOKEN_KEY = "wildcard_cloudflare_api_token"
NEW_SECRET_COLUMNS = [
    ("remnawave_settings", "api_token"),
    ("remnawave_settings", "cookie_secret"),
    ("remnawave_settings", "anomaly_tg_bot_token"),
    ("alert_settings", "telegram_bot_token"),
    ("torrent_blocker_settings", "webhook_secret"),
    ("wildcard_certificates", "privkey_pem"),
]


class FakeResult:
    def __init__(self, rows: list):
        self.rows = rows

    def fetchall(self) -> list:
        return self.rows

    all = fetchall

    def fetchone(self):
        return self.rows[0] if self.rows else None

    first = fetchone

    def scalar(self) -> None:
        return None


class FakeConn:
    """values — {(таблица, колонка): [строка]}, строка — {"id", "v"} и "key" у panel_settings.
    schema_rows — ответ на любой запрос к information_schema.columns."""

    def __init__(self, values: dict | None = None, schema_rows: list | None = None,
                 broken_tables: tuple = ()):
        self.values = values or {}
        self.schema_rows = schema_rows or []
        self.broken_tables = broken_tables
        self.altered: list[str] = []
        self.updates = 0

    async def execute(self, statement, params: dict | None = None) -> FakeResult:
        sql = " ".join(str(statement).split())
        if "information_schema.columns" in sql:
            return FakeResult(self.schema_rows)
        if sql.startswith("ALTER TABLE"):
            table = sql.split()[2]
            if table in self.broken_tables:
                raise RuntimeError("lock not available")
            self.altered.append(sql)
            return FakeResult([])
        select = re.match(r'SELECT id, "(\w+)" AS v FROM (\w+)', sql)
        if select:
            return FakeResult(self._plaintext_rows(select.group(2), select.group(1), params))
        update = re.match(r'UPDATE (\w+) SET "(\w+)" = :val WHERE id = :id', sql)
        if not update:
            return FakeResult([])
        if update.group(1) in self.broken_tables:
            raise RuntimeError("value too long for type character varying(200)")
        for row in self.values[(update.group(1), update.group(2))]:
            if row["id"] == params["id"]:
                row["v"] = params["val"]
                self.updates += 1
        return FakeResult([])

    def _plaintext_rows(self, table: str, col: str, params: dict) -> list:
        return [
            SimpleNamespace(id=row["id"], v=row["v"])
            for row in self.values.get((table, col), [])
            if row["v"] is not None
            and not row["v"].startswith(params["pfx"].rstrip("%"))
            and ("key" not in params or row.get("key") == params["key"])
        ]

    def stored(self, table: str, col: str, row_id: int = 1) -> str:
        return next(row["v"] for row in self.values[(table, col)] if row["id"] == row_id)


class EncryptionKeyCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        crypto._reset_cache_for_tests()
        os.environ["PANEL_ENC_KEY"] = base64.b64encode(b"\x22" * 32).decode()

    def tearDown(self):
        os.environ.pop("PANEL_ENC_KEY", None)
        crypto._reset_cache_for_tests()


class NeedsEncryptionTests(EncryptionKeyCase):
    def test_plaintext_needs_encryption(self):
        self.assertTrue(needs_encryption("-----BEGIN PRIVATE KEY-----"))

    def test_already_encrypted_skipped(self):
        self.assertFalse(needs_encryption(crypto.encrypt_secret("x")))

    def test_none_and_empty_skipped(self):
        self.assertFalse(needs_encryption(None))
        self.assertFalse(needs_encryption(""))


class EncryptSecretsMigrationTests(EncryptionKeyCase):
    async def test_plaintext_tokens_and_keys_get_encrypted(self):
        conn = FakeConn({target: [{"id": 1, "v": f"plain-{target[1]}"}] for target in NEW_SECRET_COLUMNS})

        await database._migrate_encrypt_secrets(conn)

        for table, col in NEW_SECRET_COLUMNS:
            stored = conn.stored(table, col)
            self.assertTrue(stored.startswith(crypto.ENC_PREFIX), f"{table}.{col}")
            self.assertEqual(crypto.decrypt_secret(stored), f"plain-{col}")

    async def test_only_the_secret_setting_is_encrypted(self):
        conn = FakeConn({("panel_settings", "value"): [
            {"id": 1, "key": CF_TOKEN_KEY, "v": "cf-token"},
            {"id": 2, "key": "refresh_interval", "v": "5"},
            {"id": 3, "key": "wildcard_email", "v": "admin@example.com"},
        ]})

        await database._migrate_encrypt_secrets(conn)

        self.assertEqual(crypto.decrypt_secret(conn.stored("panel_settings", "value", 1)), "cf-token")
        self.assertTrue(conn.stored("panel_settings", "value", 1).startswith(crypto.ENC_PREFIX))
        self.assertEqual(conn.stored("panel_settings", "value", 2), "5")
        self.assertEqual(conn.stored("panel_settings", "value", 3), "admin@example.com")

    async def test_second_run_changes_nothing(self):
        conn = FakeConn({
            ("alert_settings", "telegram_bot_token"): [{"id": 1, "v": "123:token"}],
            ("panel_settings", "value"): [{"id": 1, "key": CF_TOKEN_KEY, "v": "cf-token"}],
        })
        await database._migrate_encrypt_secrets(conn)
        first_pass = (conn.stored("alert_settings", "telegram_bot_token"), conn.stored("panel_settings", "value"))
        conn.updates = 0

        await database._migrate_encrypt_secrets(conn)

        self.assertEqual(conn.updates, 0)
        self.assertEqual(
            (conn.stored("alert_settings", "telegram_bot_token"), conn.stored("panel_settings", "value")),
            first_pass,
        )

    async def test_empty_and_missing_values_are_left_alone(self):
        conn = FakeConn({("torrent_blocker_settings", "webhook_secret"): [
            {"id": 1, "v": None}, {"id": 2, "v": ""},
        ]})

        await database._migrate_encrypt_secrets(conn)

        self.assertEqual(conn.updates, 0)

    async def test_one_failing_column_does_not_stop_the_rest(self):
        """Колонка, в которую шифротекст не лёг, остаётся открытым текстом и читается
        как раньше; панель при этом стартует, а остальные секреты шифруются."""
        conn = FakeConn(
            {
                ("alert_settings", "telegram_bot_token"): [{"id": 1, "v": "123:token"}],
                ("wildcard_certificates", "privkey_pem"): [{"id": 1, "v": "PRIVATE KEY"}],
            },
            broken_tables=("alert_settings",),
        )

        with self.assertLogs(database.logger, level="ERROR") as logs:
            await database._migrate_encrypt_secrets(conn)

        self.assertEqual(conn.stored("alert_settings", "telegram_bot_token"), "123:token")
        self.assertTrue(conn.stored("wildcard_certificates", "privkey_pem").startswith(crypto.ENC_PREFIX))
        self.assertIn("alert_settings.telegram_bot_token", logs.output[0])
        self.assertNotIn("123:token", logs.output[0])

    async def test_without_a_key_nothing_is_touched(self):
        os.environ.pop("PANEL_ENC_KEY", None)
        crypto._reset_cache_for_tests()
        conn = FakeConn({("alert_settings", "telegram_bot_token"): [{"id": 1, "v": "123:token"}]})

        await database._migrate_encrypt_secrets(conn)

        self.assertEqual(conn.stored("alert_settings", "telegram_bot_token"), "123:token")


class WidenSecretColumnsTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_narrow_secret_columns_are_widened(self):
        conn = FakeConn(schema_rows=[
            ("alert_settings", "telegram_bot_token"),
            ("alert_settings", "telegram_chat_id"),
            ("remnawave_settings", "api_token"),
            ("servers", "name"),
        ])

        await database._migrate_widen_secret_columns(conn)

        self.assertEqual(sorted(conn.altered), [
            'ALTER TABLE alert_settings ALTER COLUMN "telegram_bot_token" TYPE TEXT',
            'ALTER TABLE remnawave_settings ALTER COLUMN "api_token" TYPE TEXT',
        ])

    async def test_already_wide_schema_gets_no_ddl(self):
        conn = FakeConn(schema_rows=[("servers", "name")])

        await database._migrate_widen_secret_columns(conn)

        self.assertEqual(conn.altered, [])

    async def test_failed_widening_does_not_stop_the_start(self):
        conn = FakeConn(
            schema_rows=[("alert_settings", "telegram_bot_token"), ("remnawave_settings", "api_token")],
            broken_tables=("alert_settings",),
        )

        with self.assertLogs(database.logger, level="WARNING"):
            await database._migrate_widen_secret_columns(conn)

        self.assertEqual(conn.altered, ['ALTER TABLE remnawave_settings ALTER COLUMN "api_token" TYPE TEXT'])


class SecretSchemaTests(unittest.TestCase):
    def test_every_migrated_column_is_encrypted_by_the_model(self):
        """Миграция шифрует то, что уже лежит; новое шифрует тип колонки. Колонка из
        списка миграции с обычным типом снова писала бы открытый текст."""
        for table, col in database._SECRET_COLUMNS:
            column_type = Base.metadata.tables[table].c[col].type
            self.assertIsInstance(column_type, crypto.EncryptedString, f"{table}.{col}")

    def test_new_secrets_are_in_the_migration_list(self):
        for target in NEW_SECRET_COLUMNS:
            self.assertIn(target, database._SECRET_COLUMNS)
        self.assertIn(CF_TOKEN_KEY, database.SECRET_SETTING_KEYS)


class NewColumnsTests(unittest.IsolatedAsyncioTestCase):
    def test_model_columns(self):
        host_key = Server.__table__.c.ssh_host_key
        self.assertIsInstance(host_key.type, Text)
        self.assertTrue(host_key.nullable)

        fingerprint = Server.__table__.c.wildcard_ssl_deployed_fingerprint
        self.assertIsInstance(fingerprint.type, String)
        self.assertEqual(fingerprint.type.length, 64)
        self.assertTrue(fingerprint.nullable)

        verify_tls = RemnawaveSettings.__table__.c.verify_tls
        self.assertIsInstance(verify_tls.type, Boolean)
        self.assertFalse(verify_tls.nullable)
        self.assertIs(verify_tls.default.arg, True)
        self.assertEqual(verify_tls.server_default.arg, "true")

    async def test_host_key_and_verify_tls_are_added_to_an_existing_database(self):
        conn = FakeConn(schema_rows=[("id",), ("name",)])

        await database.run_migrations(conn)

        self.assertIn('ALTER TABLE servers ADD COLUMN "ssh_host_key" TEXT', conn.altered)
        self.assertIn(
            'ALTER TABLE remnawave_settings ADD COLUMN "verify_tls" BOOLEAN NOT NULL DEFAULT TRUE', conn.altered,
        )

    async def test_host_key_and_verify_tls_are_not_added_twice(self):
        conn = FakeConn(schema_rows=[("id",), ("ssh_host_key",), ("verify_tls",)])

        await database.run_migrations(conn)

        self.assertFalse([sql for sql in conn.altered if "ssh_host_key" in sql or "verify_tls" in sql])

    async def test_wildcard_fingerprint_is_added_to_an_existing_database(self):
        conn = FakeConn(schema_rows=[("id",), ("name",), ("wildcard_ssl_enabled",)])

        await database._migrate_wildcard_ssl(conn)

        self.assertIn(
            'ALTER TABLE servers ADD COLUMN "wildcard_ssl_deployed_fingerprint" VARCHAR(64)', conn.altered,
        )

    async def test_wildcard_fingerprint_is_not_added_twice(self):
        conn = FakeConn(schema_rows=[("id",), ("wildcard_ssl_deployed_fingerprint",)])

        await database._migrate_wildcard_ssl(conn)

        self.assertFalse([sql for sql in conn.altered if "wildcard_ssl_deployed_fingerprint" in sql])


if __name__ == "__main__":
    unittest.main()
