"""Tests for skipping the HAProxy reload when the panel sends the config that is already loaded.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Панель шлёт конфиг при любом сохранении правила, даже без правок. Каждый reload
поднимает новый воркер: сбрасываются веса и счётчики, а старый воркер через
hard-stop-after (1 ч) обрывает долгие VPN-сессии. Пропуск допустим, только
когда агент сам загрузил этот конфиг и HAProxy с ним работает.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.haproxy_manager import HAProxyManager  # noqa: E402

CONFIG_A = "global\n    no log\n\nfrontend a\n    bind *:443\n"
CONFIG_B = "global\n    no log\n\nfrontend b\n    bind *:8443\n"
RELOADED = (True, "HAProxy reloaded successfully")


class ApplyConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config_path = Path(tmp.name) / "haproxy.cfg"
        self.running = True
        self.reloads = 0
        self.reload_result = RELOADED
        self.manager = self.make_manager()

    def make_manager(self) -> HAProxyManager:
        manager = HAProxyManager.__new__(HAProxyManager)
        manager.config_path = self.config_path
        manager._loaded_config = None
        manager._prepare_config = lambda content: content
        manager._sync_silent_drop_guard = lambda content: None
        manager.check_config = lambda: (True, "")
        manager.is_running = lambda: self.running

        def reload(auto_start: bool = True) -> tuple[bool, str]:
            self.reloads += 1
            return self.reload_result

        manager.reload = reload
        return manager

    def test_same_config_is_not_reloaded_twice(self):
        self.assertEqual(self.manager.apply_config(CONFIG_A), (True, "Config applied and reloaded", True))
        success, message, reloaded = self.manager.apply_config(CONFIG_A)
        self.assertEqual((success, reloaded, self.reloads), (True, False, 1))
        self.assertIn("unchanged", message)

    def test_changed_config_reloads(self):
        self.manager.apply_config(CONFIG_A)
        self.manager.apply_config(CONFIG_B)
        self.assertEqual(self.reloads, 2)
        self.assertEqual(self.config_path.read_text(), CONFIG_B)

    def test_file_edited_by_hand_reloads(self):
        self.manager.apply_config(CONFIG_A)
        self.config_path.write_text(CONFIG_B)
        self.manager.apply_config(CONFIG_A)
        self.assertEqual(self.reloads, 2)
        self.assertEqual(self.config_path.read_text(), CONFIG_A)

    def test_stopped_haproxy_goes_through_reload(self):
        self.manager.apply_config(CONFIG_A)
        self.running = False
        self.manager.apply_config(CONFIG_A)
        self.assertEqual(self.reloads, 2)

    def test_after_agent_restart_loaded_config_is_unknown(self):
        self.config_path.write_text(CONFIG_A)
        self.manager.apply_config(CONFIG_A)
        self.assertEqual(self.reloads, 1)

    def test_ensure_started_goes_through_reload(self):
        self.manager.apply_config(CONFIG_A)
        self.manager.apply_config(CONFIG_A, ensure_started=True)
        self.assertEqual(self.reloads, 2)

    def test_failed_reload_is_not_remembered(self):
        self.reload_result = (False, "Reload failed: boom")
        self.assertFalse(self.manager.apply_config(CONFIG_A)[0])
        self.reload_result = RELOADED
        self.manager.apply_config(CONFIG_A)
        self.assertEqual(self.reloads, 2)

    def test_config_saved_without_running_haproxy_is_not_remembered(self):
        self.reload_result = (True, "Config saved (HAProxy not running)")
        self.assertEqual(self.manager.apply_config(CONFIG_A)[2], False)
        self.reload_result = RELOADED
        self.manager.apply_config(CONFIG_A)
        self.assertEqual(self.reloads, 2)

    def test_config_written_by_another_operation_is_not_trusted(self):
        # Локальная правка правил на ноде пишет конфиг и делает свой reload
        self.manager.apply_config(CONFIG_A)
        self.manager._write_config(CONFIG_A)
        self.manager.apply_config(CONFIG_A)
        self.assertEqual(self.reloads, 2)


if __name__ == "__main__":
    unittest.main()
