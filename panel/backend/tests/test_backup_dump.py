"""pg_dump для бэкапа панели: что попадает в дамп и чем кончается сбой.

Без Docker и PostgreSQL — подменяется subprocess.run. Сырые метрики при 2000
нодах — ~17 млн строк: дамп с ними не укладывался в таймаут, а таймаут вылетал
исключением мимо записи запуска, и планировщик стартовал новый дамп каждую минуту.
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers import backup as bk  # noqa: E402

DUMP = b"PGDMP\x01\x0e\x00custom-format-dump"
RAW_ROWS_FLAG = "--exclude-table-data=metrics_snapshots"
DUMP_SECONDS = 12.5


def finished(returncode: int = 0, stdout: bytes = DUMP, stderr: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class PgDumpTests(unittest.TestCase):
    def setUp(self):
        pg_env = mock.patch.object(bk, "_pg_env", return_value=("panel", "paneldb"))
        pg_env.start()
        self.addCleanup(pg_env.stop)

    def run_dump(self, **docker_behaviour) -> tuple[tuple[bytes, str | None], mock.Mock]:
        with mock.patch.object(bk.subprocess, "run", **docker_behaviour) as docker:
            return bk._run_pg_dump(), docker

    def test_only_raw_metric_rows_are_left_out(self):
        _, docker = self.run_dump(return_value=finished())
        command = list(docker.call_args.args[0])

        self.assertIn(RAW_ROWS_FLAG, command)
        # Остальное — прежний полный дамп: определение таблицы и все прочие таблицы на месте
        command.remove(RAW_ROWS_FLAG)
        self.assertEqual(
            command,
            ["docker", "exec", "panel-postgres", "pg_dump", "-U", "panel", "-d", "paneldb", "-Fc"],
        )

    def test_timeout_is_a_failed_dump_not_an_exception(self):
        timeout = subprocess.TimeoutExpired(cmd="docker exec panel-postgres pg_dump", timeout=600)

        (data, error), _ = self.run_dump(side_effect=timeout)

        self.assertEqual(data, b"")
        self.assertIn("timed out", error)

    def test_pg_dump_failure_returns_its_stderr(self):
        failed = finished(returncode=1, stdout=b"", stderr=b"pg_dump: error: connection refused")

        (data, error), _ = self.run_dump(return_value=failed)

        self.assertEqual((data, error), (b"", "pg_dump: error: connection refused"))

    def test_finished_dump_logs_its_size_and_duration(self):
        clock = FakeClock()

        def slow_dump(*_args, **_kwargs) -> subprocess.CompletedProcess:
            clock.now += DUMP_SECONDS
            return finished()

        with mock.patch("time.monotonic", clock), self.assertLogs(bk.logger, level="INFO") as logs:
            (data, error), _ = self.run_dump(side_effect=slow_dump)

        self.assertEqual((data, error), (DUMP, None))
        record = logs.records[0]
        self.assertEqual(record.levelname, "INFO")
        self.assertIn(f"{len(DUMP)} bytes", record.getMessage())
        self.assertIn(f"{DUMP_SECONDS} s", record.getMessage())


if __name__ == "__main__":
    unittest.main()
