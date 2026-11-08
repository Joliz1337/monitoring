"""Ожидание агента после доставки образа: нода любой версии.

Доставка поднимает ноду со своим docker-compose.yml с диска: у ноды текущей
версии агент слушает локальный сокет, у старой — 127.0.0.1:7500. Проба обязана
дождаться агента в обоих случаях — сначала сокет, затем старый порт.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import node_image_delivery as module  # noqa: E402

SOCKET_PROBE = "curl -sf --unix-socket /run/monitoring-node/api.sock http://localhost/health"
TCP_PROBE = "curl -sf http://127.0.0.1:7500/health"
CURL_REFUSED = 7
FAKE_CURL = """#!/bin/sh
printf '%s\\n' "$*" >> "$CURL_LOG"
case " $* " in
    *" --unix-socket "*) exit "$FAKE_SOCKET_RC" ;;
esac
exit "$FAKE_TCP_RC"
"""


class StartCommandTest(unittest.TestCase):
    def test_probes_socket_before_legacy_port(self):
        command = module._start_command("dev")
        self.assertIn(SOCKET_PROBE, command)
        self.assertIn(TCP_PROBE, command)
        self.assertLess(command.index(SOCKET_PROBE), command.index(TCP_PROBE))

    def test_waits_for_agent_after_compose_up(self):
        command = module._start_command("dev")
        self.assertIn("MON_IMAGE_TAG=dev", command)
        self.assertLess(command.index("docker compose up -d"), command.index(module.AGENT_HEALTH_PROBE))


@unittest.skipIf(sys.platform == "win32", "проба исполняется в sh ноды, на Windows bash уходит в WSL-лаунчер")
class AgentHealthProbeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stub_dir = Path(tmp.name)
        stub = self.stub_dir / "curl"
        stub.write_text(FAKE_CURL)
        stub.chmod(0o755)
        self.curl_log = self.stub_dir / "curl.log"

    def probe(self, socket_rc: int, tcp_rc: int) -> int:
        env = {
            **os.environ,
            "PATH": f"{self.stub_dir}:{os.environ['PATH']}",
            "CURL_LOG": str(self.curl_log),
            "FAKE_SOCKET_RC": str(socket_rc),
            "FAKE_TCP_RC": str(tcp_rc),
        }
        return subprocess.run(["sh", "-c", module.AGENT_HEALTH_PROBE], env=env, timeout=30).returncode

    def calls(self) -> list[str]:
        return self.curl_log.read_text().splitlines() if self.curl_log.exists() else []

    def test_current_node_answers_on_socket(self):
        self.assertEqual(self.probe(socket_rc=0, tcp_rc=CURL_REFUSED), 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("--unix-socket", self.calls()[0])

    def test_old_node_answers_on_legacy_port(self):
        self.assertEqual(self.probe(socket_rc=CURL_REFUSED, tcp_rc=0), 0)
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("127.0.0.1:7500", self.calls()[1])

    def test_no_agent_keeps_waiting(self):
        self.assertNotEqual(self.probe(socket_rc=CURL_REFUSED, tcp_rc=CURL_REFUSED), 0)


if __name__ == "__main__":
    unittest.main()
