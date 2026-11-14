"""Тесты запуска и остановки панели (lifespan в app/main.py).

Голый unittest, без PostgreSQL и без сети: все сервисы подменены, проверяется
только то, что lifespan решает сам, — пускать ли панель и в каком порядке её гасить.

HTTP-клиенты закрывались первыми, а сборщик метрик останавливался последним: его
опросы успевали упасть на закрытых клиентах и записать нодам ложную ошибку, а
хвост трафика уходил в базу в самом конце остановки, под её таймаут.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import logging
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import InsecureSettingsError, Settings  # noqa: E402

# app.main при импорте настраивает корневой логгер процесса (basicConfig) —
# остальным тестам прогона он должен достаться прежним
_root_logger = logging.getLogger()
_root_handlers, _root_level = _root_logger.handlers[:], _root_logger.level
from app import main  # noqa: E402
_root_logger.handlers[:] = _root_handlers
_root_logger.setLevel(_root_level)

PASSWORD = "a-password-set-by-the-installer"
JWT_SECRET = "s" * 64
DEFAULT_PASSWORD = "changeme"
DEFAULT_JWT_SECRET = "jwt-secret-key-change-me"
LOADERS = (
    "init_db", "load_or_create_keygen", "init_http_clients", "close_http_clients",
    "cleanup_expired_bans", "cancel_all_jobs",
    "load_branch_from_db", "load_xray_test_versions", "load_loss_exclusions",
)


class LifespanCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []
        self.use_settings(PASSWORD, JWT_SECRET)
        services = [name for name in vars(main) if name.startswith(("start_", "stop_"))]
        for name in (*services, *LOADERS):
            self.record(main, name)
        self.record(main.node_rollout, "start", sync=True)
        self.record(main.node_rollout, "stop")
        blocklist_manager = SimpleNamespace(
            start=mock.AsyncMock(side_effect=lambda: self.calls.append("start_blocklist_manager")),
            stop=mock.AsyncMock(side_effect=lambda: self.calls.append("stop_blocklist_manager")),
        )
        for target, replacement in (
            ("app.main.get_blocklist_manager", mock.Mock(return_value=blocklist_manager)),
            ("app.main.async_session", mock.MagicMock()),
            ("app.crypto.ensure_key", mock.Mock()),
            ("app.services.backup_scheduler.start_scheduler", mock.Mock()),
        ):
            patcher = mock.patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def record(self, owner: object, name: str, *, sync: bool = False) -> None:
        label = name if owner is main else f"{name}_{owner.__name__.rsplit('.', 1)[-1]}"
        note = mock.Mock(side_effect=lambda *args, **kwargs: self.calls.append(label))
        patcher = mock.patch.object(owner, name, note if sync else mock.AsyncMock(side_effect=note))
        patcher.start()
        self.addCleanup(patcher.stop)

    def use_settings(self, panel_password: str, jwt_secret: str) -> None:
        patcher = mock.patch.object(
            main, "settings", Settings(panel_password=panel_password, jwt_secret=jwt_secret),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def run_panel(self) -> tuple[list[str], list[str]]:
        """Поднять и погасить панель: (вызовы при запуске, вызовы при остановке)."""
        async with main.lifespan(SimpleNamespace(state=SimpleNamespace())):
            started = self.calls[:]
            self.calls.clear()
        return started, self.calls


class ShutdownOrderTest(LifespanCase):
    async def test_collector_is_stopped_while_http_clients_are_still_open(self):
        _, stopped = await self.run_panel()
        self.assertLess(stopped.index("stop_collector"), stopped.index("close_http_clients"))

    async def test_http_clients_are_closed_after_every_service(self):
        _, stopped = await self.run_panel()
        self.assertEqual(stopped[-1], "close_http_clients")
        self.assertEqual(stopped.count("close_http_clients"), 1)

    async def test_pending_ip_transactions_are_cancelled_first_and_proxy_cores_killed_before_other_services(self):
        _, stopped = await self.run_panel()
        self.assertEqual(stopped[:2], ["cancel_all_jobs", "stop_xray_test_service"])

    async def test_collector_is_stopped_ahead_of_the_other_services(self):
        # Его остановка дописывает в базу хвост трафика: в конце очереди её оборвал бы таймаут
        _, stopped = await self.run_panel()
        services = [call for call in stopped if call.startswith("stop_")]
        self.assertEqual(services.index("stop_collector"), 1)

    async def test_every_started_service_is_stopped(self):
        started, stopped = await self.run_panel()
        self.assertEqual(
            sorted(call.removeprefix("start_") for call in started if call.startswith("start_")),
            sorted(call.removeprefix("stop_") for call in stopped if call.startswith("stop_")),
        )


class UnsetSecretsTest(LifespanCase):
    async def refused_start(self) -> str:
        with self.assertLogs(main.logger, level="CRITICAL") as captured, \
                self.assertRaises(InsecureSettingsError):
            await self.run_panel()
        self.assertEqual(self.calls, [], "the panel began to start before the check")
        return "\n".join(record.getMessage() for record in captured.records)

    async def test_panel_does_not_start_with_the_built_in_password(self):
        self.use_settings(DEFAULT_PASSWORD, JWT_SECRET)
        log = await self.refused_start()
        self.assertIn("PANEL_PASSWORD", log)
        self.assertNotIn("JWT_SECRET", log)

    async def test_panel_does_not_start_with_an_empty_password(self):
        for empty in ("", "   "):
            self.use_settings(empty, JWT_SECRET)
            self.assertIn("PANEL_PASSWORD", await self.refused_start())

    async def test_panel_does_not_start_with_the_built_in_jwt_secret(self):
        self.use_settings(PASSWORD, DEFAULT_JWT_SECRET)
        log = await self.refused_start()
        self.assertIn("JWT_SECRET", log)
        self.assertNotIn("PANEL_PASSWORD", log)

    async def test_panel_does_not_start_with_an_empty_jwt_secret(self):
        self.use_settings(PASSWORD, "")
        self.assertIn("JWT_SECRET", await self.refused_start())

    async def test_every_unset_variable_is_named(self):
        self.use_settings(DEFAULT_PASSWORD, DEFAULT_JWT_SECRET)
        log = await self.refused_start()
        self.assertIn("PANEL_PASSWORD", log)
        self.assertIn("JWT_SECRET", log)

    async def test_secret_values_do_not_get_into_the_log(self):
        self.use_settings(DEFAULT_PASSWORD, "short-but-real-secret")
        self.assertNotIn("short-but-real-secret", await self.refused_start())

    async def test_short_jwt_secret_only_warns(self):
        self.use_settings(PASSWORD, "s" * 31)
        with self.assertLogs(main.logger, level="WARNING") as captured:
            started, _ = await self.run_panel()
        self.assertIn("JWT_SECRET", captured.records[0].getMessage())
        self.assertIn("start_collector", started)

    async def test_installer_generated_secrets_start_the_panel_silently(self):
        with self.assertNoLogs(main.logger, level="WARNING"):
            started, _ = await self.run_panel()
        self.assertIn("start_collector", started)

    def test_defaults_of_the_config_stay_importable(self):
        # Тесты и локальный запуск импортируют модули со значениями по умолчанию
        defaults = Settings(panel_password=DEFAULT_PASSWORD, jwt_secret=DEFAULT_JWT_SECRET)
        self.assertEqual(defaults.unset_secrets(), ["PANEL_PASSWORD", "JWT_SECRET"])


class LibraryLogsTest(unittest.TestCase):
    def test_libraries_that_log_secrets_at_info_are_silenced(self):
        # httpx пишет URL каждого запроса (в нём токен Telegram-бота),
        # asyncssh — команду установки ноды вместе с NODE_SECRET
        for library in ("httpx", "httpcore", "asyncssh"):
            logger = logging.getLogger(library)
            self.assertFalse(logger.isEnabledFor(logging.INFO), library)
            self.assertTrue(logger.isEnabledFor(logging.WARNING), library)


if __name__ == "__main__":
    unittest.main()
