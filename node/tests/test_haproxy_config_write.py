"""haproxy.cfg меняется целиком или никак: запись во временный файл и rename.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Запись поверх файла при падении агента или сбое питания оставляла конфиг
обрезанным — HAProxy с ним не поднимался после перезагрузки, а читающие в
момент записи видели пустой файл. Откат к снимку писал тем же способом.
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import haproxy_manager  # noqa: E402
from app.services.haproxy_manager import HAProxyManager  # noqa: E402

CONFIG_A = "global\n    no log\n\nfrontend a\n    bind *:443\n"
CONFIG_B = "global\n    no log\n\nfrontend b\n    bind *:8443\n"


class ConfigWriteTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.config_path = self.directory / "haproxy.cfg"
        self.manager = HAProxyManager.__new__(HAProxyManager)
        self.manager.config_path = self.config_path
        self.manager._loaded_config = None

    def files(self) -> list[str]:
        return sorted(path.name for path in self.directory.iterdir())

    def test_new_config_is_complete_on_disk_before_it_replaces_the_old_one(self):
        self.manager._write_config(CONFIG_A)
        seen_at_switch: dict[str, str] = {}
        real_replace = os.replace

        def replace(source, target):
            seen_at_switch["old"] = Path(target).read_text(encoding="utf-8")
            seen_at_switch["new"] = Path(source).read_text(encoding="utf-8")
            real_replace(source, target)

        with mock.patch.object(haproxy_manager.os, "replace", replace):
            self.manager._write_config(CONFIG_B)

        self.assertEqual(seen_at_switch, {"old": CONFIG_A, "new": CONFIG_B})
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), CONFIG_B)
        self.assertEqual(self.files(), ["haproxy.cfg"])

    def test_data_is_flushed_to_disk_before_the_switch(self):
        steps: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(descriptor):
            steps.append("fsync")
            real_fsync(descriptor)

        def replace(source, target):
            steps.append("replace")
            real_replace(source, target)

        with mock.patch.object(haproxy_manager.os, "fsync", fsync), \
                mock.patch.object(haproxy_manager.os, "replace", replace):
            self.manager._write_config(CONFIG_A)

        self.assertEqual(steps, ["fsync", "replace"])

    def test_failed_write_leaves_the_old_config_and_no_leftovers(self):
        self.manager._write_config(CONFIG_A)

        with mock.patch.object(haproxy_manager.os, "fsync", side_effect=OSError("No space left on device")):
            with self.assertRaises(OSError):
                self.manager._write_config(CONFIG_B)

        self.assertEqual(self.config_path.read_text(encoding="utf-8"), CONFIG_A)
        self.assertEqual(self.files(), ["haproxy.cfg"])

    def test_missing_trailing_newline_is_added(self):
        self.manager._write_config("global\n    no log")

        self.assertEqual(self.config_path.read_bytes(), b"global\n    no log\n")

    @unittest.skipIf(sys.platform == "win32", "права файла — POSIX")
    def test_permissions_of_the_config_survive_the_write(self):
        self.manager._write_config(CONFIG_A)
        self.config_path.chmod(0o640)

        self.manager._write_config(CONFIG_B)

        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o640)


class ConfigRollbackTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.config_path = self.directory / "haproxy.cfg"
        self.manager = HAProxyManager.__new__(HAProxyManager)
        self.manager.config_path = self.config_path
        self.manager._loaded_config = None

    def test_rollback_restores_the_snapshot_byte_for_byte(self):
        # Конфиг, написанный руками, бывает без завершающего перевода строки
        original = b"global\r\n    no log"
        self.config_path.write_bytes(original)

        with self.manager._config_rollback() as rollback:
            self.manager._write_config(CONFIG_B)
            rollback()

        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual([path.name for path in self.directory.iterdir()], ["haproxy.cfg"])

    def test_rollback_switches_the_file_atomically(self):
        self.manager._write_config(CONFIG_A)
        replaced: list[str] = []
        real_replace = os.replace

        def replace(source, target):
            replaced.append(Path(source).read_text(encoding="utf-8"))
            real_replace(source, target)

        with self.manager._config_rollback() as rollback:
            self.manager._write_config(CONFIG_B)
            with mock.patch.object(haproxy_manager.os, "replace", replace):
                rollback()

        self.assertEqual(replaced, [CONFIG_A])
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), CONFIG_A)

    def test_rollback_can_be_repeated_after_another_write(self):
        self.manager._write_config(CONFIG_A)

        with self.manager._config_rollback() as rollback:
            self.manager._write_config(CONFIG_B)
            rollback()
            self.manager._write_config(CONFIG_B)
            rollback()

        self.assertEqual(self.config_path.read_text(encoding="utf-8"), CONFIG_A)

    def test_rollback_without_a_config_does_nothing(self):
        with self.manager._config_rollback() as rollback:
            rollback()

        self.assertFalse(self.config_path.exists())


if __name__ == "__main__":
    unittest.main()
