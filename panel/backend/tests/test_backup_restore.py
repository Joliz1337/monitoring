"""Восстановление из бэкапа: дамп идёт в pg_restore с диска, а не из памяти бэкенда.

Без Docker и PostgreSQL — подменяется subprocess.run; подмена читает stdin так
же, как дочерний процесс: с позиции самого дескриптора. Загруженный бэкап
(обычный .dump или набор томов из Telegram) перекладывается в файл, и обе фазы
pg_restore получают из него только чистый дамп — без заголовка с .env.
"""
import asyncio
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.routers import backup as bk  # noqa: E402
from app.services import backup_telegram as bt  # noqa: E402
from app.services import fleet_history  # noqa: E402

DUMP = b"PGDMP\x01\x0e\x00" + os.urandom(5000)
ENV_TEXT = "PANEL_ENC_KEY=a2V5\nJWT_SECRET=jjj\nPOSTGRES_PASSWORD=frombackup\n"
V2_BACKUP = bk._frame_header(ENV_TEXT) + DUMP
V1_BACKUP = bk._BACKUP_MAGIC + b"a2V5\n" + DUMP
PASSWORD = "archive-pw"
VOLUME_BYTES = 2048


def backup_file(content: bytes):
    """Файл бэкапа на диске, каким его получает задача восстановления."""
    staged = tempfile.TemporaryFile()
    staged.write(content)
    staged.flush()
    return staged


def encrypted_backup(backup: bytes) -> io.BytesIO:
    """Локальный бэкап, зашифрованный паролем архива, — каким он лежит в .dump.enc."""
    encrypted = io.BytesIO()
    bt.encrypt_file(io.BytesIO(backup), PASSWORD, encrypted)
    encrypted.seek(0)
    return encrypted


def volume_set(backup: bytes) -> tuple[list[io.BytesIO], list[str]]:
    volumes = [io.BytesIO(volume) for volume in bt.EncryptedVolumes(io.BytesIO(backup), PASSWORD, VOLUME_BYTES)]
    return volumes, [f"panel-backup_20261114.enc.{number:03d}" for number in range(1, len(volumes) + 1)]


