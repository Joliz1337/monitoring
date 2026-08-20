import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers.backup import _frame_backup, _unframe_backup, _BACKUP_MAGIC  # noqa: E402

RAW_DUMP = b"PGDMP\x00\x01binary-dump-bytes"


class BackupFramingTests(unittest.TestCase):
    def test_frame_unframe_roundtrip(self):
        framed = _frame_backup(RAW_DUMP, "a2V5LWJhc2U2NA==")
        self.assertTrue(framed.startswith(_BACKUP_MAGIC))
        dump, key = _unframe_backup(framed)
        self.assertEqual(dump, RAW_DUMP)
        self.assertEqual(key, "a2V5LWJhc2U2NA==")

    def test_no_key_is_raw(self):
        self.assertEqual(_frame_backup(RAW_DUMP, None), RAW_DUMP)

    def test_legacy_dump_passthrough(self):
        dump, key = _unframe_backup(RAW_DUMP)
        self.assertEqual(dump, RAW_DUMP)
        self.assertIsNone(key)

    def test_dump_with_newlines_preserved(self):
        payload = b"PGDMP\nline1\nline2\x00\xff"
        framed = _frame_backup(payload, "k")
        dump, key = _unframe_backup(framed)
        self.assertEqual(dump, payload)
        self.assertEqual(key, "k")


if __name__ == "__main__":
    unittest.main()
