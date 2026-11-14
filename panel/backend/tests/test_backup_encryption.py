"""Шифрование локальных бэкапов паролем архива.

В файле бэкапа лежит весь .env панели: ключ шифрования секретов, JWT-секрет,
пароль базы — раньше в открытом виде, рядом с дампом. С заданным паролем архива
бэкап пишется как .dump.enc: тот же контейнер, что у набора томов для Telegram,
одним файлом. Без пароля бэкап остаётся прежним открытым .dump. Без Docker и
PostgreSQL: pg_dump — подмена subprocess.run.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.routers import backup as bk  # noqa: E402
from app.services import backup_telegram as bt  # noqa: E402

DUMP = b"PGDMP\x01\x0e\x00custom-format-dump"
ENV_TEXT = "PANEL_ENC_KEY=a2V5LW9mLXRoZS1wYW5lbA==\nJWT_SECRET=jwt-secret-value\nPOSTGRES_PASSWORD=db-password\n"
PLAIN_BACKUP = bk._frame_header(ENV_TEXT) + DUMP
PASSWORD = "archive-pw"
PLAIN_NAME = "backup_20261114_040000.dump"
ENCRYPTED_NAME = "backup_20261114_040000.dump.enc"
SECRETS_IN_CLEAR = (b"PANEL_ENC_KEY", b"a2V5LW9mLXRoZS1wYW5lbA==", b"jwt-secret-value", b"db-password", b"MONBKP2", b"PGDMP")


def pg_dump(dump: bytes = DUMP, returncode: int = 0, stderr: bytes = b""):
    """docker exec pg_dump: пишет в полученный дескриптор, как дочерний процесс."""
    def run(command, **options) -> subprocess.CompletedProcess:
        os.write(options["stdout"].fileno(), dump)
        return subprocess.CompletedProcess(command, returncode, stdout=None, stderr=stderr)
    return run


def decrypted(archive: bytes, password: str = PASSWORD) -> bytes:
    target = io.BytesIO()
    bt.decrypt_volumes([io.BytesIO(archive)], password, target)
    return target.getvalue()


class BackupDirTestCase(unittest.IsolatedAsyncioTestCase):
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
            # Стоимость вывода ключа здесь ни при чём, а шаг цикла событий она затягивает
            (bt, "_PBKDF2_ITERS", 1),
        ):
            patcher = mock.patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        status = mock.patch.dict(bk._operation_status)
        status.start()
        self.addCleanup(status.stop)
        bk._operation_status["state"] = "idle"

    def files(self) -> set[str]:
        return {path.name for path in self.backups.iterdir()}

    def put(self, name: str, content: bytes = b"x" * 32, age: int = 0) -> Path:
        """Файл в каталоге бэкапов; age — на сколько секунд он старше остальных."""
        path = self.backups / name
        path.write_bytes(content)
        stamp = 1_800_000_000 - age
        os.utime(path, (stamp, stamp))
        return path


class EncryptedBackupFileTests(BackupDirTestCase):
    async def create(self, name: str, password: str | None) -> bytes:
        bk._set_status("creating", name)
        with self.assertLogs(bk.logger, level="INFO"):
            await bk._create_backup_task(name, password)
        return (self.backups / name).read_bytes()

    async def test_backup_made_with_a_password_holds_nothing_in_clear(self):
        backup = await self.create(ENCRYPTED_NAME, PASSWORD)

        self.assertTrue(backup.startswith(b"MTGB1\n"))
        for secret in SECRETS_IN_CLEAR:
            self.assertNotIn(secret, backup)
        self.assertEqual(decrypted(backup), PLAIN_BACKUP)

    async def test_encrypted_backup_is_saved_whole_with_its_metadata(self):
        backup = await self.create(ENCRYPTED_NAME, PASSWORD)

        self.assertEqual(self.files(), {ENCRYPTED_NAME, f"{ENCRYPTED_NAME}.meta.json"})
        meta = json.loads((self.backups / f"{ENCRYPTED_NAME}.meta.json").read_text())
        self.assertEqual((meta["filename"], meta["size"]), (ENCRYPTED_NAME, len(backup)))
        self.assertEqual((bk._operation_status["state"], bk._operation_status["error"]), ("idle", None))

    async def test_encrypted_backup_being_written_is_not_among_ready_ones(self):
        seen_while_dumping: list[list[Path]] = []

        def dump_and_look(command, **options) -> subprocess.CompletedProcess:
            seen_while_dumping.append(bk._ready_backups())
            return pg_dump()(command, **options)

        self.docker.side_effect = dump_and_look
        await self.create(ENCRYPTED_NAME, PASSWORD)

        self.assertEqual(seen_while_dumping, [[]])

    async def test_open_backup_never_touches_the_backups_volume(self):
        # Открытый бэкап лежит во временном файле вне тома; на томе — только шифртекст
        seen_on_volume: list[bytes] = []
        real_encrypt = bt.encrypt_file

        def encrypt_and_look(source, password, target) -> None:
            real_encrypt(source, password, target)
            target.flush()
            seen_on_volume.extend(path.read_bytes() for path in self.backups.iterdir())

        with mock.patch.object(bt, "encrypt_file", encrypt_and_look):
            await self.create(ENCRYPTED_NAME, PASSWORD)

        self.assertEqual(len(seen_on_volume), 1)
        for secret in SECRETS_IN_CLEAR:
            self.assertNotIn(secret, seen_on_volume[0])

    async def test_dump_larger_than_one_piece_survives_the_round_trip(self):
        dump = b"PGDMP" + os.urandom(3 * 1024 * 1024 + 12345)
        self.docker.side_effect = pg_dump(dump)

        backup = await self.create(ENCRYPTED_NAME, PASSWORD)

        self.assertTrue(decrypted(backup) == bk._frame_header(ENV_TEXT) + dump, "дамп не совпал после расшифровки")

    async def test_encrypted_backup_opens_the_way_tg_restore_does(self):
        backup = await self.create(ENCRYPTED_NAME, PASSWORD)

        salt, nonce, ciphertext = backup[6:22], backup[22:34], backup[34:]
        self.assertEqual(AESGCM(bt._derive_key(PASSWORD, salt)).decrypt(nonce, ciphertext, None), PLAIN_BACKUP)

    async def test_failed_dump_leaves_nothing_behind(self):
        self.docker.side_effect = pg_dump(dump=b"PGDMP-half", returncode=1, stderr=b"pg_dump: error: out of memory")
        bk._set_status("creating", ENCRYPTED_NAME)

        with self.assertLogs(bk.logger, level="ERROR"):
            await bk._create_backup_task(ENCRYPTED_NAME, PASSWORD)

        self.assertEqual(self.files(), set())
        self.assertEqual(bk._operation_status["error"], "pg_dump: error: out of memory")

    async def test_empty_dump_is_an_error_with_a_password_too(self):
        self.docker.side_effect = pg_dump(dump=b"")
        bk._set_status("creating", ENCRYPTED_NAME)

        with self.assertLogs(bk.logger, level="ERROR"):
            await bk._create_backup_task(ENCRYPTED_NAME, PASSWORD)

        self.assertEqual(self.files(), set())
        self.assertEqual(bk._operation_status["error"], "Empty dump")

    async def test_without_a_password_the_backup_is_the_same_open_dump_as_before(self):
        self.assertEqual(await self.create(PLAIN_NAME, None), PLAIN_BACKUP)


class CreateEndpointTests(BackupDirTestCase):
    def setUp(self):
        super().setUp()
        self.saved_password = mock.AsyncMock(return_value=None)
        self.task = mock.AsyncMock()
        for name, replacement in (("_archive_password", self.saved_password), ("_create_backup_task", self.task)):
            patcher = mock.patch.object(bk, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def started_backup(self) -> tuple[str, str | None]:
        await bk.create_backup(_={})
        filename, password = self.task.call_args.args
        self.assertEqual(bk._operation_status["filename"], filename)
        return filename, password

    async def test_saved_archive_password_makes_the_backup_encrypted(self):
        self.saved_password.return_value = PASSWORD

        filename, password = await self.started_backup()

        self.assertRegex(filename, r"^backup_\d{8}_\d{6}\.dump\.enc$")
        self.assertEqual(password, PASSWORD)

    async def test_without_an_archive_password_the_backup_stays_plain(self):
        filename, password = await self.started_backup()

        self.assertRegex(filename, r"^backup_\d{8}_\d{6}\.dump$")
        self.assertIsNone(password)


class BackupShelfTests(BackupDirTestCase):
    """Список, скачивание, удаление и чистка видят оба вида бэкапов одинаково."""

    async def test_list_marks_encrypted_backups_and_skips_everything_else(self):
        self.put("backup_1.dump", age=20)
        self.put("backup_2.dump.enc", age=10)
        self.put("backup_2.dump.enc.meta.json", b'{"version": "10.80.0", "created_at": "2026-11-14T04:00:00+00:00"}')
        self.put("backup_3.dump.enc.part")
        self.put("backup_4.dump.part")
        self.put("notes.txt")

        listed = (await bk.list_backups(_={}))["backups"]

        self.assertEqual(
            [(b["filename"], b["encrypted"]) for b in listed],
            [("backup_2.dump.enc", True), ("backup_1.dump", False)],
        )
        self.assertEqual((listed[0]["version"], listed[0]["size"]), ("10.80.0", 32))

    async def test_download_gives_the_encrypted_file_as_it_is(self):
        stored = self.put("backup_2.dump.enc", b"MTGB1\n" + os.urandom(64))

        response = await bk.download_backup("backup_2.dump.enc", _={})

        self.assertEqual(Path(response.path), stored)
        self.assertEqual(response.media_type, "application/octet-stream")

    async def test_unfinished_and_service_files_are_not_downloadable(self):
        for name in ("backup_3.dump.enc.part", "backup_2.dump.enc.meta.json"):
            self.put(name)
            with self.subTest(name), self.assertRaises(HTTPException) as refused:
                await bk.download_backup(name, _={})
            self.assertEqual(refused.exception.status_code, 404)

    async def test_deleting_an_encrypted_backup_takes_its_metadata_along(self):
        self.put("backup_2.dump.enc")
        self.put("backup_2.dump.enc.meta.json", b"{}")
        self.put("backup_1.dump")

        await bk.delete_backup("backup_2.dump.enc", _={})

        self.assertEqual(self.files(), {"backup_1.dump"})

    async def test_rotation_counts_both_kinds_and_drops_the_oldest(self):
        self.put("backup_1.dump.enc", age=40)
        self.put("backup_1.dump.enc.meta.json", b"{}", age=40)
        self.put("backup_2.dump", age=30)
        self.put("backup_3.dump.enc", age=20)
        self.put("backup_4.dump", age=10)
        self.put("backup_5.dump.enc")

        with mock.patch.object(bk, "MAX_BACKUPS", 3):
            bk._cleanup_old_backups()

        self.assertEqual(self.files(), {"backup_3.dump.enc", "backup_4.dump", "backup_5.dump.enc"})


if __name__ == "__main__":
    unittest.main()