class FakeDocker:
    """docker exec вместо настоящего: помнит команды и что pg_restore прочитал со stdin."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.options: list[dict] = []
        self.fed_to_pg_restore: list[bytes] = []
        self.failures: dict[str, subprocess.CompletedProcess | Exception] = {}

    def fail(self, marker: str, returncode: int = 1, stderr: bytes = b"") -> None:
        self.failures[marker] = subprocess.CompletedProcess([], returncode, stdout=b"", stderr=stderr)

    def __call__(self, command: list[str], **options) -> subprocess.CompletedProcess:
        self.commands.append(command)
        self.options.append(options)
        if "pg_restore" in command:
            self.fed_to_pg_restore.append(self.read_like_a_child_process(options["stdin"]))
        for marker, failure in self.failures.items():
            if any(marker in argument for argument in command):
                if isinstance(failure, Exception):
                    raise failure
                return failure
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    @staticmethod
    def read_like_a_child_process(stdin) -> bytes:
        chunks = []
        while chunk := os.read(stdin.fileno(), 65536):
            chunks.append(chunk)
        return b"".join(chunks)

    def sections(self) -> list[list[str]]:
        return [
            [argument for argument in command if argument.startswith("--section=")]
            for command in self.commands if "pg_restore" in command
        ]


class RestoreTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.docker = FakeDocker()
        self.patch(bk.subprocess, "run", self.docker)
        self.patch(bk, "_pg_env", mock.Mock(return_value=("panel", "paneldb")))

    def patch(self, target, name: str, replacement):
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)
        return replacement


class PgRestoreInputTests(RestoreTestCase):
    def restore(self, content: bytes) -> str | None:
        with backup_file(content) as backup:
            dump_offset, _ = bk._read_frame(backup)
            return bk._run_pg_restore(backup, dump_offset)

    def test_both_phases_read_only_the_dump_whatever_the_backup_format(self):
        for name, content in (("v2 с .env", V2_BACKUP), ("v1 с ключом", V1_BACKUP), ("сырой pg_dump", DUMP)):
            with self.subTest(name):
                self.docker.fed_to_pg_restore.clear()

                self.assertIsNone(self.restore(content))

                self.assertEqual(self.docker.fed_to_pg_restore, [DUMP, DUMP])

    def test_dump_reaches_pg_restore_as_a_file_not_as_bytes_in_memory(self):
        self.restore(V2_BACKUP)

        for command, options in zip(self.docker.commands, self.docker.options):
            if "pg_restore" in command:
                self.assertNotIn("input", options)
                self.assertTrue(hasattr(options["stdin"], "fileno"))
                self.assertEqual(options["timeout"], bk.PG_RESTORE_PHASE_TIMEOUT_SEC)

    def test_steps_keep_their_order(self):
        self.restore(V2_BACKUP)

        steps = ["pg_restore" if "pg_restore" in command else command[-1].split()[0] for command in self.docker.commands]
        self.assertEqual(steps, ["SELECT", "DROP", "pg_restore", "DELETE", "pg_restore"])
        self.assertEqual(self.docker.sections(), [["--section=pre-data", "--section=data"], ["--section=post-data"]])
        self.assertEqual(
            self.docker.commands[2][:10],
            ["docker", "exec", "-i", "panel-postgres", "pg_restore", "-U", "panel", "-d", "paneldb", "--no-owner"],
        )

    def test_nonzero_exit_of_a_phase_is_the_error(self):
        self.docker.fail("--section=post-data", returncode=1, stderr=b"pg_restore: error: could not create unique index\nWARNING: skipped")

        error = self.restore(V2_BACKUP)

        self.assertIn("pg_restore post-data: exit code 1", error)
        self.assertIn("could not create unique index", error)
        self.assertNotIn("WARNING", error)

    def test_failed_schema_reset_stops_before_pg_restore(self):
        self.docker.fail("DROP SCHEMA", returncode=2, stderr=b"permission denied")

        error = self.restore(V2_BACKUP)

        self.assertEqual(error, "Schema reset failed: permission denied")
        self.assertEqual(self.docker.fed_to_pg_restore, [])


class RestoreTaskTests(RestoreTestCase):
    def setUp(self):
        super().setUp()
        self.in_maintenance = False
        self.maintenance_while_restoring: list[bool] = []
        self.patch(bk, "database_maintenance", self.maintenance)
        self.maintenance_while_revoking: list[bool] = []
        self.patch(bk, "revoke_sessions_after_restore", self.revoke_sessions)
        self.apply_env = self.patch(bk, "apply_restored_env", mock.Mock())
        status = mock.patch.dict(bk._operation_status)
        status.start()
        self.addCleanup(status.stop)
        bk._set_status("restoring", "backup.dump")

        real_docker = self.docker.__call__

        def docker(command, **options):
            self.maintenance_while_restoring.append(self.in_maintenance)
            return real_docker(command, **options)

        self.patch(bk.subprocess, "run", docker)

    @asynccontextmanager
    async def maintenance(self, reason: str):
        self.in_maintenance = True
        try:
            yield
        finally:
            self.in_maintenance = False

    async def revoke_sessions(self) -> None:
        self.maintenance_while_revoking.append(self.in_maintenance)

    def status(self) -> tuple[str, str | None, str | None]:
        return bk._operation_status["state"], bk._operation_status["filename"], bk._operation_status["error"]

    async def test_restored_backup_applies_its_env_and_frees_the_slot(self):
        backup = backup_file(V2_BACKUP)

        with self.assertLogs(bk.logger, level="INFO"):
            await bk._restore_backup_task(backup, "backup.dump")

        self.assertEqual(self.docker.fed_to_pg_restore, [DUMP, DUMP])
        self.assertEqual(self.status(), ("idle", "backup.dump", None))
        self.apply_env.assert_called_once_with(bk.parse_env(ENV_TEXT))
        self.assertIsNotNone(bk._operation_status["completed_at"])
        self.assertTrue(backup.closed)

    async def test_database_is_closed_to_the_panel_for_the_whole_restore(self):
        with self.assertLogs(bk.logger, level="INFO"):
            await bk._restore_backup_task(backup_file(V2_BACKUP), "backup.dump")

        self.assertEqual(self.maintenance_while_restoring, [True] * 5)
        self.assertFalse(self.in_maintenance)

    async def test_sessions_are_revoked_once_the_database_is_open_again(self):
        # В восстановленной базе поколение сессий старое — отозванные после бэкапа токены ожили бы
        with self.assertLogs(bk.logger, level="INFO"):
            await bk._restore_backup_task(backup_file(V2_BACKUP), "backup.dump")

        self.assertEqual(self.maintenance_while_revoking, [False])

    async def test_failed_restore_leaves_sessions_alone(self):
        self.docker.fail("--section=pre-data", returncode=1, stderr=b"pg_restore: error: input file is too short")

        with self.assertLogs(bk.logger, level="ERROR"):
            await bk._restore_backup_task(backup_file(V2_BACKUP), "backup.dump")

        self.assertEqual(self.maintenance_while_revoking, [])

    async def test_failed_pg_restore_is_reported_and_env_stays_untouched(self):
        self.docker.fail("--section=pre-data", returncode=1, stderr=b"pg_restore: error: input file is too short")
        backup = backup_file(V2_BACKUP)

        with self.assertLogs(bk.logger, level="ERROR"):
            await bk._restore_backup_task(backup, "backup.dump")

        state, filename, error = self.status()
        self.assertEqual((state, filename), ("idle", "backup.dump"))
        self.assertIn("pg_restore pre-data+data: exit code 1", error)
        self.apply_env.assert_not_called()
        self.assertTrue(backup.closed)
        self.assertFalse(self.in_maintenance)

    async def test_timed_out_pg_restore_is_reported_as_the_error(self):
        self.docker.failures["--section=pre-data"] = subprocess.TimeoutExpired(cmd="docker exec -i pg_restore", timeout=600)
        backup = backup_file(V2_BACKUP)

        with self.assertLogs(bk.logger, level="ERROR"):
            await bk._restore_backup_task(backup, "backup.dump")

        self.assertIn("timed out after 600 seconds", self.status()[2])
        self.assertEqual(self.status()[0], "idle")
        self.apply_env.assert_not_called()
        self.assertTrue(backup.closed)
        self.assertFalse(self.in_maintenance)

    async def test_fleet_history_counted_on_the_old_database_is_forgotten(self):
        fleet_history._cache[("24h", None)] = (0.0, {"scope": "fleet"})
        fleet_history._raw_series[("24h", None)] = fleet_history.RawSeries({}, settled_until=None)
        self.addCleanup(fleet_history.forget_fleet_history)

        with self.assertLogs(bk.logger, level="INFO"):
            await bk._restore_backup_task(backup_file(V2_BACKUP), "backup.dump")

        self.assertEqual((fleet_history._cache, fleet_history._raw_series), ({}, {}))


class StageBackupTests(unittest.TestCase):
    """Загруженные файлы → один файл бэкапа на диске."""

    def setUp(self):
        cheap_kdf = mock.patch.object(bt, "_PBKDF2_ITERS", 1)
        cheap_kdf.start()
        self.addCleanup(cheap_kdf.stop)
        self.staged_files: list = []
        real_temporary_file = tempfile.TemporaryFile

        def temporary_file():
            self.staged_files.append(real_temporary_file())
            return self.staged_files[-1]

        staging = mock.patch.object(bk.tempfile, "TemporaryFile", temporary_file)
        staging.start()
        self.addCleanup(staging.stop)

    def staged(self, uploads: list[io.BytesIO], filenames: list[str], password: str = "") -> bytes:
        with bk._stage_backup(uploads, filenames, bk.RestorePassword(password)) as backup:
            backup.seek(0)
            return backup.read()

    def assert_rejected(
        self, expected: str, uploads: list[io.BytesIO], filenames: list[str], password: str = "", from_settings: bool = False
    ) -> None:
        with self.assertRaises(HTTPException) as rejected:
            bk._stage_backup(uploads, filenames, bk.RestorePassword(password, from_settings))
        self.assertEqual(rejected.exception.status_code, 400)
        self.assertIn(expected, rejected.exception.detail)
        # Отвергнутая загрузка не оставляет после себя файла
        self.assertTrue(all(staged.closed for staged in self.staged_files))

    def test_plain_dump_is_copied_as_is(self):
        self.assertEqual(self.staged([io.BytesIO(V2_BACKUP)], ["backup.dump"]), V2_BACKUP)

    def test_volume_set_is_decrypted_into_the_backup(self):
        volumes, filenames = volume_set(V2_BACKUP)
        self.assertGreater(len(volumes), 2)

        self.assertEqual(self.staged(volumes, filenames, PASSWORD), V2_BACKUP)

    def test_wrong_password_is_rejected(self):
        volumes, filenames = volume_set(V2_BACKUP)
        self.assert_rejected("Не удалось расшифровать", volumes, filenames, "not-the-password")

    def test_set_without_its_last_volume_is_rejected(self):
        volumes, filenames = volume_set(V2_BACKUP)
        self.assert_rejected("Не удалось расшифровать", volumes[:-1], filenames[:-1], PASSWORD)

    def test_gap_in_the_set_is_named_before_the_password_is_asked(self):
        volumes, filenames = volume_set(V2_BACKUP)
        self.assert_rejected("не хватает тома .002", volumes[:1] + volumes[2:], filenames[:1] + filenames[2:])

    def test_volume_set_needs_a_password(self):
        volumes, filenames = volume_set(V2_BACKUP)
        self.assert_rejected("укажите пароль архива", volumes, filenames)

    def test_encrypted_local_backup_is_decrypted_like_a_set_of_one_file(self):
        self.assertEqual(self.staged([encrypted_backup(V2_BACKUP)], ["backup_20261114.dump.enc"], PASSWORD), V2_BACKUP)

    def test_encrypted_backup_under_any_name_is_told_by_its_content(self):
        self.assertEqual(self.staged([encrypted_backup(V2_BACKUP)], ["renamed.bin"], PASSWORD), V2_BACKUP)

    def test_wrong_password_for_an_encrypted_backup_is_rejected(self):
        self.assert_rejected(
            "Не удалось расшифровать", [encrypted_backup(V2_BACKUP)], ["backup.dump.enc"], "not-the-password"
        )

    def test_saved_password_that_does_not_fit_is_named_as_such(self):
        # Бэкап с другой панели или снятый до смены пароля: молча подставленный
        # пароль не должен выглядеть как опечатка в поле, которое оставили пустым
        self.assert_rejected(
            "Сохранённый пароль архива не подошёл", [encrypted_backup(V2_BACKUP)], ["backup.dump.enc"],
            "password-of-this-panel", from_settings=True,
        )

    def test_encrypted_backup_without_any_password_is_rejected(self):
        self.assert_rejected("укажите пароль архива", [encrypted_backup(V2_BACKUP)], ["backup.dump.enc"], from_settings=True)

    def test_damaged_encrypted_backup_is_rejected(self):
        damaged = bytearray(encrypted_backup(V2_BACKUP).getvalue())
        damaged[len(damaged) // 2] ^= 0x01
        self.assert_rejected("Не удалось расшифровать", [io.BytesIO(bytes(damaged))], ["backup.dump.enc"], PASSWORD)

    def test_several_plain_files_are_rejected(self):
        self.assert_rejected(
            "Несколько файлов", [io.BytesIO(V2_BACKUP), io.BytesIO(DUMP)], ["a.dump", "b.dump"]
        )

    def test_file_too_small_for_a_backup_is_rejected(self):
        self.assert_rejected("слишком мал", [io.BytesIO(b"PGDMP")], ["backup.dump"])


def multipart_request(files: list[tuple[str, bytes]], password: str | None = None) -> Request:
    boundary = "restore-test-boundary"
    body = b""
    for name, content in files:
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + content + b"\r\n"
    if password is not None:
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="password"\r\n\r\n{password}\r\n'.encode()
    body += f"--{boundary}--\r\n".encode()

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/backup/restore",
        "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
        "app": SimpleNamespace(),
    }
    return Request(scope, receive)


class RestoreEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cheap_kdf = mock.patch.object(bt, "_PBKDF2_ITERS", 1)
        cheap_kdf.start()
        self.addCleanup(cheap_kdf.stop)
        status = mock.patch.dict(bk._operation_status)
        status.start()
        self.addCleanup(status.stop)
        bk._operation_status["state"] = "idle"
        self.restored: list[tuple[bytes, str]] = []
        self.staged_files: list = []

        async def restore_task(backup, filename: str, app=None) -> None:
            self.staged_files.append(backup)
            backup.seek(0)
            self.restored.append((backup.read(), filename))
            backup.close()

        task = mock.patch.object(bk, "_restore_backup_task", restore_task)
        task.start()
        self.addCleanup(task.stop)
        self.saved_password = mock.AsyncMock(return_value=None)
        settings = mock.patch.object(bk, "_archive_password", self.saved_password)
        settings.start()
        self.addCleanup(settings.stop)

    async def background_tasks(self) -> None:
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        await asyncio.gather(*tasks)

    async def assert_refused(self, expected: str, files: list[tuple[str, bytes]], password: str | None = None) -> None:
        """Отказ 400 до всякого восстановления: задача не запущена, слот свободен."""
        with self.assertRaises(HTTPException) as rejected:
            await bk.restore_backup(multipart_request(files, password), _={})
        await self.background_tasks()

        self.assertEqual(rejected.exception.status_code, 400)
        self.assertIn(expected, rejected.exception.detail)
        self.assertEqual(self.restored, [])
        self.assertEqual(bk._operation_status["state"], "idle")

    async def test_encrypted_backup_opens_with_the_password_typed_in_the_form(self):
        encrypted = encrypted_backup(V2_BACKUP).getvalue()

        await bk.restore_backup(multipart_request([("backup_20261114.dump.enc", encrypted)], PASSWORD), _={})
        await self.background_tasks()

        self.assertEqual(self.restored, [(V2_BACKUP, "backup_20261114.dump.enc")])
        # Введённый пароль не подменяется сохранённым
        self.saved_password.assert_not_awaited()

    async def test_empty_password_field_falls_back_to_the_saved_archive_password(self):
        self.saved_password.return_value = PASSWORD
        encrypted = encrypted_backup(V2_BACKUP).getvalue()

        await bk.restore_backup(multipart_request([("backup_20261114.dump.enc", encrypted)]), _={})
        await self.background_tasks()

        self.assertEqual(self.restored, [(V2_BACKUP, "backup_20261114.dump.enc")])

    async def test_empty_password_field_opens_a_telegram_set_with_the_saved_password_too(self):
        self.saved_password.return_value = PASSWORD
        volumes, filenames = volume_set(V2_BACKUP)
        files = [(name, volume.getvalue()) for name, volume in zip(filenames, volumes)]

        await bk.restore_backup(multipart_request(files), _={})
        await self.background_tasks()

        self.assertEqual(self.restored, [(V2_BACKUP, filenames[0])])

    async def test_wrong_password_is_refused_before_anything_is_restored(self):
        encrypted = encrypted_backup(V2_BACKUP).getvalue()
        await self.assert_refused("Не удалось расшифровать", [("backup.dump.enc", encrypted)], "not-the-password")

    async def test_saved_password_of_another_panel_is_refused_before_anything_is_restored(self):
        self.saved_password.return_value = "password-of-this-panel"
        encrypted = encrypted_backup(V2_BACKUP).getvalue()
        await self.assert_refused("Сохранённый пароль архива не подошёл", [("backup.dump.enc", encrypted)])

    async def test_encrypted_backup_with_no_password_anywhere_is_refused(self):
        encrypted = encrypted_backup(V2_BACKUP).getvalue()
        await self.assert_refused("укажите пароль архива", [("backup.dump.enc", encrypted)])

    async def test_plain_dump_does_not_ask_the_settings_for_a_password(self):
        await bk.restore_backup(multipart_request([("backup.dump", V2_BACKUP)]), _={})
        await self.background_tasks()

        self.saved_password.assert_not_awaited()

    async def test_uploaded_dump_goes_to_the_restore_task_as_a_file(self):
        answer = await bk.restore_backup(multipart_request([("backup_20261114.dump", V2_BACKUP)]), _={})
        await self.background_tasks()

        self.assertEqual(answer, {"success": True, "message": "Restore started"})
        self.assertEqual(self.restored, [(V2_BACKUP, "backup_20261114.dump")])
        self.assertEqual((bk._operation_status["state"], bk._operation_status["operation"]), ("restoring", "restore"))

    async def test_uploaded_volumes_are_joined_by_name_and_decrypted(self):
        volumes, filenames = volume_set(V2_BACKUP)
        files = [(name, volume.getvalue()) for name, volume in zip(filenames, volumes)]

        await bk.restore_backup(multipart_request(files[::-1], PASSWORD), _={})
        await self.background_tasks()

        self.assertEqual(self.restored, [(V2_BACKUP, filenames[0])])

    async def test_request_without_a_file_is_rejected(self):
        with self.assertRaises(HTTPException) as rejected:
            await bk.restore_backup(multipart_request([], PASSWORD), _={})

        self.assertEqual((rejected.exception.status_code, rejected.exception.detail), (400, "Файл не выбран"))

    async def test_restore_started_meanwhile_wins_and_the_upload_is_dropped(self):
        staged_files = []
        real_stage = bk._stage_backup

        def stage_while_another_restore_starts(*args):
            staged_files.append(real_stage(*args))
            bk._operation_status["state"] = "restoring"
            return staged_files[-1]

        with mock.patch.object(bk, "_stage_backup", stage_while_another_restore_starts), \
                self.assertRaises(HTTPException) as rejected:
            await bk.restore_backup(multipart_request([("backup.dump", V2_BACKUP)]), _={})

        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(self.restored, [])
        self.assertTrue(staged_files[0].closed)

    async def test_busy_slot_rejects_before_the_upload_is_read(self):
        bk._operation_status["state"] = "creating"

        with self.assertRaises(HTTPException) as rejected:
            await bk.restore_backup(multipart_request([("backup.dump", V2_BACKUP)]), _={})

        self.assertEqual(rejected.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
