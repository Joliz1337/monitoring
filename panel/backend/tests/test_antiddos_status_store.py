"""Тесты записи статуса сторожа в строку сервера (app/services/antiddos_manager.py).

Опрос статуса раз в минуту переписывал строку каждого сервера, даже когда на
ноде ничего не менялось: на парке в 2000 нод это 2000 UPDATE в минуту ради
отметки времени. Теперь строка пишется, когда состояние изменилось либо отметка
синхронизации старше пяти минут. Алерт о самовключении аварийного режима при
этом обязан приходить, как и раньше, — и уходит с повтором при сбое сети.

Голый unittest, без PostgreSQL и без сети.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import AlertHistory, AlertSettings, Server  # noqa: E402
from app.services import antiddos_manager, telegram_bot  # noqa: E402
from app.services.antiddos_manager import AntiDdosManager  # noqa: E402

ATTACK_STARTED = 1_760_000_000


class Sessions:
    """Фабрика сессий без базы: считает, сколько раз в базу ходили и писали."""

    def __init__(self, rows: dict[type, list]) -> None:
        self.rows = rows
        self.opened = 0
        self.commits = 0
        self.added: list = []

    def __call__(self) -> "Sessions":
        self.opened += 1
        return self

    async def __aenter__(self) -> "Sessions":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def execute(self, statement):
        entities = self.rows.get(statement.column_descriptions[0]["entity"], [])
        return SimpleNamespace(scalar_one_or_none=lambda: entities[0] if entities else None)

    def add(self, entity) -> None:
        self.added.append(entity)

    async def commit(self) -> None:
        self.commits += 1


def minutes_ago(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def server(**columns) -> Server:
    defaults = dict(
        antiddos_emergency_mode=False, antiddos_source="none", antiddos_reason="",
        antiddos_watchdog=True, antiddos_since=None, antiddos_last_sync_at=minutes_ago(1),
    )
    return Server(id=1, name="nl-1", url="https://5.9.0.10:9100", **{**defaults, **columns})


def calm() -> dict:
    return {"mode": "off", "source": "none", "since": 0, "reason": "", "watchdog": "on"}


def under_attack(source: str = "auto") -> dict:
    return {"mode": "on", "source": source, "since": ATTACK_STARTED, "reason": "syncookies", "watchdog": "on"}


class StoreCase(unittest.IsolatedAsyncioTestCase):
    def prepare(self, row: Server) -> None:
        self.row = row
        self.sessions = Sessions({Server: [row]})
        patcher = mock.patch.object(antiddos_manager, "async_session", self.sessions)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = AntiDdosManager()
        self.manager._send_alert = mock.AsyncMock()


class WriteOnlyOnChangeTest(StoreCase):
    async def test_unchanged_node_does_not_touch_the_database_on_a_poll(self):
        self.prepare(server())

        await self.manager._store_status(1, calm(), alert=True, known=self.row)

        self.assertEqual(self.sessions.opened, 0)

    async def test_unchanged_node_is_not_rewritten_by_an_action_either(self):
        self.prepare(server())

        await self.manager._store_status(1, calm())

        self.assertEqual(self.sessions.commits, 0)

    async def test_sync_mark_is_refreshed_every_five_minutes(self):
        self.prepare(server(antiddos_last_sync_at=minutes_ago(6)))

        await self.manager._store_status(1, calm(), alert=True, known=self.row)

        self.assertEqual(self.sessions.commits, 1)
        self.assertLess(datetime.now(timezone.utc) - self.row.antiddos_last_sync_at, timedelta(seconds=5))

    async def test_node_never_synced_is_written(self):
        self.prepare(server(antiddos_source=None, antiddos_reason=None, antiddos_last_sync_at=None))

        await self.manager._store_status(1, calm(), alert=True, known=self.row)

        self.assertEqual(self.sessions.commits, 1)
        self.assertEqual((self.row.antiddos_source, self.row.antiddos_reason), ("none", ""))

    async def test_any_changed_field_is_written_at_once(self):
        changes = {
            "mode": {"mode": "on"},
            "source": {"source": "manual"},
            "reason": {"reason": "softnet drops"},
            "watchdog": {"watchdog": "off"},
            "since": {"since": ATTACK_STARTED},
        }
        for name, change in changes.items():
            with self.subTest(name):
                self.prepare(server())

                await self.manager._store_status(1, {**calm(), **change}, known=self.row)

                self.assertEqual(self.sessions.commits, 1)

    async def test_written_columns_match_what_the_node_reported(self):
        self.prepare(server())

        await self.manager._store_status(1, {**under_attack("manual"), "watchdog": "off"}, known=self.row)

        self.assertEqual(
            (self.row.antiddos_emergency_mode, self.row.antiddos_source, self.row.antiddos_reason,
             self.row.antiddos_watchdog, self.row.antiddos_since),
            (True, "manual", "syncookies", False, datetime.fromtimestamp(ATTACK_STARTED, tz=timezone.utc)),
        )

    async def test_timestamps_stored_without_a_zone_are_read_as_utc(self):
        # Колонки старых установок заведены как TIMESTAMP без зоны
        self.prepare(server(
            antiddos_emergency_mode=True, antiddos_source="auto", antiddos_reason="syncookies",
            antiddos_since=datetime.fromtimestamp(ATTACK_STARTED, tz=timezone.utc).replace(tzinfo=None),
            antiddos_last_sync_at=minutes_ago(1).replace(tzinfo=None),
        ))

        await self.manager._store_status(1, under_attack(), alert=True, known=self.row)

        self.assertEqual(self.sessions.opened, 0)

    async def test_row_changed_since_the_pass_started_is_judged_by_the_database(self):
        # Снимок начала прохода устарел: админ уже включил режим, и запись свежая
        snapshot = server()
        self.prepare(server(
            antiddos_emergency_mode=True, antiddos_source="manual", antiddos_reason="syncookies",
            antiddos_since=datetime.fromtimestamp(ATTACK_STARTED, tz=timezone.utc),
        ))

        await self.manager._store_status(1, under_attack("manual"), alert=True, known=snapshot)

        self.assertEqual((self.sessions.opened, self.sessions.commits), (1, 0))

    async def test_removed_server_is_skipped(self):
        self.prepare(server())
        self.sessions.rows[Server] = []

        await self.manager._store_status(1, under_attack())

        self.assertEqual(self.sessions.commits, 0)


class AutoEmergencyAlertTest(StoreCase):
    async def test_node_that_switched_itself_on_raises_an_alert(self):
        self.prepare(server())

        await self.manager._store_status(1, under_attack(), alert=True, known=self.row)

        self.manager._send_alert.assert_awaited_once_with(1, "nl-1", "syncookies")

    async def test_alert_is_not_repeated_while_the_mode_stays_on(self):
        self.prepare(server())

        await self.manager._store_status(1, under_attack(), alert=True, known=self.row)
        await self.manager._store_status(1, under_attack(), alert=True, known=self.row)

        self.manager._send_alert.assert_awaited_once()

    async def test_refresh_of_the_sync_mark_during_an_attack_is_not_a_new_alert(self):
        self.prepare(server(
            antiddos_emergency_mode=True, antiddos_source="auto", antiddos_reason="syncookies",
            antiddos_since=datetime.fromtimestamp(ATTACK_STARTED, tz=timezone.utc),
            antiddos_last_sync_at=minutes_ago(6),
        ))

        await self.manager._store_status(1, under_attack(), alert=True, known=self.row)

        self.assertEqual(self.sessions.commits, 1)
        self.manager._send_alert.assert_not_awaited()

    async def test_mode_pinned_by_the_admin_is_not_an_alert(self):
        self.prepare(server())

        await self.manager._store_status(1, under_attack("manual"), alert=True, known=self.row)

        self.manager._send_alert.assert_not_awaited()

    async def test_status_stored_by_an_action_does_not_alert(self):
        self.prepare(server())

        await self.manager._store_status(1, under_attack())

        self.manager._send_alert.assert_not_awaited()

    async def test_stale_snapshot_does_not_alert_about_a_mode_already_known(self):
        snapshot = server()
        self.prepare(server(
            antiddos_emergency_mode=True, antiddos_source="auto", antiddos_reason="old reason",
            antiddos_since=datetime.fromtimestamp(ATTACK_STARTED, tz=timezone.utc),
        ))

        await self.manager._store_status(1, under_attack(), alert=True, known=snapshot)

        self.assertEqual(self.sessions.commits, 1)
        self.manager._send_alert.assert_not_awaited()


class AlertDeliveryTest(unittest.IsolatedAsyncioTestCase):
    def prepare(self, settings: AlertSettings) -> None:
        self.sessions = Sessions({AlertSettings: [settings]})
        self.bot = SimpleNamespace(send_message=mock.AsyncMock(return_value=True))
        for target, name, replacement in (
            (antiddos_manager, "async_session", self.sessions),
            (telegram_bot, "get_telegram_bot_service", lambda: self.bot),
        ):
            patcher = mock.patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manager = AntiDdosManager()

    async def test_alert_survives_a_network_failure(self):
        # retry_later: сообщение, не ушедшее из-за сети, остаётся в очереди повтора
        self.prepare(AlertSettings(telegram_bot_token="token", telegram_chat_id="-100"))

        await self.manager._send_alert(1, "nl-1", "syncookies")

        self.bot.send_message.assert_awaited_once_with("token", "-100", mock.ANY, retry_later=True)
        self.assertIn("nl-1", self.bot.send_message.await_args.args[2])

    async def test_alert_is_kept_in_the_history(self):
        self.prepare(AlertSettings(telegram_bot_token="token", telegram_chat_id="-100"))

        await self.manager._send_alert(1, "nl-1", "syncookies")

        (entry,) = self.sessions.added
        self.assertIsInstance(entry, AlertHistory)
        self.assertEqual((entry.server_id, entry.alert_type, entry.notified), (1, "antiddos_emergency", True))

    async def test_without_a_bot_the_alert_is_only_recorded(self):
        self.prepare(AlertSettings(telegram_bot_token=None, telegram_chat_id=None))

        await self.manager._send_alert(1, "nl-1", "syncookies")

        self.bot.send_message.assert_not_awaited()
        self.assertFalse(self.sessions.added[0].notified)


if __name__ == "__main__":
    unittest.main()
