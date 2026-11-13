"""Аварийный режим: порт API ноды не пускается безусловно, транзит ограничен отдельной цепочкой.

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
TRANSIT_CHAIN = "ANTIDDOS_TRANSIT"
# Цепочка INPUT, к которой у аудита нет претензий, — чтобы отказ был только из-за транзита
SSH_AND_AGENT_RULES = (
    "iptables -A ANTIDDOS -p tcp --dport 22 -j ACCEPT\n"
    f"iptables -A ANTIDDOS -s {LOOPBACK_NET} -p tcp --dport {AGENT_PORT} -j ACCEPT\n"
)

ACCEPT_DPORT = re.compile(r"--dport\s+(\d+)\b.*-j ACCEPT")


class WatchdogCase(unittest.TestCase):
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


@unittest.skipIf(sys.platform == "win32", "bash-скрипт хоста: на Windows гоняется под WSL")
class WatchdogNeverDropTest(WatchdogCase):
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
        self.assertEqual(self.run_watchdog("version").stdout.decode().strip(), "2.6.0")


@unittest.skipIf(sys.platform == "win32", "bash-скрипт хоста: на Windows гоняется под WSL")
class WatchdogTransitChainTest(WatchdogCase):
    """Транзит (порты контейнеров, проброс) идёт мимо INPUT — у него своя цепочка.

    Она стоит в mangle/POSTROUTING, где проходит и весь исходящий трафик хоста,
    поэтому важно не только что она режет, но и чего она не трогает.
    """

    def dry_run_lines(self) -> list[str]:
        result = self.run_watchdog("dry-run")
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        return result.stdout.decode().splitlines()

    def transit_rules(self) -> list[str]:
        return [l for l in self.dry_run_lines() if f"-A {TRANSIT_CHAIN} " in l]

    def test_own_traffic_leaves_before_any_drop(self):
        rules = self.transit_rules()
        first_drop = next(i for i, l in enumerate(rules) if "-j DROP" in l)
        exits = rules[:first_drop]
        self.assertTrue(any("ESTABLISHED" in l and "-j RETURN" in l for l in exits), exits)
        self.assertTrue(any("! --ctstate DNAT" in l and "-j RETURN" in l for l in exits), exits)

    def test_invalid_packets_are_never_matched(self):
        # SYN-ACK, который SYNPROXY шлёт клиенту, conntrack видит как INVALID
        for line in self.transit_rules():
            self.assertNotIn("INVALID", line)

    def test_limit_spares_whitelist_host_and_private_sources(self):
        rules = self.transit_rules()
        limit = next(i for i, l in enumerate(rules) if "hashlimit" in l)
        before_limit = "\n".join(rules[:limit])
        for exemption in (
            "--match-set antiddos_allow src -j RETURN",
            "--src-type LOCAL -j RETURN",
            "-s 10.0.0.0/8 -j RETURN",
            "-s 172.16.0.0/12 -j RETURN",
            "-s 192.168.0.0/16 -j RETURN",
        ):
            self.assertIn(exemption, before_limit)

    def test_limit_counts_new_connections_per_source_in_its_own_table(self):
        # Общая с INPUT таблица делила бы один запас соединений на порты хоста и транзит
        limits = [l for l in self.transit_rules() if "hashlimit" in l]
        self.assertEqual(len(limits), 1, limits)
        self.assertIn("--ctstate NEW", limits[0])
        self.assertIn("--hashlimit-mode srcip", limits[0])
        self.assertIn("--hashlimit-name ad_emg_fwd", limits[0])
        self.assertTrue(limits[0].endswith("-j DROP"))

    def test_chain_is_jumped_from_mangle_postrouting(self):
        self.assertIn(f"iptables -t mangle -I POSTROUTING 1 -j {TRANSIT_CHAIN}", self.dry_run_lines())

    def test_audit_accepts_generated_chain(self):
        result = self.run_watchdog("audit-rules", stdin=SSH_AND_AGENT_RULES + "\n".join(self.transit_rules()) + "\n")
        self.assertEqual(result.returncode, 0, result.stdout.decode())
        self.assertIn("transit: established and non-DNAT traffic leave before any DROP", result.stdout.decode())

    def test_audit_rejects_drop_ahead_of_the_exits(self):
        rules = self.transit_rules()
        first_drop = next(l for l in rules if "-j DROP" in l)
        result = self.run_watchdog("audit-rules", stdin=SSH_AND_AGENT_RULES + "\n".join([first_drop] + rules) + "\n")
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())

    def test_audit_rejects_chain_matching_invalid(self):
        broken = self.transit_rules() + [
            f"iptables -t mangle -A {TRANSIT_CHAIN} -p tcp -m conntrack --ctstate INVALID -j DROP",
        ]
        result = self.run_watchdog("audit-rules", stdin=SSH_AND_AGENT_RULES + "\n".join(broken) + "\n")
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())


if __name__ == "__main__":
    unittest.main()
