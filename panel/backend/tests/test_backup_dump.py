"""pg_dump для бэкапа панели: что попадает в дамп, куда он пишется и чем кончается сбой.

Без Docker и PostgreSQL — подменяется subprocess.run. Сырые метрики при 2000
нодах — ~17 млн строк: дамп с ними не укладывался в таймаут, а таймаут вылетал
исключением мимо записи запуска, и планировщик стартовал новый дамп каждую минуту.
Сам дамп — сотни мегабайт: pg_dump пишет его прямо в файл, через память бэкенда
он не проходит.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers import backup as bk  # noqa: E402

DUMP = b"PGDMP\x01\x0e\x00custom-format-dump"
ENV_TEXT = "PANEL_ENC_KEY=a2V5\nJWT_SECRET=jjj\n"
FRAME = b"MONBKP2\n34\n" + ENV_TEXT.encode()
RAW_ROWS_FLAG = "--exclude-table-data=metrics_snapshots"
DUMP_SECONDS = 12.5


def pg_dump(dump: bytes = DUMP, returncode: int = 0, stderr: bytes = b""):
    """docker exec pg_dump: пишет в полученный дескриптор, как дочерний процесс, — мимо буфера Python."""
    def run(command, **options) -> subprocess.CompletedProcess:
        os.write(options["stdout"].fileno(), dump)
        return subprocess.CompletedProcess(command, returncode, stdout=None, stderr=stderr)
    return run


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

    def run_dump(self, docker) -> tuple[tuple[int, str | None], bytes, mock.Mock]:
        """Исход дампа, содержимое файла после него и вызов docker."""
        with tempfile.TemporaryFile() as target, mock.patch.object(bk.subprocess, "run", side_effect=docker) as run:
            outcome = bk._run_pg_dump(target)
            target.seek(0)
            return outcome, target.read(), run

    def test_only_raw_metric_rows_are_left_out(self):
        _, _, docker = self.run_dump(pg_dump())
        command = list(docker.call_args.args[0])

        self.assertIn(RAW_ROWS_FLAG, command)
        # Остальное — прежний полный дамп: определение таблицы и все прочие таблицы на месте
        command.remove(RAW_ROWS_FLAG)
        self.assertEqual(
            command,
            ["docker", "exec", "panel-postgres", "pg_dump", "-U", "panel", "-d", "paneldb", "-Fc"],
        )

    def test_dump_is_written_by_pg_dump_itself_not_through_backend_memory(self):
        (dump_bytes, error), written, docker = self.run_dump(pg_dump())
        options = docker.call_args.kwargs

        self.assertEqual((dump_bytes, error, written), (len(DUMP), None, DUMP))
        self.assertTrue(hasattr(options["stdout"], "fileno"))
        self.assertEqual(options["stderr"], subprocess.PIPE)
        self.assertNotIn("capture_output", options)

    def test_timeout_is_a_failed_dump_not_an_exception(self):
        timeout = subprocess.TimeoutExpired(cmd="docker exec panel-postgres pg_dump", timeout=600)

        (dump_bytes, error), _, _ = self.run_dump(timeout)

        self.assertEqual(dump_bytes, 0)
        self.assertIn("timed out", error)

    def test_pg_dump_failure_returns_its_stderr(self):
        failed = pg_dump(dump=b"", returncode=1, stderr=b"pg_dump: error: connection refused")

        (dump_bytes, error), _, _ = self.run_dump(failed)

        self.assertEqual((dump_bytes, error), (0, "pg_dump: error: connection refused"))

    def test_finished_dump_logs_its_size_and_duration(self):
        clock = FakeClock()

        def slow_dump(command, **options) -> subprocess.CompletedProcess:
            clock.now += DUMP_SECONDS
            return pg_dump()(command, **options)

        with mock.patch("time.monotonic", clock), self.assertLogs(bk.logger, level="INFO") as logs:
            (dump_bytes, error), _, _ = self.run_dump(slow_dump)

        self.assertEqual((dump_bytes, error), (len(DUMP), None))
        record = logs.records[0]
        self.assertEqual(record.levelname, "INFO")
        self.assertIn(f"{len(DUMP)} bytes", record.getMessage())
        self.assertIn(f"{DUMP_SECONDS} s", record.getMessage())


class WriteBackupTests(unittest.TestCase):
    """Файл бэкапа — заголовок с .env и сразу за ним дамп, который дописал pg_dump."""

    def setUp(self):
        for target, name, replacement in (
            (bk, "_pg_env", mock.Mock(return_value=("panel", "paneldb"))),
            (bk, "read_panel_env", mock.Mock(return_value=ENV_TEXT)),
            (bk.subprocess, "run", mock.Mock(side_effect=pg_dump())),
        ):
            patcher = mock.patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def written_backup(self) -> tuple[tuple[int, str | None], bytes]:
        with tempfile.TemporaryFile() as target:
            outcome = bk.write_backup(target)
            target.seek(0)
            return outcome, target.read()

    def test_env_header_comes_first_and_the_dump_right_after(self):
        outcome, backup = self.written_backup()

        self.assertEqual(backup, FRAME + DUMP)
        # Размер — самого дампа: по нему отличают пустой дамп от бэкапа из одного заголовка
        self.assertEqual(outcome, (len(DUMP), None))

    def test_without_env_the_backup_is_a_raw_dump(self):
        bk.read_panel_env.return_value = None

        outcome, backup = self.written_backup()

        self.assertEqual((outcome, backup), ((len(DUMP), None), DUMP))

    def test_dump_that_wrote_nothing_has_zero_size_despite_the_header(self):
        bk.subprocess.run.side_effect = pg_dump(dump=b"")

        outcome, backup = self.written_backup()

        self.assertEqual((outcome, backup), ((0, None), FRAME))


class CreateBackupTests(unittest.IsolatedAsyncioTestCase):
    """Кнопка «Создать бэкап»: готовый файл появляется целиком или не появляется вовсе."""

    NAME = "backup_20261114_040000.dump"

    def setUp(self):
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        self.backups = Path(workdir.name)
        self.docker = mock.Mock(side_effect=pg_dump())
        for target, name, replacement in (
            (bk, "BACKUP_DIR", self.backups),
            (bk, "_pg_env", mock.Mock(return_value=("panel", "paneldb"))),
            (bk, "read_panel_env", mock.Mock(return_value=ENV_TEXT)),
            (bk.subprocess, "run", self.docker),
        ):
            patcher = mock.patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        status = mock.patch.dict(bk._operation_status)
        status.start()
        self.addCleanup(status.stop)
        bk._set_status("creating", self.NAME)

    def files(self) -> set[str]:
        return {path.name for path in self.backups.iterdir()}

    async def test_finished_backup_is_saved_with_its_metadata(self):
        with self.assertLogs(bk.logger, level="INFO"):
            await bk._create_backup_task(self.NAME)

        self.assertEqual(self.files(), {self.NAME, f"{self.NAME}.meta.json"})
        self.assertEqual((self.backups / self.NAME).read_bytes(), FRAME + DUMP)
        meta = json.loads((self.backups / f"{self.NAME}.meta.json").read_text())
        self.assertEqual((meta["filename"], meta["size"]), (self.NAME, len(FRAME + DUMP)))
        self.assertEqual(
            (bk._operation_status["state"], bk._operation_status["filename"], bk._operation_status["error"]),
            ("idle", self.NAME, None),
        )

    async def test_backup_being_written_is_not_among_ready_ones(self):
        seen_while_dumping: list[set[str]] = []

        def dump_and_look(command, **options) -> subprocess.CompletedProcess:
            seen_while_dumping.append({path.name for path in self.backups.glob("*.dump")})
            return pg_dump()(command, **options)

        self.docker.side_effect = dump_and_look
        with self.assertLogs(bk.logger, level="INFO"):
            await bk._create_backup_task(self.NAME)

        self.assertEqual(seen_while_dumping, [set()])
        self.assertIn(self.NAME, self.files())

    async def test_failed_dump_leaves_no_file_and_reports_the_error(self):
        self.docker.side_effect = pg_dump(dump=b"PGDMP-half", returncode=1, stderr=b"pg_dump: error: out of memory")

        with self.assertLogs(bk.logger, level="ERROR"):
            await bk._create_backup_task(self.NAME)

        self.assertEqual(self.files(), set())
        self.assertEqual(
            (bk._operation_status["state"], bk._operation_status["error"]),
            ("idle", "pg_dump: error: out of memory"),
        )

    async def test_timed_out_dump_leaves_no_file_and_reports_the_timeout(self):
        self.docker.side_effect = subprocess.TimeoutExpired(cmd="docker exec panel-postgres pg_dump", timeout=600)

        with self.assertLogs(bk.logger, level="ERROR"):
            await bk._create_backup_task(self.NAME)

        self.assertEqual(self.files(), set())
        self.assertIn("timed out", bk._operation_status["error"])

    async def test_empty_dump_is_an_error_not_a_backup_of_the_env_alone(self):
        self.docker.side_effect = pg_dump(dump=b"")

        with self.assertLogs(bk.logger, level="ERROR"):
            await bk._create_backup_task(self.NAME)

        self.assertEqual(self.files(), set())
        self.assertEqual(bk._operation_status["error"], "Empty dump")

    async def test_leftover_of_an_interrupted_backup_is_removed(self):
        # Панель перезапустили посреди дампа: недописанный файл остался на томе
        (self.backups / "backup_20261113_040000.dump.part").write_bytes(b"MONBKP2\n" + b"x" * 100)

        with self.assertLogs(bk.logger, level="INFO"):
            await bk._create_backup_task(self.NAME)

        self.assertEqual(self.files(), {self.NAME, f"{self.NAME}.meta.json"})


if __name__ == "__main__":
    unittest.main()
