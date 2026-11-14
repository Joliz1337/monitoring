"""Секретные ключи panel_settings закрыты для общего API настроек.

Токен Cloudflare лежит в одной таблице с обычными настройками, и
GET /api/settings отдавал его вместе с ними. Таблица поднята в SQLite в памяти,
запросы роутера выполняются по-настоящему.
"""
import base64
import os
import sys
import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crypto  # noqa: E402
from app.models import PanelSettings  # noqa: E402
from app.routers import settings as settings_router  # noqa: E402

CF_TOKEN_KEY = "wildcard_cloudflare_api_token"


class SettingsDb:
    def __init__(self, rows: dict[str, str]):
        engine = create_engine("sqlite://")
        PanelSettings.__table__.create(engine)
        self.session = Session(engine, expire_on_commit=False)
        self.session.add_all(PanelSettings(key=key, value=value) for key, value in rows.items())
        self.session.commit()

    async def execute(self, statement):
        return self.session.execute(statement)

    async def commit(self) -> None:
        self.session.commit()

    def add(self, row: PanelSettings) -> None:
        self.session.add(row)

    def stored(self, key: str) -> str | None:
        return self.session.execute(
            text("SELECT value FROM panel_settings WHERE key = :key"), {"key": key},
        ).scalar()


class SettingsSecretKeysTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        crypto._reset_cache_for_tests()
        os.environ["PANEL_ENC_KEY"] = base64.b64encode(b"\x55" * 32).decode()
        self.db = SettingsDb({
            CF_TOKEN_KEY: "cf-token",
            "refresh_interval": "7",
            "wildcard_email": "admin@example.com",
        })

    def tearDown(self):
        self.db.session.close()
        os.environ.pop("PANEL_ENC_KEY", None)
        crypto._reset_cache_for_tests()

    async def test_list_does_not_contain_the_secret(self):
        response = await settings_router.get_all_settings(self.db, {})

        self.assertNotIn(CF_TOKEN_KEY, response["settings"])
        self.assertNotIn("cf-token", str(response))

    async def test_list_keeps_ordinary_settings_and_defaults(self):
        response = await settings_router.get_all_settings(self.db, {})

        self.assertEqual(response["settings"]["refresh_interval"], "7")
        self.assertEqual(response["settings"]["wildcard_email"], "admin@example.com")
        self.assertEqual(response["settings"]["compact_view"], "false")

    async def test_secret_cannot_be_overwritten_through_the_generic_endpoint(self):
        with self.assertRaises(HTTPException) as ctx:
            await settings_router.update_setting(
                CF_TOKEN_KEY, settings_router.SettingUpdate(value="attacker-token"), self.db, {},
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(crypto.decrypt_secret(self.db.stored(CF_TOKEN_KEY)), "cf-token")

    async def test_ordinary_setting_is_still_saved(self):
        response = await settings_router.update_setting(
            "refresh_interval", settings_router.SettingUpdate(value="15"), self.db, {},
        )

        self.assertEqual(response, {"success": True, "key": "refresh_interval", "value": "15"})
        self.assertEqual(self.db.stored("refresh_interval"), "15")


if __name__ == "__main__":
    unittest.main()
