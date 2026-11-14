import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.exceptions import InvalidTag  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

from app.services import backup_telegram as bt  # noqa: E402


def encrypt(data: bytes, password: str, volume_bytes: int) -> list[bytes]:
    return list(bt.EncryptedVolumes(io.BytesIO(data), password, volume_bytes))


def decrypt(volumes: list[bytes], password: str) -> bytes:
    target = io.BytesIO()
    bt.decrypt_volumes([io.BytesIO(volume) for volume in volumes], password, target)
    return target.getvalue()


class BackupTelegramCryptoTests(unittest.TestCase):
    def test_roundtrip_single_volume(self):
        data = b"a pg_dump payload with secrets"
        parts = encrypt(data, "s3cret", 1024 * 1024)
        self.assertEqual(len(parts), 1)
        self.assertEqual(decrypt(parts, "s3cret"), data)

    def test_roundtrip_many_volumes(self):
        data = os.urandom(500_000)
        parts = encrypt(data, "pw", 100_000)
        self.assertGreater(len(parts), 4)  # разбилось на тома
        self.assertEqual(decrypt(parts, "pw"), data)

    def test_wrong_password_fails(self):
        parts = encrypt(b"data", "right", 1024 * 1024)
        with self.assertRaises(InvalidTag):
            decrypt(parts, "wrong")

    def test_reordered_volumes_fail(self):
        parts = encrypt(os.urandom(300_000), "pw", 100_000)
        reordered = [parts[1], parts[0]] + parts[2:]
        with self.assertRaises((InvalidTag, ValueError)):
            decrypt(reordered, "pw")

    def test_set_without_its_last_volume_fails(self):
        # По именам отсутствие последнего тома не видно — его ловит только проверка подлинности
        parts = encrypt(os.urandom(300_000), "pw", 100_000)
        with self.assertRaises((InvalidTag, ValueError)):
            decrypt(parts[:-1], "pw")

    def test_set_cut_inside_its_header_fails(self):
        parts = encrypt(b"data", "pw", 1024 * 1024)
        with self.assertRaises((InvalidTag, ValueError)):
            decrypt([parts[0][:20]], "pw")

    def test_empty_password_rejected(self):
        with self.assertRaises(ValueError):
            encrypt(b"data", "", 1024 * 1024)


