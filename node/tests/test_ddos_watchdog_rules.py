"""Аварийная цепочка ANTIDDOS: порт API ноды не пускается безусловно.

Гоняет настоящий configs/ddos-watchdog.sh (dry-run, audit-rules, version) под bash.
Запуск:  python -m unittest discover -s node/tests   (на Windows — под WSL)
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WATCHDOG = Path(__file__).resolve().parents[2] / "configs" / "ddos-watchdog.sh"
LOOPBACK_NET = "127.0.0.0/8"
NODE_API_PORT = 9100
AGENT_PORT = 7500
FAKE_SS = "#!/bin/sh\nexit 0\n"

ACCEPT_DPORT = re.compile(r"--dport\s+(\d+)\b.*-j ACCEPT")


@unittest.skipIf(sys.platform == "win32", "bash-скрипт хоста: на Windows гоняется под WSL")
class WatchdogNeverDropTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub_dir = tempfile.TemporaryDirectory()
        fake_ss = Path(cls.stub_dir.name) / "ss"
        fake_ss.write_text(FAKE_SS)
        fake_ss.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        cls.stub_dir.cleanup()

    def run_watchdog(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.stub_dir.name}:{os.environ['PATH']}"}
        return subprocess.run(
            ["bash", str(WATCHDOG), *args],
            input=stdin.encode(), capture_output=True, env=env, timeout=60,
        )

    def accept_lines(self) -> list[str]:
        result = self.run_watchdog("dry-run")
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        return [l for l in result.stdout.decode().splitlines() if ACCEPT_DPORT.search(l)]

    def test_node_api_port_has_no_accept_rule(self):
        for line in self.accept_lines():
            self.assertNotIn(f"--dport {NODE_API_PORT} ", line + " ", line)

    def test_agent_port_accept_only_from_loopback(self):
        agent_rules = [l for l in self.accept_lines() if f"--dport {AGENT_PORT} " in l + " "]
        self.assertEqual(len(agent_rules), 1, agent_rules)
        self.assertIn(f" -s {LOOPBACK_NET} ", agent_rules[0])

    def test_ssh_stays_unconditional(self):
        ssh_rules = [
            l for l in self.accept_lines()
            if " -s " not in l and not re.search(rf"--dport ({NODE_API_PORT}|{AGENT_PORT})\b", l)
        ]
        self.assertTrue(ssh_rules, "SSH never-drop пропал из цепочки")

    def test_audit_rejects_unconditional_node_api_accept(self):
        rules = f"iptables -A ANTIDDOS -p tcp --dport 22 -j ACCEPT\n" \
                f"iptables -A ANTIDDOS -p tcp --dport {NODE_API_PORT} -j ACCEPT\n"
        result = self.run_watchdog("audit-rules", stdin=rules)
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())

    def test_audit_rejects_agent_port_from_anywhere(self):
        rules = "iptables -A ANTIDDOS -p tcp --dport 22 -j ACCEPT\n" \
                f"iptables -A ANTIDDOS -p tcp --dport {AGENT_PORT} -j ACCEPT\n"
        result = self.run_watchdog("audit-rules", stdin=rules)
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())

    def test_audit_rejects_missing_ssh_accept(self):
        rules = f"iptables -A ANTIDDOS -s {LOOPBACK_NET} -p tcp --dport {AGENT_PORT} -j ACCEPT\n"
        result = self.run_watchdog("audit-rules", stdin=rules)
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())

    def test_audit_accepts_expected_chain(self):
        rules = "iptables -A ANTIDDOS -p tcp --dport 22 -j ACCEPT\n" \
                f"iptables -A ANTIDDOS -s {LOOPBACK_NET} -p tcp --dport {AGENT_PORT} -j ACCEPT\n"
        result = self.run_watchdog("audit-rules", stdin=rules)
        self.assertEqual(result.returncode, 0, result.stdout.decode())

    def test_version_bumped(self):
        self.assertEqual(self.run_watchdog("version").stdout.decode().strip(), "2.5.0")


if __name__ == "__main__":
    unittest.main()
