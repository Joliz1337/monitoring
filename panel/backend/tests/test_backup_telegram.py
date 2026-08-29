import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography.exceptions import InvalidTag  # noqa: E402

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
