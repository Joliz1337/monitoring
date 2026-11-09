"""Откуда апдейтер панели берёт код: GitHub, а зеркало — только со сверкой коммита.

Шаг клона рендерится в sh-текст и прогоняется настоящим `sh` с подставным
`git`: он «роняет» github.com, а «зеркало» отдаёт клон с заданным HEAD. За
шагом клона в драйвере стоит маркер запуска update.sh — по нему видно,
дошло ли бы дело до исполнения скачанного кода.

Запуск из panel/backend:  python -m unittest tests.test_update_source
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import update_source  # noqa: E402

EXPECTED = "0123456789abcdef0123456789abcdef01234567"
FOREIGN = "fedcba9876543210fedcba9876543210fedcba98"
RUN_MARKER = "UPDATE_SH_STARTED"

# Подставной git: клон кладёт в каталог файл с «HEAD», rev-parse его читает.
# Каждый вызов clone пишется в git.log — так видно, ходил ли апдейтер на зеркало
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


class RenderTests(unittest.TestCase):

    def test_mirror_is_absent_without_expected_commit(self):
        step = update_source.render_clone_step("main", "/tmp/clone", None)
        self.assertNotIn("ghfast", step)
        self.assertIn("[ERROR]", step)

    def test_mirror_clone_is_followed_by_commit_check(self):
        step = update_source.render_clone_step("main", "/tmp/clone", EXPECTED)
        self.assertIn(update_source.MIRROR_URL, step)
        self.assertIn("rev-parse HEAD", step)
        self.assertIn(EXPECTED, step)


@unittest.skipIf(sys.platform == "win32", "шаг клона исполняет sh апдейтера, на Windows sh уходит в WSL-лаунчер")
@unittest.skipUnless(shutil.which("sh") and shutil.which("timeout"), "sh/timeout not available")
class CloneStepTests(unittest.TestCase):

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="update-source-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        bin_dir = self.work / "bin"
        bin_dir.mkdir()
        fake_git = bin_dir / "git"
        fake_git.write_bytes(FAKE_GIT.encode())
        fake_git.chmod(0o755)
        self.clone_dir = self.work / "clone"

    def run_step(self, expected_commit, *, github_ok=False, github_head=EXPECTED, mirror_head=EXPECTED):
        step = update_source.render_clone_step("main", str(self.clone_dir), expected_commit)
        driver = self.work / "driver.sh"
        driver.write_bytes(f"set -e\n{step}\necho {RUN_MARKER}\n".encode())
        env = {
            "PATH": f"{self.work / 'bin'}:/usr/bin:/bin",
            "WORK": str(self.work),
            "GITHUB_OK": "1" if github_ok else "0",
            "GITHUB_HEAD": github_head,
            "MIRROR_HEAD": mirror_head,
        }
        return subprocess.run(["sh", str(driver)], env=env, capture_output=True, timeout=60)

    def cloned_from(self) -> list[str]:
        log = self.work / "git.log"
        return log.read_text().split() if log.exists() else []

    def test_verified_mirror_clone_reaches_update(self):
        result = self.run_step(EXPECTED, mirror_head=EXPECTED)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(RUN_MARKER.encode(), result.stdout)
        self.assertEqual(self.cloned_from(), [update_source.REPO_URL, update_source.MIRROR_URL])

    def test_commit_mismatch_aborts_before_update(self):
        result = self.run_step(EXPECTED, mirror_head=FOREIGN)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(RUN_MARKER.encode(), result.stdout)
        self.assertIn(FOREIGN.encode(), result.stdout)
        self.assertFalse(self.clone_dir.exists(), "код с зеркала не должен остаться на диске")

    def test_without_expected_commit_mirror_is_not_used(self):
        result = self.run_step(None)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(RUN_MARKER.encode(), result.stdout)
        self.assertEqual(self.cloned_from(), [update_source.REPO_URL])

    def test_without_expected_commit_github_still_updates(self):
        result = self.run_step(None, github_ok=True, github_head=FOREIGN)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(RUN_MARKER.encode(), result.stdout)


if __name__ == "__main__":
    unittest.main()
