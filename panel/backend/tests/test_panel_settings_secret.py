"""Секретные значения в общей таблице panel_settings.

Таблица хранит все настройки панели подряд, и шифровать нужно только токен
Cloudflare. Колонка ключа строки не видит: что шифровать, решает модель при
присвоении, а читается любое значение — зашифрованное расшифровывается,
обычное отдаётся как есть.
"""
import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crypto  # noqa: E402
from app.models import PanelSettings  # noqa: E402

CF_TOKEN_KEY = "wildcard_cloudflare_api_token"
VALUE_TYPE = PanelSettings.__table__.c.value.type


def stored(row: PanelSettings) -> str | None:
    """Что уйдёт в базу при записи строки."""
    return VALUE_TYPE.process_bind_param(row.value, dialect=None)


class PanelSettingsSecretTests(unittest.TestCase):
    def setUp(self):
        crypto._reset_cache_for_tests()
        os.environ["PANEL_ENC_KEY"] = base64.b64encode(b"\x44" * 32).decode()

    def tearDown(self):
        os.environ.pop("PANEL_ENC_KEY", None)
        crypto._reset_cache_for_tests()

    def test_new_secret_setting_is_stored_encrypted(self):
        row = PanelSettings(key=CF_TOKEN_KEY, value="cf-token")

        self.assertTrue(stored(row).startswith(crypto.ENC_PREFIX))
        self.assertEqual(crypto.decrypt_secret(stored(row)), "cf-token")

    def test_replaced_secret_setting_is_stored_encrypted(self):
        row = PanelSettings(key=CF_TOKEN_KEY, value="old-token")
        row.value = "new-token"

        self.assertEqual(crypto.decrypt_secret(stored(row)), "new-token")

    def test_secret_stays_readable_on_the_object(self):
        row = PanelSettings(key=CF_TOKEN_KEY, value="cf-token")

        self.assertEqual(row.value, "cf-token")

    def test_ordinary_setting_is_stored_as_is(self):
        row = PanelSettings(key="refresh_interval", value="5")

        self.assertEqual(stored(row), "5")

    def test_cleared_secret_is_not_encrypted(self):
        for empty in ("", None):
            row = PanelSettings(key=CF_TOKEN_KEY, value=empty)

            self.assertEqual(stored(row), empty)

    def test_encrypted_value_is_decrypted_on_read(self):
        encrypted = crypto.encrypt_secret("cf-token")

        self.assertEqual(VALUE_TYPE.process_result_value(encrypted, dialect=None), "cf-token")

    def test_plain_value_is_read_as_is(self):
        self.assertEqual(VALUE_TYPE.process_result_value("5", dialect=None), "5")
        self.assertIsNone(VALUE_TYPE.process_result_value(None, dialect=None))

    def test_secret_not_yet_migrated_is_read_as_is(self):
        """До первого старта с миграцией токен лежит открытым текстом — выпуск
        сертификата не должен на этом сломаться."""
        self.assertEqual(VALUE_TYPE.process_result_value("legacy-cf-token", dialect=None), "legacy-cf-token")


if __name__ == "__main__":
    unittest.main()
