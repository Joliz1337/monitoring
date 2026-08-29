"""Команда установки Remnawave через агента ноды и разбор SSE исполнителя.

Отдельно закреплено: агент логирует первые 100 символов команды, поэтому
сертификат не должен попадать в этот префикс — curl-часть обязана идти первой.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import update_channel  # noqa: E402
from app.services.deploy_service import build_remnawave_install_command  # noqa: E402
from app.services.remnawave_node_install import parse_sse_event  # noqa: E402

CERT = "SSL_CERT=eyJub2RlQ2VydFBlbSI6IJERTIFIRSTLINE\neySECONDLINEOFCERT"


class RemnawaveInstallCommandTests(unittest.TestCase):
    def tearDown(self):
        update_channel.set_current_branch(update_channel.STABLE_BRANCH)

    def test_installs_only_remnawave(self):
        command = build_remnawave_install_command(CERT)
        self.assertIn("MON_INSTALL_REMNAWAVE=1", command)
        self.assertNotIn("MON_INSTALL_NODE", command)
        self.assertNotIn("NODE_SECRET", command)
        self.assertTrue(command.endswith("--unattended"))

    def test_cert_newlines_are_escaped(self):
        command = build_remnawave_install_command("line1\r\nline2\nline3")
        self.assertIn("line1\\nline2\\nline3", command)
        self.assertNotIn("line1\r\nline2", command)

    def test_cert_never_reaches_node_log_prefix(self):
        # host_executor логирует command[:100] — секрет должен быть дальше
        command = build_remnawave_install_command(CERT)
        self.assertNotIn(CERT[:20], command[:100])
        self.assertTrue(command[:100].startswith("curl -fsSL"))

    def test_dev_channel_is_propagated(self):
        update_channel.set_current_branch(update_channel.DEV_BRANCH)
        command = build_remnawave_install_command(CERT)
        self.assertIn("MON_BRANCH=dev", command)
        self.assertIn("/dev/install.sh", command)

    def test_stable_channel_has_no_branch_env(self):
        command = build_remnawave_install_command(CERT)
        self.assertNotIn("MON_BRANCH", command)
        self.assertIn("/main/install.sh", command)


class ParseSseEventTests(unittest.TestCase):
    def test_stdout_and_stderr_become_log(self):
        self.assertEqual(
            parse_sse_event("stdout", '{"line": "Installing..."}'),
            {"type": "log", "line": "Installing..."},
        )
        self.assertEqual(
            parse_sse_event("stderr", '{"line": "warn"}'),
            {"type": "log", "line": "warn"},
        )

    def test_done_carries_exit_code(self):
        event = parse_sse_event("done", '{"exit_code": 2, "success": false}')
        self.assertEqual(event, {"type": "_exit", "code": 2, "success": False})

    def test_error_event(self):
        event = parse_sse_event("error", '{"message": "boom"}')
        self.assertEqual(event, {"type": "error", "message": "boom"})

    def test_unknown_event_and_broken_json(self):
        self.assertIsNone(parse_sse_event("ping", "{}"))
        self.assertEqual(parse_sse_event("stdout", "not-json"), {"type": "log", "line": ""})


if __name__ == "__main__":
    unittest.main()
