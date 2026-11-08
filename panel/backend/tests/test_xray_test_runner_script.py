"""Разбор задания в configs/xray-test-runner.sh: порядок строк CORE и CELL64.

Скрипт читается с диска, его последняя строка `main "$@"` отрезается, а
скачивание и запуск проверок подменяются заглушками — так гоняется настоящий
разбор `main` и настоящий `ensure_core`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[3] / "configs" / "xray-test-runner.sh"
MAIN_CALL = 'main "$@"'

SHA = "a" * 64
LEGIT_URL = "https://panel.example.com:8443/api/xray-test/bundle/Xk3_-9aZ0PqRsTuVwXyZ1234567890abcdefghijklm"
EVIL_URL = "https://evil.example.com/payload"

STUBS_FOR_MAIN = r"""
kill_stale_cores() { :; }
ensure_core() {
    printf '%s\t%s\t%s\t%s\n' "$@" >> "$WORK/ensure.log"
    printf '%s' "$CORES_DIR/$1-$2"
}
run_cell() {
    printf 'RUN\t%s\t%s\t%s\t%s\t%s\t%s\t%s\tpaths=%s|%s\n' "$@" \
        "${CORE_PATHS[xray]:-}" "${CORE_PATHS[sing-box]:-}"
}
"""

DRIVER_FOR_MAIN = r"""
source "$WORK/runner_lib.sh"
CORES_DIR="$WORK/cores"
""" + STUBS_FOR_MAIN + r"""
main "$PAYLOAD"
"""

DRIVER_FOR_ENSURE_CORE = r"""
source "$WORK/runner_lib.sh"
CORES_DIR="$WORK/cores"
curl() {
    printf '%s\n' "$*" >> "$WORK/curl.log"
    local out=""
    while [ $# -gt 0 ]; do
        [ "$1" = "-o" ] && out="$2"
        shift
    done
    printf 'fake-core' > "$out"
}
ensure_core "$CORE_NAME" "$CORE_VERSION" "$CORE_URL" "$CORE_SHA"
printf '\nrc=%s\n' "$?"
"""


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def job(*rows: tuple[str, ...]) -> str:
    return b64("\n".join("\t".join(row) for row in rows) + "\n")


def core_row(name: str = "xray", version: str = "1.8.24", url: str = LEGIT_URL, sha: str = SHA) -> tuple[str, ...]:
    return ("CORE", name, version, url, sha)


def cell64_row(index: str, address: str, sni: str = "", core: str = "xray", socks: str = "20001") -> tuple[str, ...]:
    return ("CELL64", index, core, b64(address), "443", "0", socks, b64(sni) if sni else "")


OPTS_NO_HTTP = ("OPTS", "1", "0", "0", "0")


@unittest.skipIf(sys.platform == "win32", "скрипт ноды рассчитан на POSIX-хост, на Windows bash уходит в WSL-лаунчер")
@unittest.skipUnless(shutil.which("bash"), "bash not available")
class RunnerScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="xtest-runner-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        source = RUNNER.read_bytes().decode("utf-8")
        stripped, _, tail = source.rpartition(MAIN_CALL)
        self.assertEqual(tail.strip(), "", "после вызова main в скрипте ничего не должно быть")
        (self.work / "runner_lib.sh").write_bytes(stripped.encode("utf-8"))

    def run_bash(self, driver: str, **env: str) -> subprocess.CompletedProcess[bytes]:
        script = self.work / "driver.sh"
        script.write_bytes(driver.encode("utf-8"))
        return subprocess.run(
            ["bash", str(script)],
            env={"PATH": "/usr/bin:/bin:/usr/local/bin", "WORK": str(self.work), **env},
            capture_output=True,
            timeout=60,
        )

    def run_main(self, payload: str) -> tuple[list[str], list[dict], list[list[str]]]:
        result = self.run_bash(DRIVER_FOR_MAIN, PAYLOAD=payload)
        stdout = result.stdout.decode("utf-8")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace") + stdout)
        logs, cells, runs = [], [], []
        for line in stdout.splitlines():
            if line.startswith("RUN\t"):
                runs.append(line.split("\t")[1:])
                continue
            record = json.loads(line)
            if record["type"] == "log":
                logs.append(record["line"])
            elif record["type"] == "cell":
                cells.append(record)
        return logs, cells, runs

    def ensure_calls(self) -> list[list[str]]:
        log = self.work / "ensure.log"
        if not log.exists():
            return []
        return [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines()]

    def run_ensure_core(self, name: str, version: str, url: str, sha: str) -> tuple[int, str]:
        result = self.run_bash(
            DRIVER_FOR_ENSURE_CORE,
            CORE_NAME=name, CORE_VERSION=version, CORE_URL=url, CORE_SHA=sha,
        )
        lines = result.stdout.decode("utf-8").splitlines()
        self.assertTrue(lines, result.stderr.decode("utf-8", "replace"))
        returncode = int(lines[-1].removeprefix("rc="))
        curl_log = self.work / "curl.log"
        return returncode, curl_log.read_text(encoding="utf-8") if curl_log.exists() else ""

    def test_version_command_reports_bumped_runner(self) -> None:
        result = subprocess.run(["bash", str(RUNNER), "version"], capture_output=True, timeout=30)
        self.assertEqual(result.stdout.decode().strip(), "2.14.0")

    def test_core_after_first_cell_is_ignored(self) -> None:
        payload = job(
            core_row(),
            OPTS_NO_HTTP,
            cell64_row("0", "a.example.com"),
            core_row(version="9.9.9", url=EVIL_URL),
            core_row(name="sing-box", version="1.12.0"),
        )
        logs, _, runs = self.run_main(payload)

        self.assertEqual(self.ensure_calls(), [["xray", "1.8.24", LEGIT_URL, SHA]])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][-1], f"paths={self.work}/cores/xray-1.8.24|")
        self.assertEqual(sum("после проверок" in line for line in logs), 2)

    def test_core_after_legacy_cell_is_ignored(self) -> None:
        payload = job(
            OPTS_NO_HTTP,
            ("CELL", "0", "xray", "a.example.com", "443", "0", "20001", ""),
            core_row(),
        )
        logs, _, runs = self.run_main(payload)

        self.assertEqual(self.ensure_calls(), [])
        self.assertEqual(runs[0][-1], "paths=|")
        self.assertTrue(any("после проверок" in line for line in logs))

    def test_duplicate_core_for_same_name_is_ignored(self) -> None:
        payload = job(
            core_row(),
            core_row(version="9.9.9", url=EVIL_URL),
            core_row(name="sing-box", version="1.12.0"),
            OPTS_NO_HTTP,
            cell64_row("0", "a.example.com"),
        )
        logs, _, runs = self.run_main(payload)

        self.assertEqual(
            self.ensure_calls(),
            [["xray", "1.8.24", LEGIT_URL, SHA], ["sing-box", "1.12.0", LEGIT_URL, SHA]],
        )
        self.assertEqual(
            runs[0][-1],
            f"paths={self.work}/cores/xray-1.8.24|{self.work}/cores/sing-box-1.12.0",
        )
        self.assertTrue(any("повторная строка CORE xray" in line for line in logs))

    def test_core_with_unknown_name_is_not_passed_to_download(self) -> None:
        payload = job(core_row(name="../../tmp/x"), OPTS_NO_HTTP, cell64_row("0", "a.example.com"))
        logs, _, _ = self.run_main(payload)

        self.assertEqual(self.ensure_calls(), [])
        self.assertTrue(any("неизвестное ядро" in line for line in logs))

    def test_cell64_decodes_address_and_sni(self) -> None:
        payload = job(
            OPTS_NO_HTTP,
            cell64_row("0", "xn--e1afmkfd.example.com", "sni.example.org"),
            cell64_row("1", "[2001:db8::1]", ""),
        )
        _, cells, runs = self.run_main(payload)

        by_index = {row[0]: row for row in runs}
        self.assertEqual(by_index["0"][1:7], ["xray", "xn--e1afmkfd.example.com", "443", "0", "20001", "sni.example.org"])
        self.assertEqual(by_index["1"][1:7], ["xray", "[2001:db8::1]", "443", "0", "20001", ""])
        self.assertEqual(cells, [])

    def test_empty_sni_keeps_fields_in_place(self) -> None:
        payload = job(OPTS_NO_HTTP, cell64_row("7", "host.example.com", "", socks="20005"))
        _, _, runs = self.run_main(payload)

        self.assertEqual(runs, [["7", "xray", "host.example.com", "443", "0", "20005", "", "paths=|"]])

    def test_legacy_cell_with_raw_values_still_runs(self) -> None:
        payload = job(OPTS_NO_HTTP, ("CELL", "3", "xray", "raw.example.com", "443", "0", "20002", "sni.example.org"))
        _, _, runs = self.run_main(payload)

        self.assertEqual(runs[0][:7], ["3", "xray", "raw.example.com", "443", "0", "20002", "sni.example.org"])

    def test_invalid_base64_fails_only_that_cell(self) -> None:
        broken = ("CELL64", "1", "xray", "!!!not-base64", "443", "0", "20002", "")
        payload = job(OPTS_NO_HTTP, cell64_row("0", "ok.example.com"), broken, cell64_row("2", "ok2.example.com"))
        _, cells, runs = self.run_main(payload)

        self.assertEqual(sorted(row[0] for row in runs), ["0", "2"])
        self.assertEqual(len(cells), 1)
        self.assertEqual((cells[0]["index"], cells[0]["verdict"]), (1, "fail"))

    def test_control_character_in_decoded_value_fails_cell(self) -> None:
        tainted = ("CELL64", "1", "xray", b64("a.example.com\tinjected"), "443", "0", "20002", "")
        payload = job(OPTS_NO_HTTP, tainted, cell64_row("2", "ok.example.com", "bad\nsni"))
        _, cells, runs = self.run_main(payload)

        self.assertEqual(runs, [])
        self.assertEqual(sorted(cell["index"] for cell in cells), [1, 2])

    def test_ensure_core_accepts_url_built_by_panel(self) -> None:
        digest = hashlib.sha256(b"fake-core").hexdigest()
        for url in (
            LEGIT_URL,
            "https://203.0.113.7/api/xray-test/bundle/abc_DEF-123",
            "https://panel.example.com/api/xray-test/bundle/abc",
        ):
            with self.subTest(url=url):
                returncode, curl_log = self.run_ensure_core("xray", "1.8.24", url, digest)
                self.assertEqual(returncode, 0)
                self.assertIn(url, curl_log)
                (self.work / "curl.log").unlink(missing_ok=True)
                shutil.rmtree(self.work / "cores", ignore_errors=True)

    def test_ensure_core_accepts_prerelease_version(self) -> None:
        digest = hashlib.sha256(b"fake-core").hexdigest()
        returncode, _ = self.run_ensure_core("sing-box", "1.13.0-beta.5", LEGIT_URL, digest)
        self.assertEqual(returncode, 0)

    def test_ensure_core_rejects_bad_arguments_without_download(self) -> None:
        digest = hashlib.sha256(b"fake-core").hexdigest()
        cases = {
            "name": ("evil", "1.8.24", LEGIT_URL, digest),
            "name path": ("../xray", "1.8.24", LEGIT_URL, digest),
            "version path": ("xray", "../1.8", LEGIT_URL, digest),
            "version letters first": ("xray", "v1.8", LEGIT_URL, digest),
            "sha short": ("xray", "1.8.24", LEGIT_URL, "abc123"),
            "sha uppercase": ("xray", "1.8.24", LEGIT_URL, digest.upper()),
            "url http": ("xray", "1.8.24", LEGIT_URL.replace("https", "http"), digest),
            "url no bundle": ("xray", "1.8.24", EVIL_URL, digest),
            "url file scheme": ("xray", "1.8.24", "file:///etc/passwd/bundle/x", digest),
            "url userinfo": ("xray", "1.8.24", "https://panel.example.com@evil.example.com/bundle/x", digest),
            "url query": ("xray", "1.8.24", LEGIT_URL + "?x=1", digest),
            "url space": ("xray", "1.8.24", LEGIT_URL + " -o /etc/x", digest),
            "url bundle not segment": ("xray", "1.8.24", "https://h.example.com/xbundle/abc", digest),
        }
        for label, args in cases.items():
            with self.subTest(label):
                returncode, curl_log = self.run_ensure_core(*args)
                self.assertNotEqual(returncode, 0)
                self.assertEqual(curl_log, "")


if __name__ == "__main__":
    unittest.main()
