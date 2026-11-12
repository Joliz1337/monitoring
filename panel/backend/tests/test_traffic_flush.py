"""Тесты записи накопленного трафика в базу (TrafficIngest.flush).

Голый unittest, без PostgreSQL: сессия подменена и запоминает запросы.

На парке в тысячи нод минутный флаш — десятки тысяч строк. Запрос, текст которого
зависит от числа строк, SQLAlchemy собирает и компилирует заново на каждый батч,
и это секунды процессора бэкенда каждую минуту. Текст запроса обязан быть постоянным.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import math
import os
import sys
import unittest
from datetime import datetime
from enum import Enum
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import traffic_ingest  # noqa: E402
from app.services.traffic_ingest import BucketDelta, CounterState, TrafficIngest  # noqa: E402

HOUR = datetime(2026, 11, 12, 8)
DAY = datetime(2026, 11, 12)


class RecordingSession:
    def __init__(self, log: list):
        self._log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, params=None):
        self._log.append((statement, params))

    async def commit(self):
        self._log.append(("commit", None))


def filled_ingest(servers: int, ports: int = 1) -> TrafficIngest:
    ingest = TrafficIngest()
    for server_id in range(1, servers + 1):
        for port in range(ports):
            key = str(443 + port)
            ingest._accum[(server_id, "hour", "port", key, HOUR)] = BucketDelta(server_id * 10 + port, server_id, 60)
            ingest._accum[(server_id, "day", "port", key, DAY)] = BucketDelta(server_id * 10 + port, server_id, 60)
            counter = (server_id, "port", key)
            ingest._counters[counter] = CounterState(
                rx_value=1000 + server_id, tx_value=2000 + server_id,
                observed_at=HOUR, boot_id=None, boot_at=None,
            )
            ingest._dirty.add(counter)
    return ingest


class TrafficFlushTest(unittest.IsolatedAsyncioTestCase):
    async def _flush(self, ingest: TrafficIngest) -> list:
        log: list = []
        with mock.patch.object(traffic_ingest, "async_session", lambda: RecordingSession(log)):
            await ingest.flush()
        return log

    @staticmethod
    def _statements(log: list, table: str) -> list:
        return [
            (stmt, params) for stmt, params in log
            if stmt != "commit" and f"INTO {table} (" in " ".join(str(stmt).split())
        ]

    async def test_statement_text_does_not_depend_on_the_number_of_rows(self):
        small = self._statements(await self._flush(filled_ingest(servers=2)), "server_traffic")
        large = self._statements(await self._flush(filled_ingest(servers=300)), "server_traffic")
        self.assertIs(small[0][0], large[0][0])

    async def test_fleet_sized_flush_takes_a_handful_of_statements(self):
        servers, ports = 2000, 6
        log = await self._flush(filled_ingest(servers=servers, ports=ports))
        bucket_rows = servers * ports * 2
        expected = math.ceil(bucket_rows / traffic_ingest.UPSERT_BATCH_SIZE)
        self.assertEqual(len(self._statements(log, "server_traffic")), expected)
        self.assertLessEqual(expected, 10)

    async def test_every_bucket_row_reaches_the_database_with_its_own_values(self):
        ingest = filled_ingest(servers=3, ports=2)
        expected = {key: (delta.rx_bytes, delta.tx_bytes) for key, delta in ingest._accum.items()}

        log = await self._flush(ingest)

        written = {}
        for _, params in self._statements(log, "server_traffic"):
            for i, server_id in enumerate(params["server_id"]):
                key = (server_id, params["period_type"][i], params["scope"][i],
                       params["scope_key"][i], params["bucket"][i])
                written[key] = (params["rx_bytes"][i], params["tx_bytes"][i])
        self.assertEqual(written, expected)

    async def test_counters_are_written_in_the_same_transaction(self):
        log = await self._flush(filled_ingest(servers=3))
        counters = self._statements(log, "server_traffic_counters")
        self.assertEqual(len(counters), 1)
        self.assertEqual(sorted(counters[0][1]["server_id"]), [1, 2, 3])
        self.assertEqual([entry[0] for entry in log].count("commit"), 1)
        self.assertEqual(log[-1][0], "commit")

    async def test_enum_members_are_sent_as_plain_strings(self):
        class Period(str, Enum):
            HOUR = "hour"

        ingest = TrafficIngest()
        ingest._accum[(1, Period.HOUR, "total", "", HOUR)] = BucketDelta(1, 2, 60)
        log = await self._flush(ingest)
        period = self._statements(log, "server_traffic")[0][1]["period_type"][0]
        self.assertIs(type(period), str)
        self.assertEqual(period, "hour")


if __name__ == "__main__":
    unittest.main()
