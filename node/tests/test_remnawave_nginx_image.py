"""Образ nginx установки Remnawave и скрипт перевода на исправленную версию.

scripts/update-remnawave-nginx.sh гоняется настоящим sh с подставным `docker`
в PATH: состояние контейнера лежит файлами в каталоге FAKE, вызовы пишутся в
журнал. `sleep` заменён пустышкой, чтобы тесты не ждали запуска nginx.

Запуск из node/:  python -m unittest tests.test_remnawave_nginx_image
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.remnawave_nginx_manager import FALLBACK_NGINX_IMAGE  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "install.sh"
UPDATE_SCRIPT = REPO / "scripts" / "update-remnawave-nginx.sh"

COMPOSE = """services:
  remnawave-nginx:
    image: nginx:{tag}
    container_name: remnawave-nginx
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro

  remnanode:
    image: remnawave/node:latest
"""

FAKE_DOCKER = r"""#!/bin/sh
echo "$*" >> "$FAKE/calls"
state() { cat "$FAKE/$1" 2>/dev/null; }
compose_tag() { sed -n 's/^    image: nginx:\(.*\)$/\1/p' "$REMNAWAVE_DIR/docker-compose.yml" | head -n1; }
version_of() {
    case "$1" in
        1.2[0-9]*) echo 1.28.3 ;;
        1.30*|stable*) echo 1.30.7 ;;
        *) echo 1.31.9 ;;
    esac
}
case "$1 $2" in
    "inspect remnawave-nginx") [ -f "$FAKE/id" ] ;;
    "inspect -f")
        case "$3" in
            "{{.State.Status}}") state status ;;
            "{{.Id}}") state id ;;
            "{{.State.Status}}/{{.RestartCount}}") echo "$(state status)/$(state restarts)" ;;
            "{{.Config.Image}}") echo "nginx:$(state tag)" ;;
        esac ;;
    "exec remnawave-nginx")
        [ "$(state status)" = running ] || { echo "container is not running" >&2; exit 1; }
        echo "nginx version: nginx/$(state version)" >&2 ;;
    "compose pull")
        [ ! -f "$FAKE/pull-fails" ] || { echo "pull access denied" >&2; exit 1; }
        [ -f "$FAKE/registry-has-nothing-new" ] || touch "$FAKE/pulled" ;;
    "compose run")
        [ ! -f "$FAKE/config-rejected" ] || { echo "nginx: [emerg] unknown directive"; exit 1; } ;;
    "compose up")
        tag=$(compose_tag)
        # compose пересоздаёт контейнер, когда сменился тег или под прежним тегом скачан новый образ
        if [ "$tag" != "$(state tag)" ] || [ -f "$FAKE/pulled" ]; then
            rm -f "$FAKE/pulled"
            echo "$(state id)+" > "$FAKE/id"
            echo "$tag" > "$FAKE/tag"
            version_of "$tag" > "$FAKE/version"
            if [ -f "$FAKE/start-fails" ] && [ "$tag" != "$(state installed-tag)" ]; then
                echo restarting > "$FAKE/status"; echo 1 > "$FAKE/restarts"
            else
                echo running > "$FAKE/status"; echo 0 > "$FAKE/restarts"
            fi
        fi ;;
    "logs --tail") echo "bind() to 0.0.0.0:443 failed" ;;
