"""Тесты переноса легаси-истории трафика с нод (app/services/traffic_import.py).

Голый unittest, без PostgreSQL и без сети. Раскладка выгрузки в строки server_traffic —
чистая функция `build_traffic_rows`. Сценарии всего переноса прогоняются через подкласс
`RecordingImporter`: в нём заглушены три метода-границы (HTTP к ноде, запись строк,
сохранение состояния), причём хранилище повторяет семантику `ON CONFLICT DO NOTHING` —
именно на ней держится идемпотентность повторного прогона.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services.traffic_import import (  # noqa: E402
        MIN_NODE_VERSION_FOR_TRAFFIC_V2,
        ImportStatus,
        TrafficImporter,
        build_traffic_rows,
        node_supports_traffic_v2,
    )
except ImportError as e:  # рантайм панели (sqlalchemy, httpx, asyncpg) не установлен
    raise unittest.SkipTest(f"traffic_import requires the panel runtime: {e}")

SERVER_ID = 7
DAY = datetime(2026, 3, 1)
NEXT_DAY = datetime(2026, 3, 2)


def conflict_key(row: dict) -> tuple:
    return (
        row["server_id"], row["period_type"], row["scope"], row["bucket"], row["scope_key"],
    )


def by_scope(rows: list[dict], scope: str, scope_key=None) -> list[dict]:
    return [
        row for row in rows
        if row["scope"] == scope and (scope_key is None or row["scope_key"] == scope_key)
    ]


def daily_row(date: str, rx: int, tx: int, interface=None, port=None) -> dict:
    return {
        "date": date, "interface": interface, "port": port, "rx_bytes": rx, "tx_bytes": tx,
    }


class RecordingImporter(TrafficImporter):
    """Импортёр с заглушенными границами ввода-вывода."""

    def __init__(self, export: dict):
        super().__init__()
        self.export = export
        self.stored: dict[tuple, dict] = {}
        self.saved: list[dict] = []
        self.export_calls = 0
        self.purge_calls = 0

    async def _fetch_export(self, server) -> dict:
        self.export_calls += 1
        return self.export

    async def _purge_node(self, server) -> None:
        self.purge_calls += 1

    async def _store_rows(self, rows: list[dict]) -> None:
        for row in rows:
            self.stored.setdefault(conflict_key(row), row)

    async def _save_state(self, server_id: int, **fields) -> None:
        self.saved.append(fields)

    @property
    def state(self) -> dict:
        merged: dict = {}
        for fields in self.saved:
            merged.update(fields)
        return merged


def node(version: str = MIN_NODE_VERSION_FOR_TRAFFIC_V2):
    return SimpleNamespace(id=SERVER_ID, name="node-1", node_version=version)


def run_import(importer: RecordingImporter, server) -> None:
    asyncio.run(importer._import_and_purge(server))


class DailyAggregationTests(unittest.TestCase):
    def test_duplicate_rows_are_summed(self):
        rows = build_traffic_rows(SERVER_ID, [
            daily_row("2026-03-01", 100, 10, interface="eth0"),
            daily_row("2026-03-01", 50, 5, interface="eth0"),
            daily_row("2026-03-01", 70, 7, port=443),
            daily_row("2026-03-01", 30, 3, port=443),
            daily_row("2026-03-02", 11, 1, interface="eth0"),
        ])

        eth0 = by_scope(rows, "iface", "eth0")
        self.assertEqual(len(eth0), 2)
        first_day = next(row for row in eth0 if row["bucket"] == DAY)
        self.assertEqual((first_day["rx_bytes"], first_day["tx_bytes"]), (150, 15))

        port = by_scope(rows, "port", "443")
        self.assertEqual(len(port), 1)
        self.assertEqual((port[0]["rx_bytes"], port[0]["tx_bytes"]), (100, 10))

        totals = {row["bucket"]: (row["rx_bytes"], row["tx_bytes"]) for row in by_scope(rows, "total")}
        self.assertEqual(totals, {DAY: (150, 15), NEXT_DAY: (11, 1)})

        # Ключ конфликта обязан быть уникальным: две строки с одним ключом в одном
        # INSERT ... ON CONFLICT DO NOTHING молча теряют вторую, и iface разойдётся с total.
        keys = [conflict_key(row) for row in rows]
        self.assertEqual(len(keys), len(set(keys)))

    def test_virtual_interfaces_excluded_from_total(self):
        rows = build_traffic_rows(SERVER_ID, [
            daily_row("2026-03-01", 200, 20, interface="eth0"),
            daily_row("2026-03-01", 90, 9, interface="docker0"),
            daily_row("2026-03-01", 5, 1, interface="veth7f2a"),
            daily_row("2026-03-01", 7, 2, interface="br-abc123"),
        ])

        totals = by_scope(rows, "total")
        self.assertEqual(len(totals), 1)
        self.assertEqual((totals[0]["rx_bytes"], totals[0]["tx_bytes"]), (200, 20))

    def test_rows_carry_legacy_source_and_day_period(self):
        rows = build_traffic_rows(SERVER_ID, [daily_row("2026-03-01", 1, 1, interface="eth0")])
        self.assertTrue(all(row["source"] == "legacy" for row in rows))
        self.assertTrue(all(row["period_type"] == "day" for row in rows))
        self.assertTrue(all(row["covered_seconds"] == 0 for row in rows))

    def test_malformed_entries_are_skipped(self):
        rows = build_traffic_rows(SERVER_ID, [
            "not a dict",
            daily_row("31-02-2026", 5, 5, interface="eth0"),
            daily_row("2026-03-01", 5, 5, port=70_000),
            daily_row("2026-03-01", 9, 9, interface="eth0"),
        ])
        self.assertEqual([row["scope"] for row in rows], ["iface", "total"])


class VersionGateTests(unittest.TestCase):
    def test_gate_is_exactly_10_13_0(self):
        self.assertFalse(node_supports_traffic_v2("10.12.0"))
        self.assertFalse(node_supports_traffic_v2("10.12.9"))
        self.assertTrue(node_supports_traffic_v2("10.13.0"))
        self.assertTrue(node_supports_traffic_v2("11.0.0"))
        self.assertFalse(node_supports_traffic_v2(None))
        self.assertFalse(node_supports_traffic_v2(""))


class ImportFlowTests(unittest.TestCase):
    EXPORT = {
        "available": True,
        "purged_at": None,
        "fingerprint": "abc123",
        "row_count": 3,
        "min_date": "2026-03-01",
        "max_date": "2026-03-02",
        "truncated": False,
        "daily": [
            daily_row("2026-03-01", 200, 20, interface="eth0"),
            daily_row("2026-03-01", 70, 7, port=443),
            daily_row("2026-03-02", 300, 30, interface="eth0"),
        ],
    }

    def test_second_run_adds_nothing(self):
        importer = RecordingImporter(self.EXPORT)
        server = node()

        run_import(importer, server)
        rows_after_first = len(importer.stored)
        imported_first = importer.state["rows_imported"]

        run_import(importer, server)

        self.assertEqual(len(importer.stored), rows_after_first)
        self.assertEqual(importer.state["rows_imported"], imported_first)
        self.assertEqual(
            [fields["rows_imported"] for fields in importer.saved if "rows_imported" in fields],
            [imported_first, imported_first],
        )

    def test_import_then_purge(self):
        importer = RecordingImporter(self.EXPORT)
        run_import(importer, node())

        self.assertEqual(importer.purge_calls, 1)
        self.assertEqual(importer.state["status"], ImportStatus.PURGED.value)
        self.assertEqual(
            {row["scope"] for row in importer.stored.values()}, {"iface", "port", "total"},
        )

    def test_already_purged_node_stores_nothing(self):
        importer = RecordingImporter({"available": False, "purged_at": "2026-03-01T10:00:00Z"})
        run_import(importer, node())

        self.assertEqual(importer.state["status"], ImportStatus.PURGED.value)
        self.assertEqual(importer.state["purged_at"], datetime(2026, 3, 1, 10, 0))
        self.assertEqual(importer.stored, {})
        self.assertEqual(importer.purge_calls, 0)

    def test_node_without_legacy_db_is_marked_empty(self):
        importer = RecordingImporter({"available": False, "reason": "no_db"})
        run_import(importer, node())

        self.assertEqual(importer.state["status"], ImportStatus.EMPTY.value)
        self.assertEqual(importer.stored, {})
        self.assertEqual(importer.purge_calls, 0)

    def test_old_node_is_postponed_without_export_request(self):
        importer = RecordingImporter(self.EXPORT)
        run_import(importer, node(version="10.12.0"))

        self.assertEqual(importer.state["status"], ImportStatus.NODE_TOO_OLD.value)
        self.assertEqual(importer.export_calls, 0)
        self.assertEqual(importer.stored, {})


if __name__ == "__main__":
    unittest.main()
