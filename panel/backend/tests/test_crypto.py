import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crypto  # noqa: E402

KEY_B64 = base64.b64encode(b"\x11" * 32).decode()


class CryptoTests(unittest.TestCase):
    def setUp(self):
        crypto._reset_cache_for_tests()
        os.environ["PANEL_ENC_KEY"] = KEY_B64

    def tearDown(self):
        os.environ.pop("PANEL_ENC_KEY", None)
        crypto._reset_cache_for_tests()

    def test_roundtrip(self):
        enc = crypto.encrypt_secret("hunter2")
        self.assertTrue(enc.startswith(crypto.ENC_PREFIX))
        self.assertEqual(crypto.decrypt_secret(enc), "hunter2")

    def test_nonce_random_per_call(self):
        self.assertNotEqual(crypto.encrypt_secret("x"), crypto.encrypt_secret("x"))

    def test_legacy_plaintext_passthrough(self):
        self.assertEqual(crypto.decrypt_secret("-----BEGIN KEY-----"), "-----BEGIN KEY-----")

    def test_none_passthrough(self):
        self.assertIsNone(crypto.decrypt_secret(None))

    def test_no_key_rejects_write(self):
        os.environ.pop("PANEL_ENC_KEY", None)
        crypto._reset_cache_for_tests()
        self.assertFalse(crypto.encryption_enabled())
        with self.assertRaises(crypto.EncryptionUnavailable):
            crypto.encrypt_secret("x")

    def test_no_key_encrypted_reads_none(self):
        enc = crypto.encrypt_secret("secret")
        os.environ.pop("PANEL_ENC_KEY", None)
        crypto._reset_cache_for_tests()
        self.assertIsNone(crypto.decrypt_secret(enc))

    def test_corrupt_ciphertext_reads_none(self):
        self.assertIsNone(crypto.decrypt_secret(crypto.ENC_PREFIX + "not-base64!!"))

    def test_encrypted_string_bind_and_result(self):
        col = crypto.EncryptedString()
        bound = col.process_bind_param("topsecret", dialect=None)
        self.assertTrue(bound.startswith(crypto.ENC_PREFIX))
        self.assertEqual(col.process_result_value(bound, dialect=None), "topsecret")

    def test_encrypted_string_none(self):
        col = crypto.EncryptedString()
        self.assertIsNone(col.process_bind_param(None, dialect=None))
        self.assertIsNone(col.process_result_value(None, dialect=None))

    def test_encrypted_string_reads_legacy(self):
        col = crypto.EncryptedString()
        self.assertEqual(col.process_result_value("plain-pem", dialect=None), "plain-pem")


if __name__ == "__main__":
    unittest.main()
