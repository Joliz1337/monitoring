"""Self-check достижимости сторожа: агент ноды любой версии.

Сторож панель берёт с GitHub своего канала, независимо от версии ноды: агент
текущей ноды отвечает на локальном сокете, ноды старой версии — на 127.0.0.1:7500.
Проверка обязана найти живого агента в обоих случаях, иначе под атакой сторож
сочтёт исправную ноду отрезанной и снимет защитную цепочку.
Гоняет настоящий configs/ddos-watchdog.sh (verb self-check) с подставными curl/ss.
Запуск:  python -m unittest discover -s node/tests   (на Windows — под WSL)
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WATCHDOG = Path(__file__).resolve().parents[2] / "configs" / "ddos-watchdog.sh"
SOCKET_PROBE = "-sf --max-time 3 --unix-socket /run/monitoring-node/api.sock http://localhost/health"
TCP_PROBE = "-sf --max-time 3 http://127.0.0.1:7500/health"
CURL_REFUSED = 7

FAKE_SS = "#!/bin/sh\nexit 0\n"
FAKE_CURL = """#!/bin/sh
printf '%s\\n' "$*" >> "$CURL_LOG"
case " $* " in
    *" --unix-socket "*) exit "$FAKE_SOCKET_RC" ;;
esac
exit "$FAKE_TCP_RC"
"""


@unittest.skipIf(sys.platform == "win32", "bash-скрипт хоста: на Windows гоняется под WSL")
class WatchdogSelfCheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stub_dir = Path(tmp.name)
        for name, body in (("ss", FAKE_SS), ("curl", FAKE_CURL)):
            stub = self.stub_dir / name
            stub.write_text(body)
            stub.chmod(0o755)
        self.curl_log = self.stub_dir / "curl.log"

    def self_check(self, socket_rc: int, tcp_rc: int) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{self.stub_dir}:{os.environ['PATH']}",
            "CURL_LOG": str(self.curl_log),
            "FAKE_SOCKET_RC": str(socket_rc),
            "FAKE_TCP_RC": str(tcp_rc),
        }
        return subprocess.run(["bash", str(WATCHDOG), "self-check"], capture_output=True, env=env, timeout=60)

    def curl_calls(self) -> list[str]:
        if not self.curl_log.exists():
            return []
        return self.curl_log.read_text().splitlines()

    def test_socket_agent_is_enough(self):
        result = self.self_check(socket_rc=0, tcp_rc=CURL_REFUSED)
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        self.assertEqual(self.curl_calls(), [SOCKET_PROBE])

    def test_falls_back_to_legacy_port(self):
        result = self.self_check(socket_rc=CURL_REFUSED, tcp_rc=0)
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        self.assertEqual(self.curl_calls(), [SOCKET_PROBE, TCP_PROBE])

    def test_unreachable_without_agent_and_ssh(self):
        result = self.self_check(socket_rc=CURL_REFUSED, tcp_rc=CURL_REFUSED)
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        self.assertEqual(self.curl_calls(), [SOCKET_PROBE, TCP_PROBE])


if __name__ == "__main__":
    unittest.main()