esac
"""


class ImageAgreementTests(unittest.TestCase):
    """Образ задан в трёх местах на двух языках: расхождение оставило бы часть
    нод на ветке, которую nginx уже не исправляет."""

    def test_installer_manager_and_update_script_name_one_branch(self):
        installer_image = re.search(
            r"^  remnawave-nginx:\n    image: (\S+)$", INSTALLER.read_text(encoding="utf-8"), re.MULTILINE
        ).group(1)
        script = UPDATE_SCRIPT.read_text(encoding="utf-8")
        stable_branch = re.search(r"^STABLE_BRANCH=(\S+)$", script, re.MULTILINE).group(1)
        fixed_stable = re.search(r"^FIXED_STABLE=(\S+)$", script, re.MULTILINE).group(1)

        self.assertEqual(installer_image, FALLBACK_NGINX_IMAGE)
        self.assertEqual(f"nginx:{stable_branch}", FALLBACK_NGINX_IMAGE)
        self.assertTrue(fixed_stable.startswith(f"{stable_branch}."), fixed_stable)


@unittest.skipIf(sys.platform == "win32", "скрипт рассчитан на sh хоста, на Windows sh уходит в WSL-лаунчер")
class UpdateScriptTests(unittest.TestCase):

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="rw-nginx-image-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.fake = self.work / "fake"
        self.install_dir = self.work / "remnawave"
        bin_dir = self.work / "bin"
        for directory in (self.fake, self.install_dir, bin_dir):
            directory.mkdir()
        for name, body in (("docker", FAKE_DOCKER), ("sleep", "#!/bin/sh\n")):
            tool = bin_dir / name
            tool.write_bytes(body.encode())
            tool.chmod(0o755)
        self.compose = self.install_dir / "docker-compose.yml"

    def install_nginx(self, tag="1.28", version="1.28.3", status="running", restarts="0"):
        self.compose.write_bytes(COMPOSE.format(tag=tag).encode())
        container = {"id": "c1", "tag": tag, "installed-tag": tag,
                     "version": version, "status": status, "restarts": restarts}
        for name, value in container.items():
            (self.fake / name).write_text(f"{value}\n")

    def run_update(self) -> tuple[int, str]:
        result = subprocess.run(
            ["sh", str(UPDATE_SCRIPT)],
            env={
                "PATH": f"{self.work / 'bin'}:/usr/bin:/bin",
                "FAKE": str(self.fake),
                "REMNAWAVE_DIR": str(self.install_dir),
            },
            capture_output=True,
            timeout=60,
        )
        return result.returncode, result.stdout.decode("utf-8") + result.stderr.decode("utf-8", "replace")

    def compose_image(self) -> str:
        return re.search(r"^    image: (nginx:\S+)$", self.compose.read_text(), re.MULTILINE).group(1)

    def compose_calls(self) -> list[str]:
        log = self.fake / "calls"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line.split()[1] for line in lines if line.startswith("compose ")]

    def test_abandoned_branch_moves_to_stable(self):
        self.install_nginx()
        returncode, output = self.run_update()
        self.assertEqual(returncode, 0, output)
        self.assertIn("OK: 1.28.3 -> 1.30.7", output)
        self.assertEqual(self.compose_image(), "nginx:1.30")
        self.assertEqual(self.compose_calls(), ["pull", "run", "up"])
        self.assertIn("image: remnawave/node:latest", self.compose.read_text())
        self.assertIn("up -d --no-deps remnawave-nginx", (self.fake / "calls").read_text())

    def test_image_tags(self):
        cases = (
            ("1.27.5-alpine", "nginx:1.30-alpine"),
            ("1.29", "nginx:1.30"),
            ("1.30.5", "nginx:1.30"),
            ("1.31.8-alpine", "nginx:1.31-alpine"),
            ("1.30", "nginx:1.30"),
            ("1.30.7", "nginx:1.30.7"),
            ("alpine", "nginx:alpine"),
            ("stable-alpine", "nginx:stable-alpine"),
        )
        for tag, expected in cases:
            with self.subTest(tag=tag):
                (self.fake / "calls").unlink(missing_ok=True)
                self.install_nginx(tag=tag)
                returncode, output = self.run_update()
                self.assertEqual(returncode, 0, output)
                self.assertEqual(self.compose_image(), expected)

    def test_fixed_nginx_is_left_alone(self):
        self.install_nginx(tag="1.30", version="1.30.7")
        returncode, output = self.run_update()
        self.assertEqual(returncode, 0, output)
        self.assertIn("уже 1.30.7", output)
        self.assertEqual(self.compose_calls(), [])
        self.assertFalse(Path(f"{self.compose}.bak-nginx").exists())

    def test_nginx_switched_off_in_panel_is_not_started(self):
        self.install_nginx(status="exited")
        returncode, output = self.run_update()
        self.assertEqual(returncode, 0, output)
        self.assertIn("выключен", output)
        self.assertEqual(self.compose_image(), "nginx:1.30")
        self.assertEqual(self.compose_calls(), ["pull"])

    def test_config_rejected_by_new_nginx_keeps_running_container(self):
        self.install_nginx()
        (self.fake / "config-rejected").touch()
        returncode, output = self.run_update()
        self.assertEqual(returncode, 1, output)
        self.assertIn("unknown directive", output)
        self.assertEqual(self.compose_image(), "nginx:1.28")
        self.assertEqual(self.compose_calls(), ["pull", "run"])

    def test_failed_pull_changes_nothing(self):
        self.install_nginx()
        (self.fake / "pull-fails").touch()
        returncode, output = self.run_update()
        self.assertEqual(returncode, 1, output)
        self.assertIn("pull access denied", output)
        self.assertEqual(self.compose_image(), "nginx:1.28")
        self.assertEqual(self.compose_calls(), ["pull"])

    def test_nginx_that_does_not_start_returns_to_previous_image(self):
        self.install_nginx()
        (self.fake / "start-fails").touch()
        returncode, output = self.run_update()
        self.assertEqual(returncode, 1, output)
        self.assertIn("возвращаю прежний", output)
        self.assertIn("после отката: running 1.28.3", output)
        self.assertEqual(self.compose_image(), "nginx:1.28")
        self.assertEqual(self.compose_calls(), ["pull", "run", "up", "up"])

    def test_image_pinned_by_hand_is_reported_not_rolled_back(self):
        # Контейнер не пересоздан, и его счётчик рестартов — история прежних падений,
        # а не признак неудачного старта
        self.install_nginx(tag="1.28@sha256:0123", restarts="3")
        (self.fake / "registry-has-nothing-new").touch()
        returncode, output = self.run_update()
        self.assertEqual(returncode, 2, output)
        self.assertIn("WARN: 1.28.3 -> 1.28.3", output)
        self.assertEqual(self.compose_calls(), ["pull", "run", "up"])

    def test_server_without_remnawave_is_skipped(self):
        returncode, output = self.run_update()
        self.assertEqual(returncode, 0, output)
        self.assertIn("SKIP", output)


if __name__ == "__main__":
    unittest.main()
