"""Клон репозитория в install.sh: зеркало только со сверкой коммита или явным разрешением.

Из install.sh вырезаются функции клона и гоняются настоящим bash с подставным
`git` в PATH: он «роняет» github.com, а «зеркало» отдаёт клон с заданным HEAD.
Спиннер и сообщения заменены заглушками — msg печатает ключ сообщения.

Запуск из node/:  python -m unittest tests.test_installer_mirror
"""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[2] / "install.sh"
CLONE_FUNCTIONS = ("mirror_allowed", "mirror_commit_verified", "clone_repo_with_fallback")
REPO_URL = "https://github.com/Joliz1337/monitoring.git"
MIRROR_URL = f"https://ghfast.top/{REPO_URL}"

EXPECTED = "0123456789abcdef0123456789abcdef01234567"
FOREIGN = "fedcba9876543210fedcba9876543210fedcba98"

FAKE_GIT = r"""#!/bin/sh
if [ "$1" = "-C" ]; then
    cat "$2/.fake-head"
    exit 0
fi
while [ "$1" != "clone" ]; do shift; done
for arg; do last_but_one="$url"; url="$arg"; done
echo "$last_but_one" >> "$WORK/git.log"
case "$last_but_one" in
    https://github.com/*) [ "$GITHUB_OK" = "1" ] || exit 128; head="$GITHUB_HEAD" ;;
    *) head="$MIRROR_HEAD" ;;
esac
mkdir -p "$url"
printf '%s\n' "$head" > "$url/.fake-head"
"""

DRIVER = r"""
msg() { echo "$1"; }
log_info() { echo "[INFO] $1"; }
log_warn() { echo "[WARN] $1"; }
log_error() { echo "[ERROR] $1"; }
spin_retry() { shift 4; "$@"; }
TIMEOUT_GIT_CLONE=10
MAX_RETRIES=1
RETRY_DELAY=0
source "$WORK/clone_lib.sh"
clone_repo_with_fallback "$WORK/clone" main
echo "rc=$?"
"""


def extract_functions(source: str, names: tuple[str, ...]) -> str:
    """Тела функций install.sh: от `name() {` до `}` в первой колонке."""
    blocks = []
    for name in names:
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", source, re.MULTILINE | re.DOTALL)
        if match:
            blocks.append(match.group(0))
    return "\n".join(blocks)


@unittest.skipIf(sys.platform == "win32", "install.sh рассчитан на bash хоста, на Windows bash уходит в WSL-лаунчер")
@unittest.skipUnless(shutil.which("bash"), "bash not available")
class InstallerCloneTests(unittest.TestCase):

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="installer-mirror-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        bin_dir = self.work / "bin"
        bin_dir.mkdir()
        fake_git = bin_dir / "git"
        fake_git.write_bytes(FAKE_GIT.encode())
        fake_git.chmod(0o755)
        library = extract_functions(INSTALLER.read_bytes().decode("utf-8"), CLONE_FUNCTIONS)
        self.assertIn("clone_repo_with_fallback()", library)
        (self.work / "clone_lib.sh").write_bytes(library.encode())
        (self.work / "driver.sh").write_bytes(DRIVER.encode())
        self.clone_dir = self.work / "clone"

    def run_clone(self, *, github_ok=False, mirror_head=EXPECTED, **env) -> tuple[int, str]:
        result = subprocess.run(
            ["bash", str(self.work / "driver.sh")],
            env={
                "PATH": f"{self.work / 'bin'}:/usr/bin:/bin",
                "WORK": str(self.work),
                "GITHUB_OK": "1" if github_ok else "0",
                "GITHUB_HEAD": FOREIGN,
                "MIRROR_HEAD": mirror_head,
                **env,
            },
            capture_output=True,
            timeout=60,
        )
        output = result.stdout.decode("utf-8")
        last_line = output.strip().splitlines()[-1]
        self.assertTrue(last_line.startswith("rc="), output + result.stderr.decode("utf-8", "replace"))
        return int(last_line.removeprefix("rc=")), output

    def cloned_from(self) -> list[str]:
        log = self.work / "git.log"
        return log.read_text().split() if log.exists() else []

    def test_github_clone_needs_nothing_extra(self):
        returncode, _ = self.run_clone(github_ok=True)
        self.assertEqual(returncode, 0)
        self.assertEqual(self.cloned_from(), [REPO_URL])

    def test_mirror_is_not_used_without_permission(self):
        returncode, output = self.run_clone()
        self.assertEqual(returncode, 1)
        self.assertEqual(self.cloned_from(), [REPO_URL])
        self.assertIn("mirror_disabled", output)

    def test_mirror_clone_matching_expected_commit_is_kept(self):
        returncode, _ = self.run_clone(MON_EXPECTED_COMMIT=EXPECTED, mirror_head=EXPECTED)
        self.assertEqual(returncode, 0)
        self.assertEqual(self.cloned_from(), [REPO_URL, MIRROR_URL])
        self.assertTrue(self.clone_dir.exists())

    def test_mirror_clone_with_other_commit_is_discarded(self):
        returncode, output = self.run_clone(MON_EXPECTED_COMMIT=EXPECTED, mirror_head=FOREIGN)
        self.assertEqual(returncode, 1)
        self.assertIn("mirror_commit_mismatch", output)
        self.assertFalse(self.clone_dir.exists(), "код с зеркала не должен остаться на диске")

    def test_malformed_expected_commit_disables_mirror(self):
        for commit in ("abc123", EXPECTED.upper(), EXPECTED + ";id"):
            with self.subTest(commit=commit):
                (self.work / "git.log").unlink(missing_ok=True)
                returncode, output = self.run_clone(MON_EXPECTED_COMMIT=commit, MON_ALLOW_UNVERIFIED_MIRROR="1")
                self.assertEqual(returncode, 1)
                self.assertEqual(self.cloned_from(), [REPO_URL])
                self.assertIn("mirror_bad_commit", output)

    def test_unverified_mirror_on_explicit_request_warns(self):
        returncode, output = self.run_clone(MON_ALLOW_UNVERIFIED_MIRROR="1", mirror_head=FOREIGN)
        self.assertEqual(returncode, 0)
        self.assertEqual(self.cloned_from(), [REPO_URL, MIRROR_URL])
        self.assertIn("[WARN] mirror_unverified", output)


if __name__ == "__main__":
    unittest.main()