class VolumeLayoutTests(unittest.TestCase):
    """Тома — подряд идущие срезы одного архива «сигнатура, соль, nonce, шифртекст».

    Раскладку байтов по томам менять нельзя: наборы собирают и окно
    восстановления панели, и отдельный tg-restore.py. Эталон здесь — шифрование
    одним вызовом AESGCM: им писались наборы, уже лежащие в Telegram, им же
    расшифровывает tg-restore.py.
    """

    VOLUME = 4096
    GCM_TAG_LEN = 16
    ENVELOPE = len(bt._MAGIC) + bt._SALT_LEN + bt._NONCE_LEN + GCM_TAG_LEN

    def setUp(self):
        # Раскладка от стоимости вывода ключа не зависит — считать его всерьёз незачем
        cheap_kdf = mock.patch.object(bt, "_PBKDF2_ITERS", 1)
        cheap_kdf.start()
        self.addCleanup(cheap_kdf.stop)

    def one_call_archive(self, data: bytes, salt: bytes, nonce: bytes) -> bytes:
        return bt._MAGIC + salt + nonce + AESGCM(bt._derive_key("pw", salt)).encrypt(nonce, data, None)

    def test_volumes_are_consecutive_slices_of_one_archive(self):
        full = self.VOLUME
        # full + 9 и 3 * full + 7 — тег подлинности разорван между двумя последними томами
        for archive_len in (self.ENVELOPE, full - 1, full, full + 1, full + 9, 3 * full, 3 * full + 7):
            with self.subTest(archive_len=archive_len):
                data = os.urandom(archive_len - self.ENVELOPE)
                salt, nonce = os.urandom(bt._SALT_LEN), os.urandom(bt._NONCE_LEN)
                with mock.patch.object(bt.os, "urandom", side_effect=[salt, nonce]):
                    volumes = bt.EncryptedVolumes(io.BytesIO(data), "pw", full)
                parts = list(volumes)

                archive = self.one_call_archive(data, salt, nonce)
                self.assertEqual(len(archive), archive_len)
                self.assertEqual(parts, [archive[i:i + full] for i in range(0, archive_len, full)])
                # Число томов уходит в подпись первого — оно известно до шифрования
                self.assertEqual(len(volumes), len(parts))

    def test_layout_does_not_depend_on_how_the_file_is_read(self):
        data = os.urandom(3 * self.VOLUME + 7)
        salt, nonce = os.urandom(bt._SALT_LEN), os.urandom(bt._NONCE_LEN)
        archive = self.one_call_archive(data, salt, nonce)
        for read_chunk in (1, 1000, self.VOLUME, 10 * self.VOLUME):
            with self.subTest(read_chunk=read_chunk), \
                    mock.patch.object(bt, "_READ_CHUNK_BYTES", read_chunk), \
                    mock.patch.object(bt.os, "urandom", side_effect=[salt, nonce]):
                parts = encrypt(data, "pw", self.VOLUME)
                self.assertEqual(b"".join(parts), archive)
                self.assertEqual(decrypt(parts, "pw"), data)

    def test_set_made_by_one_call_encryption_still_restores(self):
        data = os.urandom(3 * self.VOLUME)
        archive = self.one_call_archive(data, os.urandom(bt._SALT_LEN), os.urandom(bt._NONCE_LEN))
        old_set = [archive[i:i + self.VOLUME] for i in range(0, len(archive), self.VOLUME)]

        self.assertEqual(decrypt(old_set, "pw"), data)

    def test_new_set_opens_with_one_call_decryption(self):
        data = os.urandom(3 * self.VOLUME)
        blob = b"".join(encrypt(data, "pw", self.VOLUME))
        header_len = len(bt._MAGIC) + bt._SALT_LEN + bt._NONCE_LEN
        salt, nonce = blob[len(bt._MAGIC):header_len - bt._NONCE_LEN], blob[header_len - bt._NONCE_LEN:header_len]

        self.assertEqual(AESGCM(bt._derive_key("pw", salt)).decrypt(nonce, blob[header_len:], None), data)

    def test_volumes_are_plain_bytes(self):
        # httpx отправляет файлом только bytes
        parts = encrypt(os.urandom(3 * self.VOLUME + 7 - self.ENVELOPE), "pw", self.VOLUME)
        self.assertEqual(len(parts), 4)
        self.assertEqual({type(part) for part in parts}, {bytes})


class VolumeSetDetectionTests(unittest.TestCase):
    def test_first_volume_carries_signature(self):
        parts = encrypt(os.urandom(300_000), "pw", 100_000)
        self.assertTrue(bt.is_encrypted_set(io.BytesIO(parts[0])))
        self.assertFalse(bt.is_encrypted_set(io.BytesIO(parts[1])))

    def test_plain_dump_is_not_a_set(self):
        self.assertFalse(bt.is_encrypted_set(io.BytesIO(b"PGDMP\x01\x0e\x00\x04")))
        self.assertFalse(bt.is_encrypted_set(io.BytesIO(b"")))

    def test_looking_at_the_signature_leaves_the_file_at_its_start(self):
        volume = io.BytesIO(encrypt(b"data", "pw", 1024 * 1024)[0])
        volume.seek(10)

        self.assertTrue(bt.is_encrypted_set(volume))
        self.assertEqual(volume.tell(), 0)

    def test_complete_set_has_no_gaps(self):
        names = ["panel-backup_2026-08-26.enc.003", "panel-backup_2026-08-26.enc.001", "panel-backup_2026-08-26.enc.002"]
        self.assertEqual(bt.missing_volume_numbers(names), [])

    def test_gap_in_the_middle_is_reported(self):
        names = ["x.enc.001", "x.enc.004"]
        self.assertEqual(bt.missing_volume_numbers(names), [2, 3])

    def test_set_without_first_volume_is_reported(self):
        self.assertEqual(bt.missing_volume_numbers(["x.enc.002"]), [1])

    def test_names_that_are_not_volumes_are_ignored(self):
        self.assertEqual(bt.missing_volume_numbers(["backup_20260826.dump"]), [])
        self.assertEqual(bt.missing_volume_numbers(["x.enc.001", "notes.txt"]), [])
        self.assertEqual(bt.missing_volume_numbers([]), [])


if __name__ == "__main__":
    unittest.main()
