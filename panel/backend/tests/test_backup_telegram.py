import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.exceptions import InvalidTag  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

from app.services import backup_telegram as bt  # noqa: E402


class BackupTelegramCryptoTests(unittest.TestCase):
    def test_roundtrip_single_volume(self):
        data = b"a pg_dump payload with secrets"
        parts = bt.encrypt_and_split(data, "s3cret", 1024 * 1024)
        self.assertEqual(len(parts), 1)
        self.assertEqual(bt.join_and_decrypt(parts, "s3cret"), data)

    def test_roundtrip_many_volumes(self):
        data = os.urandom(500_000)
        parts = bt.encrypt_and_split(data, "pw", 100_000)
        self.assertGreater(len(parts), 4)  # разбилось на тома
        self.assertEqual(bt.join_and_decrypt(parts, "pw"), data)

    def test_wrong_password_fails(self):
        parts = bt.encrypt_and_split(b"data", "right", 1024 * 1024)
        with self.assertRaises(InvalidTag):
            bt.join_and_decrypt(parts, "wrong")

    def test_reordered_volumes_fail(self):
        parts = bt.encrypt_and_split(os.urandom(300_000), "pw", 100_000)
        reordered = [parts[1], parts[0]] + parts[2:]
        with self.assertRaises((InvalidTag, ValueError)):
            bt.join_and_decrypt(reordered, "pw")

    def test_empty_password_rejected(self):
        with self.assertRaises(ValueError):
            bt.encrypt_and_split(b"data", "", 1024 * 1024)


class VolumeLayoutTests(unittest.TestCase):
    """Тома — подряд идущие срезы одного архива «сигнатура, соль, nonce, шифртекст».

    Раскладку байтов по томам менять нельзя: наборы собирают и окно
    восстановления панели, и отдельный tg-restore.py.
    """

    VOLUME = 4096
    GCM_TAG_LEN = 16
    ENVELOPE = len(bt._MAGIC) + bt._SALT_LEN + bt._NONCE_LEN + GCM_TAG_LEN

    def setUp(self):
        # Раскладка от стоимости вывода ключа не зависит — считать его всерьёз незачем
        cheap_kdf = mock.patch.object(bt, "_PBKDF2_ITERS", 1)
        cheap_kdf.start()
        self.addCleanup(cheap_kdf.stop)

    def test_volumes_are_consecutive_slices_of_one_archive(self):
        full = self.VOLUME
        for archive_len in (full - 1, full, full + 1, 3 * full, 3 * full + 7):
            with self.subTest(archive_len=archive_len):
                data = os.urandom(archive_len - self.ENVELOPE)
                salt, nonce = os.urandom(bt._SALT_LEN), os.urandom(bt._NONCE_LEN)
                with mock.patch.object(bt.os, "urandom", side_effect=[salt, nonce]):
                    parts = bt.encrypt_and_split(data, "pw", full)

                ciphertext = AESGCM(bt._derive_key("pw", salt)).encrypt(nonce, data, None)
                archive = bt._MAGIC + salt + nonce + ciphertext
                self.assertEqual(len(archive), archive_len)
                self.assertEqual(list(parts), [archive[i:i + full] for i in range(0, archive_len, full)])
                self.assertEqual(len(parts), -(-archive_len // full))

    def test_volumes_read_like_a_list_of_bytes(self):
        parts = bt.encrypt_and_split(os.urandom(3 * self.VOLUME + 7 - self.ENVELOPE), "pw", self.VOLUME)
        by_index = [parts[i] for i in range(4)]

        self.assertEqual(list(parts), by_index)
        self.assertEqual(parts[-1], by_index[3])
        self.assertEqual(parts[1:3], by_index[1:3])
        with self.assertRaises(IndexError):
            parts[4]
        # httpx отправляет файлом только bytes
        self.assertEqual({type(part) for part in parts}, {bytes})


class VolumeSetDetectionTests(unittest.TestCase):
    def test_first_volume_carries_signature(self):
        parts = bt.encrypt_and_split(os.urandom(300_000), "pw", 100_000)
        self.assertTrue(bt.is_encrypted_set(parts[0]))
        self.assertFalse(bt.is_encrypted_set(parts[1]))

    def test_plain_dump_is_not_a_set(self):
        self.assertFalse(bt.is_encrypted_set(b"PGDMP\x01\x0e\x00\x04"))
        self.assertFalse(bt.is_encrypted_set(b""))

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
