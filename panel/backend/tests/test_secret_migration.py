import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crypto  # noqa: E402
from app.database import needs_encryption  # noqa: E402


class SecretMigrationTests(unittest.TestCase):
    def setUp(self):
        crypto._reset_cache_for_tests()
        os.environ["PANEL_ENC_KEY"] = base64.b64encode(b"\x22" * 32).decode()

    def tearDown(self):
        os.environ.pop("PANEL_ENC_KEY", None)
        crypto._reset_cache_for_tests()

    def test_plaintext_needs_encryption(self):
        self.assertTrue(needs_encryption("-----BEGIN PRIVATE KEY-----"))

    def test_already_encrypted_skipped(self):
        self.assertFalse(needs_encryption(crypto.encrypt_secret("x")))

    def test_none_and_empty_skipped(self):
        self.assertFalse(needs_encryption(None))
        self.assertFalse(needs_encryption(""))


if __name__ == "__main__":
    unittest.main()
