"""Tests for the read-only export of the legacy traffic SQLite.

Runnable with plain stdlib:  python -m unittest discover -s tests -p "test_*.py"
(from the node/ directory — the store uses stdlib sqlite3, aiosqlite is gone).

The duplicated fixture rows are not invented. The old collector's UPSERT into
daily_traffic never fired, because a NULL inside a SQLite UNIQUE key never
conflicts and every port row has interface NULL (and every interface row has
port NULL). Each collection cycle therefore appended another row instead of
incrementing one, and the table grew without bound. Handing those raw rows to
the panel would import thousands of partial points instead of daily totals.
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import legacy_traffic_store  # noqa: E402
from app.services.legacy_traffic_store import LegacyTrafficStore  # noqa: E402


# Verbatim from traffic_collector.py — including the UNIQUE that never fires.
LEGACY_SCHEMA = """
    CREATE TABLE daily_traffic (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        interface TEXT,
        port INTEGER,
        rx_bytes INTEGER NOT NULL,
        tx_bytes INTEGER NOT NULL,
        UNIQUE(date, interface, port)
    );
"""

INSERT_ROW = (
    "INSERT INTO daily_traffic (date, interface, port, rx_bytes, tx_bytes) VALUES (?, ?, ?, ?, ?)"
)


def day(offset: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y-%m-%d")


TODAY, YESTERDAY = day(0), day(1)

# Three collection cycles for eth0 today, two for port 443, one of each yesterday.
DUPLICATED_ROWS = [
    (TODAY, "eth0", None, 100, 200),
    (TODAY, "eth0", None, 300, 400),
    (TODAY, "eth0", None, 500, 600),
    (TODAY, None, 443, 10, 20),
    (TODAY, None, 443, 30, 40),
    (YESTERDAY, "eth0", None, 1000, 2000),
    (YESTERDAY, None, 443, 7, 8),
]

EXPECTED_TOTALS = {
    (TODAY, "eth0", None): (900, 1200),
    (TODAY, None, 443): (40, 60),
    (YESTERDAY, "eth0", None): (1000, 2000),
    (YESTERDAY, None, 443): (7, 8),
}


@dataclass
class FakeSettings:
    traffic_db_path: str


def totals_by_bucket(daily: list[dict]) -> dict:
    return {
        (row["date"], row["interface"], row["port"]): (row["rx_bytes"], row["tx_bytes"])
        for row in daily
    }


class LegacyStoreTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db_path = self.dir / "traffic.db"
        self.config_path = self.dir / "traffic_config.json"

        original = legacy_traffic_store.get_settings
        legacy_traffic_store.get_settings = lambda: FakeSettings(str(self.db_path))
        self.addCleanup(setattr, legacy_traffic_store, "get_settings", original)

        self.store = LegacyTrafficStore()

    def create_legacy_db(self, rows=(), schema: str = LEGACY_SCHEMA):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(schema)
            if rows:
                conn.executemany(INSERT_ROW, rows)
            conn.commit()

    def raw_row_count(self) -> int:
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute("SELECT COUNT(*) FROM daily_traffic").fetchone()[0]


class ExportTests(LegacyStoreTestCase):
    def test_duplicate_rows_collapse_into_daily_totals(self):
        self.create_legacy_db(DUPLICATED_ROWS)

        result = asyncio.run(self.store.export(400))

        self.assertTrue(result["available"])
        self.assertIsNone(result["purged_at"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["row_count"], len(EXPECTED_TOTALS))
        self.assertEqual(totals_by_bucket(result["daily"]), EXPECTED_TOTALS)

    def test_span_and_fingerprint_are_reported(self):
        self.create_legacy_db(DUPLICATED_ROWS)

        result = asyncio.run(self.store.export(400))

        self.assertEqual(result["min_date"], YESTERDAY)
        self.assertEqual(result["max_date"], TODAY)
        self.assertRegex(result["fingerprint"], r"^[0-9a-f]{64}$")

    def test_window_limits_the_exported_rows(self):
        self.create_legacy_db([(day(120), "eth0", None, 42, 43), *DUPLICATED_ROWS])

        narrow = asyncio.run(self.store.export(30))
        wide = asyncio.run(self.store.export(400))

        self.assertNotIn(day(120), {row["date"] for row in narrow["daily"]})
        self.assertIn(day(120), {row["date"] for row in wide["daily"]})

    def test_export_is_truncated_at_the_row_cap(self):
        # Building 50 000 real rows would only prove sqlite counts; the cap
        # itself is the logic worth pinning, so it is lowered for the test.
        self.create_legacy_db(DUPLICATED_ROWS)
        original = legacy_traffic_store.MAX_EXPORT_ROWS
        legacy_traffic_store.MAX_EXPORT_ROWS = 2
        self.addCleanup(setattr, legacy_traffic_store, "MAX_EXPORT_ROWS", original)

        result = asyncio.run(self.store.export(400))

        self.assertTrue(result["truncated"])
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(len(result["daily"]), 2)

    def test_database_without_the_table_exports_empty(self):
        # Node generations shipped different schemas; a missing table is "no
        # history", not a failed export.
        self.create_legacy_db(schema="CREATE TABLE hourly_traffic (hour TEXT);")

        result = asyncio.run(self.store.export(400))

        self.assertTrue(result["available"])
        self.assertEqual(result["row_count"], 0)
        self.assertEqual(result["daily"], [])
        self.assertIsNone(result["min_date"])

    def test_missing_database_reports_no_db(self):
        result = asyncio.run(self.store.export(400))

        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "no_db")


class ReadOnlyAccessTests(LegacyStoreTestCase):
    def test_the_export_connection_rejects_writes(self):
        """The node must never mutate the legacy DB it is exporting.

        The panel may re-request the export after a failed import, so a write
        that slipped through would change what the second attempt sees.
        """
        self.create_legacy_db(DUPLICATED_ROWS)

        def attempt_write(conn):
            try:
                conn.execute(INSERT_ROW, ("1999-01-01", "eth0", None, 1, 1))
            except sqlite3.OperationalError as exc:
                return str(exc)
            return None

        message = self.store._read(attempt_write)

        self.assertIsNotNone(message, "the export connection accepted a write")
        self.assertIn("readonly", message.lower())
        self.assertEqual(self.raw_row_count(), len(DUPLICATED_ROWS))

    def test_export_leaves_the_rows_untouched(self):
        self.create_legacy_db(DUPLICATED_ROWS)

        asyncio.run(self.store.export(400))

        self.assertEqual(self.raw_row_count(), len(DUPLICATED_ROWS))


class PurgeTests(LegacyStoreTestCase):
    def setUp(self):
        super().setUp()
        self.create_legacy_db(DUPLICATED_ROWS)
        self.wal = self.dir / "traffic.db-wal"
        self.shm = self.dir / "traffic.db-shm"
        self.state = self.dir / "traffic_state.json"
        for sidecar in (self.wal, self.shm):
            sidecar.write_bytes(b"stale")
        self.state.write_text(json.dumps({"interface_bytes": {}}))
        self.config_path.write_text(json.dumps({"tracked_ports": [443]}))

    def test_purge_removes_the_store_but_keeps_traffic_config(self):
        # traffic_config.json is the iptables rule set the new sampler still
        # needs; deleting it would silently stop per-port accounting.
        result = asyncio.run(self.store.purge())

        self.assertTrue(result["success"])
        for gone in (self.db_path, self.wal, self.shm, self.state):
            self.assertFalse(gone.exists(), gone.name)
        self.assertEqual(
            json.loads(self.config_path.read_text()), {"tracked_ports": [443]}
        )

    def test_purge_leaves_a_tombstone(self):
        result = asyncio.run(self.store.purge())

        tombstone = self.dir / legacy_traffic_store.TOMBSTONE_NAME
        self.assertTrue(tombstone.exists())
        self.assertEqual(
            json.loads(tombstone.read_text())["purged_at"], result["purged_at"]
        )
        self.assertFalse(self.store.is_available())

    def test_second_purge_is_a_no_op(self):
        # The panel retries the purge whenever it is unsure the first one landed.
        first = asyncio.run(self.store.purge())
        second = asyncio.run(self.store.purge())

        self.assertTrue(second["success"])
        self.assertEqual(second["removed"], [])
        self.assertEqual(second["purged_at"], first["purged_at"])

    def test_export_after_purge_reports_the_tombstone(self):
        purged = asyncio.run(self.store.purge())

        result = asyncio.run(self.store.export(400))

        self.assertFalse(result["available"])
        self.assertEqual(result["purged_at"], purged["purged_at"])
        self.assertNotIn("reason", result)


if __name__ == "__main__":
    unittest.main()
