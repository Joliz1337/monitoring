"""Внутренний API ноды слушает локальный сокет, а не TCP-порт на петле.

У uvicorn нет своей аутентификации: порт на петле доступен любому процессу и
контейнеру хоста, а в каталог сокета заходят только root и воркеры nginx. Тесты сверяют, что compose, шаблон
nginx и host-скрипты сходятся на одном пути сокета, а образ по-прежнему
поднимается со старыми compose-файлами (панель доставляет новый образ и на ноду,
чей код на диске не обновился).
Запуск:  cd node && python -m unittest tests.test_agent_socket  (сокет — под WSL)
"""

import http.server
import json
import re
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import yaml

NODE_DIR = Path(__file__).resolve().parents[1]
COMPOSE = NODE_DIR / "docker-compose.yml"
DOCKERFILE = NODE_DIR / "Dockerfile"
NGINX_TEMPLATE = NODE_DIR / "nginx" / "templates" / "api.conf.template"
APPLY_UPDATE = NODE_DIR / "scripts" / "apply-update.sh"
DEPLOY = NODE_DIR / "deploy.sh"

SOCKET_DIR = "/run/monitoring-node"
SOCKET = f"{SOCKET_DIR}/api.sock"
# uid/gid воркеров nginx в официальном образе nginx
NGINX_GID = "101"
LEGACY_PORT = "7500"


def compose_services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def agent_command() -> str:
    command = compose_services()["api"].get("command")
    if isinstance(command, list):
        return " ".join(command)
    return command or ""


class ComposeAgentSocketTest(unittest.TestCase):
    def test_agent_starts_on_socket(self):
        command = agent_command()
        self.assertIn(f"--uds {SOCKET}", command)
        self.assertNotIn("--port", command)
        self.assertNotIn("--host", command)

    def test_uvicorn_replaces_shell(self):
        # Без exec SIGTERM получает sh: uvicorn не завершается штатно, lifespan
        # не останавливает фоновые задачи, сокет не удаляется
        self.assertRegex(agent_command(), r"\bexec uvicorn\b")

    def test_socket_dir_open_only_to_nginx_group(self):
        command = agent_command()
        self.assertIn(f"chgrp {NGINX_GID} {SOCKET_DIR}", command)
        self.assertIn(f"chmod 0710 {SOCKET_DIR}", command)
        self.assertLess(command.index("chmod"), command.index("exec uvicorn"))

    def test_socket_dir_mounted_into_api_and_nginx(self):
        services = compose_services()
        self.assertIn(f"{SOCKET_DIR}:{SOCKET_DIR}", services["api"]["volumes"])
        self.assertIn(f"{SOCKET_DIR}:{SOCKET_DIR}:ro", services["nginx"]["volumes"])

    def test_healthcheck_goes_through_socket(self):
        test = compose_services()["api"]["healthcheck"]["test"]
        self.assertEqual(test[:2], ["CMD", "curl"])
        self.assertIn("--unix-socket", test)
        self.assertEqual(test[test.index("--unix-socket") + 1], SOCKET)
        self.assertEqual(test[-1], "http://localhost/health")
        self.assertNotIn(LEGACY_PORT, " ".join(test))


class NginxUpstreamTest(unittest.TestCase):
    def upstream_block(self) -> str:
        match = re.search(r"upstream api_backend \{(.*?)\}", NGINX_TEMPLATE.read_text(encoding="utf-8"), re.S)
        self.assertIsNotNone(match, "upstream api_backend не найден")
        return match.group(1)

    def test_upstream_is_agent_socket(self):
        servers = re.findall(r"^\s*server\s+([^;]+);", self.upstream_block(), re.M)
        self.assertEqual(servers, [f"unix:{SOCKET}"])

    def test_no_tcp_fallback_upstream(self):
        # Запасной 127.0.0.1:7500 отдал бы запросы панели любому, кто займёт
        # свободный порт на петле, пока агент перезапускается
        self.assertNotIn(LEGACY_PORT, NGINX_TEMPLATE.read_text(encoding="utf-8"))


class DockerfileLegacyCmdTest(unittest.TestCase):
    def test_image_default_serves_legacy_compose(self):
        # Нода, чей код на диске не обновился, получает с панели новый образ и
        # поднимает его со старым compose без command — тот ждёт агента на
        # 127.0.0.1:7500 (healthcheck и шаблон nginx); только петля, не 0.0.0.0
        match = re.search(r"^CMD (\[.*\])\s*$", DOCKERFILE.read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(match, "CMD не найден")
        self.assertEqual(
            json.loads(match.group(1)),
            ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", LEGACY_PORT],
        )


class HostScriptsProbeTest(unittest.TestCase):
    def assert_probes_socket(self, script: Path):
        text = script.read_text(encoding="utf-8")
        self.assertIn(f'AGENT_SOCKET="{SOCKET}"', text)
        self.assertRegex(text, r'curl -sf (--max-time \S+ )?--unix-socket "\$AGENT_SOCKET" "?http://localhost')
        self.assertNotIn(LEGACY_PORT, text)

    def test_apply_update_waits_on_socket(self):
        self.assert_probes_socket(APPLY_UPDATE)

    def test_apply_update_probe_has_deadline(self):
        # Агент, принявший соединение и замолчавший, повесил бы обновление
        # навсегда: у curl без --max-time своего потолка нет
        text = APPLY_UPDATE.read_text(encoding="utf-8")
        self.assertRegex(text, r'curl -sf --max-time \S+ --unix-socket "\$AGENT_SOCKET"')

    def test_deploy_checks_on_socket(self):
        self.assert_probes_socket(DEPLOY)


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        healthy = self.path == "/health"
        body = b'{"status":"ok"}' if healthy else b"{}"
        self.send_response(200 if healthy else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@unittest.skipIf(sys.platform == "win32", "AF_UNIX-сервер и curl хоста: на Windows гоняется под WSL")
class HealthcheckOverRealSocketTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.socket_path = str(Path(tmp.name) / "api.sock")

    def healthcheck(self) -> subprocess.CompletedProcess:
        argv = [self.socket_path if arg == SOCKET else arg
                for arg in compose_services()["api"]["healthcheck"]["test"][1:]]
        return subprocess.run(argv, capture_output=True, timeout=30)

    def serve(self):
        server = socketserver.UnixStreamServer(self.socket_path, _HealthHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def test_healthy_agent_passes(self):
        self.serve()
        result = self.healthcheck()
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_missing_socket_fails(self):
        self.assertNotEqual(self.healthcheck().returncode, 0)


if __name__ == "__main__":
    unittest.main()
