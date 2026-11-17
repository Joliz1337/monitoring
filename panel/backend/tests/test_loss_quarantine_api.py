"""Tests for the auto-disable routes of the loss page and the default notifier.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Роутер — граница с интерфейсом: строгая проверка ввода живёт здесь. Старый
интерфейс, который ещё не знает блока настроек автоотключения, не должен
сбросить его своим сохранением.
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.routers import loss as loss_router  # noqa: E402
from app.services import backend_quarantine_runner as runner_module  # noqa: E402
from app.services.backend_quarantine import Event, EventKind, Mode, ReasonKind  # noqa: E402
from app.services.backend_quarantine_settings import QuarantineSettings, parse_settings  # noqa: E402
from app.services.loss_exclusions import LossExclusions  # noqa: E402
from app.services.loss_registry import LossRegistry  # noqa: E402

VALID = {
    "mode": "enforce", "loss_threshold": 60.0, "sustained_seconds": 240, "downs_threshold": 4,
    "calm_seconds": 900, "chronic_strikes": 5, "max_share_pct": 40, "drop_sessions": False, "notify": True,
    "notify_scope": "dead", "notify_dead_share_pct": 80,
    "client_probe": True,
    "exempt_server_ids": [3], "exempt_targets": ["1.2.3.4", "5.6.7.8:8443"],
}
NO_EXCLUSIONS = LossExclusions(server_ids=frozenset(), targets=())


class SettingsModelTests(unittest.TestCase):
    def test_valid_block(self):
        block = loss_router.QuarantineSettingsIn(**VALID)
        self.assertEqual((block.mode, block.loss_threshold, block.max_share_pct), (Mode.ENFORCE, 60.0, 40))

    def test_bounds_are_enforced_at_the_border(self):
        for field, value in (("loss_threshold", 49.9), ("loss_threshold", 90.1), ("sustained_seconds", 59),
                             ("sustained_seconds", 1801), ("downs_threshold", 1), ("downs_threshold", 21),
                             ("calm_seconds", 119), ("calm_seconds", 3601), ("chronic_strikes", 1),
                             ("chronic_strikes", 21), ("max_share_pct", 9), ("max_share_pct", 91),
                             ("mode", "wipe")):
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                loss_router.QuarantineSettingsIn(**{**VALID, field: value})

    def test_block_of_an_older_tab_leaves_the_client_probe_off(self):
        older = {key: value for key, value in VALID.items() if key != "client_probe"}
        self.assertIs(loss_router.QuarantineSettingsIn(**older).client_probe, False)
        self.assertIs(loss_router.QuarantineSettingsIn(**VALID).client_probe, True)

    def test_share_out_of_bounds_is_rejected(self):
        for value in (9, 101):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                loss_router.QuarantineSettingsIn(**{**VALID, "notify_dead_share_pct": value})

    def test_old_interface_body_has_no_block(self):
        update = loss_router.LossSettingsUpdate(excluded_server_ids=[1], excluded_targets=[])
        self.assertIsNone(update.quarantine)


class SettingsRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stored = parse_settings(json.dumps({"mode": "observe"}))
        self.runner = mock.Mock()
        self.save_exclusions = mock.AsyncMock(return_value=NO_EXCLUSIONS)
        self.save_quarantine = mock.AsyncMock()
        for name, replacement in (
            ("load_loss_exclusions", mock.AsyncMock(return_value=NO_EXCLUSIONS)),
            ("save_loss_exclusions", self.save_exclusions),
            ("load_quarantine_settings", mock.AsyncMock(return_value=self.stored)),
            ("save_quarantine_settings", self.save_quarantine),
            ("get_quarantine_runner", mock.Mock(return_value=self.runner)),
        ):
            patcher = mock.patch.object(loss_router, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.db = object()

    def update(self, **fields) -> loss_router.LossSettingsUpdate:
        return loss_router.LossSettingsUpdate(excluded_server_ids=[], excluded_targets=[], **fields)

    async def test_settings_carry_the_quarantine_block(self):
        response = await loss_router.get_loss_settings(self.db)
        self.assertEqual(response["quarantine"]["mode"], "observe")
        self.assertEqual((response["excluded_server_ids"], response["excluded_targets"]), ([], []))

    async def test_saving_without_the_block_keeps_auto_disable_as_is(self):
        response = await loss_router.update_loss_settings(self.update(), self.db)
        self.save_quarantine.assert_not_awaited()
        self.runner.set_settings.assert_not_called()
        self.assertEqual(response["quarantine"]["mode"], "observe")

    async def test_saving_the_block_stores_it_and_tells_the_runner(self):
        response = await loss_router.update_loss_settings(self.update(quarantine=VALID), self.db)
        saved = self.save_quarantine.await_args.args[1]
        self.assertEqual(saved.to_dict(), VALID)
        self.runner.set_settings.assert_called_once_with(saved)
        self.assertEqual(response["quarantine"], VALID)

    async def test_old_tab_without_new_fields_keeps_the_stored_scope(self):
        # Вкладка, открытая до обновления панели, режима уведомлений не знает и своим сохранением его не сбрасывает
        self.stored = parse_settings(json.dumps({"mode": "observe", "notify_scope": "dead", "notify_dead_share_pct": 80}))
        loss_router.load_quarantine_settings.return_value = self.stored
        older = {key: value for key, value in VALID.items() if key not in ("notify_scope", "notify_dead_share_pct")}

        response = await loss_router.update_loss_settings(self.update(quarantine=older), self.db)

        self.assertEqual((response["quarantine"]["notify_scope"], response["quarantine"]["notify_dead_share_pct"]),
                         ("dead", 80))

    async def test_unreadable_exempt_address_is_a_400_and_saves_nothing(self):
        block = {**VALID, "exempt_targets": ["1.2.3.4", "not-an-address"]}
        with self.assertRaises(HTTPException) as caught:
            await loss_router.update_loss_settings(self.update(quarantine=block), self.db)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertTrue(caught.exception.detail.startswith("not-an-address: "))
        self.save_quarantine.assert_not_awaited()
        self.save_exclusions.assert_not_awaited()
        self.runner.set_settings.assert_not_called()


class QuarantineRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runner = mock.Mock()
        self.registry = LossRegistry()
        for name, replacement in (
            ("get_quarantine_runner", mock.Mock(return_value=self.runner)),
            ("get_loss_registry", mock.Mock(return_value=self.registry)),
        ):
            patcher = mock.patch.object(loss_router, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def observe(self, server_id: int, address: str, *targets: str) -> None:
        self.registry.update(server_id, f"relay{server_id}", {
            "loss_probe": [{"ip": ip, "port": 443, "loss_pct": 30.0, "rtt_ms": 40.0, "samples": 60} for ip in targets],
            "network": {"interfaces": [{"addresses": [{"type": "ipv4", "address": address}]}]},
        })

    async def test_view_is_built_with_address_owners(self):
        self.observe(1, "10.0.0.5")
        self.runner.view.return_value = {"mode": "enforce", "items": [], "chronic": [], "paused_relays": []}
        response = await loss_router.get_quarantine()
        self.runner.view.assert_called_once_with({"10.0.0.5": "relay1"})
        self.assertEqual(response["mode"], "enforce")

    async def test_release_passes_the_pair(self):
        self.runner.release = mock.AsyncMock(return_value=True)
        request = loss_router.QuarantineReleaseRequest(server_id=7, target=" 10.0.0.5:443 ")
        self.assertEqual(await loss_router.release_quarantine(request), {"released": True})
        self.runner.release.assert_awaited_once_with(7, "10.0.0.5:443")

    async def test_release_of_an_unknown_pair_is_404(self):
        self.runner.release = mock.AsyncMock(return_value=False)
        with self.assertRaises(HTTPException) as caught:
            await loss_router.release_quarantine(loss_router.QuarantineReleaseRequest(server_id=7, target="1.1.1.1:1"))
        self.assertEqual(caught.exception.status_code, 404)

    async def test_overview_marks_relays_where_the_address_is_disabled(self):
        self.observe(1, "10.9.0.1", "10.0.0.5", "10.0.0.6")
        self.runner.disabled_relays.return_value = {"10.0.0.5:443": [1, 4]}
        alerter = SimpleNamespace(loss_episodes=lambda: {})
        with mock.patch.object(loss_router, "get_server_alerter", return_value=alerter):
            response = await loss_router.get_overview()
        marks = {row["target"]: row["quarantined"] for row in response["targets"]}
        self.assertEqual(marks, {"10.0.0.5:443": [1, 4], "10.0.0.6:443": []})


class CheckRouteTests(unittest.IsolatedAsyncioTestCase):
    """«Проверить адрес» делает рукопожатие «как клиент», только когда оно включено в настройках."""

    async def test_check_follows_the_client_probe_setting(self):
        servers = [SimpleNamespace(id=1, name="msk-1")]
        found = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: servers))
        for wanted in (True, False):
            db = mock.Mock(execute=mock.AsyncMock(return_value=found), commit=mock.AsyncMock())
            runner = mock.Mock(client_probe_wanted=mock.Mock(return_value=wanted))
            fan_out = mock.AsyncMock(return_value=[])
            with self.subTest(client_probe=wanted), \
                    mock.patch.object(loss_router, "get_quarantine_runner", return_value=runner), \
                    mock.patch.object(loss_router, "check_from_servers", fan_out):
                response = await loss_router.check_target(
                    loss_router.LossCheckRequest(target="62.50.146.225:443", server_ids=[1]), db)
                self.assertEqual(response, {"ip": "62.50.146.225", "port": 443, "results": []})
                fan_out.assert_awaited_once_with(servers, "62.50.146.225", 443, wanted)


class FakeDb:
    def __init__(self, alert_settings):
        self.alert_settings = alert_settings
        self.added: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.alert_settings)

    def add_all(self, rows) -> None:
        self.added += list(rows)

    async def commit(self) -> None:
        self.committed = True


EVENT = Event(EventKind.DISABLED, 1, "msk-1", "10.0.0.5:443", ReasonKind.LOSS, 25.0, 0, term_sec=600, sessions=3, strikes=1)


DEAD = Event(EventKind.DEAD, 1, "msk-1", "10.0.0.5:443", failing=5, total=6, reasons=(ReasonKind.LOSS,), owner="nl-3")
DEAD_ONLY = parse_settings(json.dumps({"notify_scope": "dead"}))


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    def sent_text(self) -> str:
        self.telegram.send_message.assert_awaited_once()
        return self.telegram.send_message.await_args.args[2]

    def rows(self) -> list[tuple[str, bool]]:
        return [(row.alert_type, row.notified) for row in self.db.added]

    async def test_dead_scope_sends_the_address_state_and_keeps_the_whole_history(self):
        await self.notify(self.alerts(), DEAD_ONLY, events=(EVENT, DEAD))
        text = self.sent_text()
        self.assertIn("Адрес не работает", text)
        self.assertNotIn("Отключены", text)
        self.assertEqual(self.rows(), [("backend_disabled", False), ("backend_dead", True)])

    async def test_all_events_scope_keeps_its_digest_and_does_not_claim_the_dead_row(self):
        await self.notify(self.alerts(), events=(EVENT, DEAD))
        text = self.sent_text()
        self.assertIn("Отключены", text)
        self.assertNotIn("Адрес не работает", text)
        self.assertEqual(self.rows(), [("backend_disabled", True), ("backend_dead", False)])

    async def test_dead_scope_without_address_events_sends_nothing(self):
        await self.notify(self.alerts(), DEAD_ONLY)
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual(self.rows(), [("backend_disabled", False)])

    async def test_failed_delivery_of_the_dead_digest_is_recorded(self):
        await self.notify(self.alerts(), DEAD_ONLY, sent=False, events=(DEAD,))
        self.assertEqual(self.rows(), [("backend_dead", False)])

    async def notify(self, alert_settings, settings: QuarantineSettings = QuarantineSettings(), sent: bool = True,
                     events: tuple[Event, ...] = (EVENT,)):
        self.db = FakeDb(alert_settings)
        self.telegram = mock.Mock(send_message=mock.AsyncMock(return_value=sent))
        registry = LossRegistry()
        with mock.patch.object(runner_module, "async_session", lambda: self.db),              mock.patch.object(runner_module, "get_telegram_bot_service", return_value=self.telegram),              mock.patch.object(runner_module, "get_loss_registry", return_value=registry):
            await runner_module.notify_events(list(events), settings)

    @staticmethod
    def alerts(**fields):
        base = dict(enabled=True, telegram_bot_token="token", telegram_chat_id="chat", language="ru")
        return SimpleNamespace(**{**base, **fields})

    async def test_event_goes_to_telegram_and_history(self):
        await self.notify(self.alerts())
        self.telegram.send_message.assert_awaited_once()
        token, chat, text = self.telegram.send_message.await_args.args
        self.assertEqual((token, chat), ("token", "chat"))
        self.assertIn("10.0.0.5:443", text)
        [row] = self.db.added
        self.assertEqual((row.server_id, row.server_name, row.alert_type, row.severity, row.notified),
                         (1, "msk-1", "backend_disabled", "warning", True))
        self.assertEqual(json.loads(row.details)["target"], "10.0.0.5:443")
        self.assertTrue(self.db.committed)

    async def test_notify_toggle_silences_telegram_but_not_history(self):
        await self.notify(self.alerts(), parse_settings(json.dumps({"notify": False})))
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual([row.notified for row in self.db.added], [False])

    async def test_alerts_switched_off_silence_telegram(self):
        await self.notify(self.alerts(enabled=False))
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual(len(self.db.added), 1)

    async def test_no_bot_configured(self):
        for alert_settings in (self.alerts(telegram_bot_token=None), self.alerts(telegram_chat_id=""), None):
            with self.subTest(alerts=alert_settings):
                await self.notify(alert_settings)
                self.telegram.send_message.assert_not_awaited()
                self.assertEqual(len(self.db.added), 1)

    async def test_failed_delivery_is_recorded(self):
        await self.notify(self.alerts(), sent=False)
        self.assertEqual([row.notified for row in self.db.added], [False])

    async def test_language_follows_alert_settings(self):
        await self.notify(self.alerts(language="EN"))
        self.assertIn("Disabled", self.telegram.send_message.await_args.args[2])


if __name__ == "__main__":
    unittest.main()
