"""Tests for panel-host time sync: container script wrapping and result parsing.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Скрипт хоста лежит в двух копиях — у ноды и у панели, — потому что образы
собираются из разных контекстов. Разошедшиеся копии означали бы, что панель и
ноды настраивают время по-разному, поэтому тест сверяет их побайтно.
"""

import base64
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.time_sync import (  # noqa: E402
    HOST_TIME_SYNC_SCRIPT,
    build_container_script,
    parse_key_values,
)

PANEL_SCRIPT = Path(__file__).resolve().parents[1] / "app" / "services" / "host_time_sync.sh"
NODE_SCRIPT = Path(__file__).resolve().parents[3] / "node" / "app" / "services" / "host_time_sync.sh"


class ScriptCopiesTests(unittest.TestCase):
    def test_panel_and_node_copies_are_identical(self):
        if not NODE_SCRIPT.exists():
            self.skipTest("node tree is not available next to the panel")
        self.assertEqual(
            PANEL_SCRIPT.read_bytes(), NODE_SCRIPT.read_bytes(),
            "host_time_sync.sh differs between panel and node — copy the edited file to the other side",
        )


class ContainerScriptTests(unittest.TestCase):
    def test_host_command_is_passed_through_base64_with_quoted_timezone(self):
        script = build_container_script("Europe/Moscow; id")
        encoded = script.split("$(echo ", 1)[1].split(" | base64 -d)", 1)[0]
        host_command = base64.b64decode(encoded).decode("utf-8")
        self.assertEqual(host_command, f"TZ_NAME='Europe/Moscow; id'\n{HOST_TIME_SYNC_SCRIPT}")
        self.assertIn("nsenter -t 1 -m -u -n -i -p -- bash -c", script)


class ParseTests(unittest.TestCase):
    def test_key_values_from_script_and_timedatectl(self):
        values = parse_key_values("NTPService=chrony\nNTPInstalled=yes\nnoise\nNTPSynchronized=yes\nTimezone=UTC")
        self.assertEqual(values["NTPService"], "chrony")
        self.assertEqual(values["NTPInstalled"], "yes")
        self.assertEqual(values["NTPSynchronized"], "yes")
        self.assertEqual(values["Timezone"], "UTC")
        self.assertNotIn("noise", values)


if __name__ == "__main__":
    unittest.main()
