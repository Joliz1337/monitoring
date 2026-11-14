"""Отзыв сессий панели.

Выход раньше только удалял cookie в браузере: сам токен оставался действующим
до истечения срока, и украденный токен жил сутки. Теперь у сессий есть поколение:
оно хранится в panel_settings, вписано в токен, и выход его меняет — все выданные
токены перестают приниматься. Поколение читается из базы один раз на процесс.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException
from sqlalchemy.exc import OperationalError
from starlette.responses import Response

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import auth  # noqa: E402
from app.models import PanelSettings  # noqa: E402
from app.routers import auth_router  # noqa: E402

CLIENT_IP = "203.0.113.5"


class FakeSettingsDb:
    """panel_settings из одной строки session_epoch — и как сессия запроса, и как
    сессия, которую auth открывает сам при первом чтении поколения."""

    def __init__(self, stored: str | None = None):
        self.row = PanelSettings(key=auth.SESSION_EPOCH_KEY, value=stored) if stored else None
        self.reads = 0
        self.commit = mock.AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def scalar(self, statement):
        self.reads += 1
        if self.row is None:
            return None
        wants_whole_row = statement.column_descriptions[0]["expr"] is PanelSettings
        return self.row if wants_whole_row else self.row.value

    def add(self, row: PanelSettings) -> None:
        self.row = row


def request_with(token: str | None = None, bearer: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        cookies={"auth_token": token} if token else {},
        headers={"Authorization": f"Bearer {bearer}"} if bearer else {},
        client=SimpleNamespace(host=CLIENT_IP),
    )


class SessionCase(unittest.IsolatedAsyncioTestCase):
    def use_db(self, stored: str | None = None) -> FakeSettingsDb:
        """Процесс бэкенда «запущен заново»: поколение в памяти не прочитано."""
        db = FakeSettingsDb(stored)
        auth._session_epoch = None
        patcher = mock.patch.object(auth, "async_session_maker", return_value=db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, auth, "_session_epoch", None)
        return db

    async def log_in(self, db: FakeSettingsDb) -> tuple[dict, str]:
        response = Response()
        with mock.patch.object(auth, "ensure_not_banned", mock.AsyncMock()), \
                mock.patch.object(auth, "clear_failed_attempts", mock.AsyncMock()):
            body = await auth.login(auth.settings.panel_password, request_with(), response, db)
        cookie = response.headers["set-cookie"]
        return body, cookie.split("auth_token=", 1)[1].split(";", 1)[0]

    async def assert_rejected(self, token: str) -> None:
        with self.assertRaises(HTTPException) as caught:
            await auth.verify_auth(request_with(token))
        self.assertEqual(caught.exception.status_code, 401)


class LoginTests(SessionCase):
    async def test_token_goes_to_cookie_only(self):
        body, token = await self.log_in(self.use_db())

        self.assertEqual(body, {"success": True})
        self.assertEqual((await auth.verify_auth(request_with(token)))["sub"], "panel_user")

    async def test_token_carries_current_generation(self):
        _, token = await self.log_in(self.use_db(stored="17"))

        self.assertEqual(auth.verify_token(token)[auth.SESSION_EPOCH_CLAIM], 17)

    async def test_bearer_header_is_still_accepted(self):
        _, token = await self.log_in(self.use_db())

        self.assertIsNotNone(await auth.verify_auth(request_with(bearer=token)))


class GenerationCheckTests(SessionCase):
    async def test_generation_is_read_from_database_once(self):
        db = self.use_db(stored="17")
        token = auth.create_token({"sub": "panel_user", auth.SESSION_EPOCH_CLAIM: 17})

        for _ in range(50):
            await auth.verify_auth(request_with(token))

        self.assertEqual(db.reads, 1)

    async def test_token_of_another_generation_is_rejected(self):
        self.use_db(stored="17")

        await self.assert_rejected(auth.create_token({"sub": "panel_user", auth.SESSION_EPOCH_CLAIM: 16}))

    async def test_token_issued_before_generations_works_until_first_logout(self):
        db = self.use_db()
        legacy = auth.create_token({"sub": "panel_user", "ip": CLIENT_IP})

        self.assertIsNotNone(await auth.verify_auth(request_with(legacy)))

        await auth.logout(request_with(legacy), Response(), db)
        await self.assert_rejected(legacy)

    async def test_revoked_generation_survives_restart(self):
        db = self.use_db()
        _, token = await self.log_in(db)
        await auth.logout(request_with(token), Response(), db)

        restarted = self.use_db(stored=db.row.value)

        await self.assert_rejected(token)
        self.assertEqual(restarted.reads, 1)


class LogoutTests(SessionCase):
    async def test_logout_ends_every_session(self):
        db = self.use_db()
        _, stolen = await self.log_in(db)
        _, own = await self.log_in(db)

        await auth.logout(request_with(own), Response(), db)

        await self.assert_rejected(stolen)
        await self.assert_rejected(own)
        db.commit.assert_awaited_once()

    async def test_login_after_logout_gives_working_session(self):
        db = self.use_db()
        _, old = await self.log_in(db)
        await auth.logout(request_with(old), Response(), db)

        _, fresh = await self.log_in(db)

        self.assertIsNotNone(await auth.verify_auth(request_with(fresh)))

    async def test_each_logout_starts_a_newer_generation(self):
        db = self.use_db()
        generations = []
        for _ in range(3):
            _, token = await self.log_in(db)
            await auth.logout(request_with(token), Response(), db)
            generations.append(int(db.row.value))

        self.assertEqual(generations, sorted(set(generations)))

    async def test_logout_without_valid_session_revokes_nothing(self):
        db = self.use_db()
        _, token = await self.log_in(db)
        response = Response()

        for stranger in (request_with(), request_with("garbage")):
            await auth.logout(stranger, response, db)

        self.assertIsNotNone(await auth.verify_auth(request_with(token)))
        db.commit.assert_not_awaited()
        self.assertIn("auth_token=", response.headers["set-cookie"])

    async def test_restore_from_backup_ends_sessions_and_outruns_the_old_generation(self):
        db = self.use_db()
        _, revoked_before_restore = await self.log_in(db)
        await auth.logout(request_with(revoked_before_restore), Response(), db)
        _, current = await self.log_in(db)
        generation_before_restore = int(db.row.value)

        # База из бэкапа: поколения сессий в ней ещё нет
        db.row = None
        await auth.revoke_sessions_after_restore()

        await self.assert_rejected(current)
        self.assertGreater(int(db.row.value), generation_before_restore)
        self.use_db(stored=db.row.value)
        await self.assert_rejected(revoked_before_restore)

    async def test_database_failure_after_restore_is_not_raised(self):
        db = self.use_db()
        _, token = await self.log_in(db)
        db.commit.side_effect = OperationalError("UPDATE", {}, Exception("db is down"))

        with self.assertLogs(auth.logger, level="ERROR"):
            await auth.revoke_sessions_after_restore()

        self.assertIsNotNone(await auth.verify_auth(request_with(token)))

    async def test_endpoint_revokes_and_reports_success(self):
        db = self.use_db()
        _, token = await self.log_in(db)

        body = await auth_router.auth_logout(request_with(token), Response(), db)

        self.assertEqual(body, {"success": True})
        await self.assert_rejected(token)


if __name__ == "__main__":
    unittest.main()
