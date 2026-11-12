"""Белый список анти-DDoS грузится в ipset одним `ipset restore`.

Гоняет настоящий configs/ddos-watchdog.sh (whitelist-sync) под bash. Вместо ipset
в PATH стоит заглушка: она записывает свои вызовы и то, что пришло в `restore`.
Под root не запускается: скрипт записал бы список в /opt/monitoring/antiddos хоста.
Запуск:  python -m unittest discover -s node/tests   (на Windows — под WSL)
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WATCHDOG = Path(__file__).resolve().parents[2] / "configs" / "ddos-watchdog.sh"
LIVE_SET = "antiddos_allow"
STAGING_SET = "antiddos_allow_swap"
RUNS_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

FAKE_IPSET = """#!/bin/sh
echo "$*" >> "$IPSET_RECORD_DIR/calls"
[ "$1" = restore ] || exit 0
cat > "$IPSET_RECORD_DIR/restore.stdin"
exit "$IPSET_RESTORE_EXIT_CODE"
"""


@unittest.skipIf(sys.platform == "win32", "bash-скрипт хоста: на Windows гоняется под WSL")
@unittest.skipIf(RUNS_AS_ROOT, "под root скрипт записал бы список в /opt/monitoring/antiddos хоста")
class WhitelistSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        self.stub_dir = Path(workdir.name) / "bin"
        self.record_dir = Path(workdir.name) / "record"
        self.stub_dir.mkdir()
        self.record_dir.mkdir()
        fake_ipset = self.stub_dir / "ipset"
        fake_ipset.write_text(FAKE_IPSET)
        fake_ipset.chmod(0o755)

    def sync(self, *entries: str, restore_exit_code: int = 0) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{self.stub_dir}:{os.environ['PATH']}",
            "IPSET_RECORD_DIR": str(self.record_dir),
            "IPSET_RESTORE_EXIT_CODE": str(restore_exit_code),
        }
        return subprocess.run(
            ["bash", str(WATCHDOG), "whitelist-sync"],
            input="\n".join(entries).encode(), capture_output=True, env=env, timeout=60,
        )

    def ipset_calls(self) -> list[str]:
        return (self.record_dir / "calls").read_text().splitlines()

    def loaded_entries(self) -> list[str]:
        restore_input = (self.record_dir / "restore.stdin").read_text().splitlines()
        return [line.split()[2] for line in restore_input if line.startswith(f"add {STAGING_SET} ")]

    def test_whole_list_goes_through_a_single_restore(self):
        # Отдельный процесс ipset на каждый адрес не укладывался в таймаут запроса на большом списке
        result = self.sync("10.0.0.1", "10.0.0.2", "192.168.0.0/24")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(sorted(self.loaded_entries()), ["10.0.0.1", "10.0.0.2", "192.168.0.0/24"])
        calls = self.ipset_calls()
        self.assertEqual([call for call in calls if call.startswith("restore")], ["restore"])
        self.assertEqual([call for call in calls if call.startswith("add ")], [])

    def test_loaded_list_replaces_the_live_set(self):
        self.sync("10.0.0.1")

        self.assertIn(f"swap {STAGING_SET} {LIVE_SET}", self.ipset_calls())

    def test_entries_ipset_cannot_take_as_written_are_left_out(self):
        # restore обрывается на первой отвергнутой строке: один такой адрес оставил бы
        # ноду без всего списка. Ведущий ноль ipset читает как восьмеричное число —
        # `08` отвергает, а `010` молча превращает в 8
        unusable = ["0.0.0.0/0", "10.0.0.1/33", "300.0.0.1", "10.0.0.08", "010.0.0.1", "10.0.0.2/08"]

        result = self.sync("10.0.0.3", *unusable, "10.0.0.0/8")

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(sorted(self.loaded_entries()), ["10.0.0.0/8", "10.0.0.3"])

    def test_failed_restore_leaves_the_live_set_untouched(self):
        result = self.sync("10.0.0.1", restore_exit_code=1)

        self.assertEqual(result.returncode, 1)
        self.assertEqual([call for call in self.ipset_calls() if call.startswith("swap")], [])

    def test_last_output_line_is_the_number_of_entries(self):
        # Агент ноды читает число записей из последней строки вывода
        result = self.sync("10.0.0.1", "10.0.0.2")

        self.assertEqual(result.stdout.decode().strip().splitlines()[-1], "2")


if __name__ == "__main__":
    unittest.main()
