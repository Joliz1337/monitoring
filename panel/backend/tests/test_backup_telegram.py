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


if __name__ == "__main__":
    unittest.main()
