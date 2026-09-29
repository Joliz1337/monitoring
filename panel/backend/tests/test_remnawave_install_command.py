"""Команда установки Remnawave, разбор SSE исполнителя агента и установка по SSH
с загрузкой всего через панель (сервер за ТСПУ).

Отдельно закреплено: агент логирует первые 100 символов команды, поэтому
сертификат не должен попадать в этот префикс — curl-часть обязана идти первой.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncssh  # noqa: E402

from app.services import update_channel  # noqa: E402
from app.services.deploy_service import (  # noqa: E402
    TUNNEL_DENIED_MESSAGE,
    build_remnawave_install_command,
    install_via_panel,
)
from app.services.remnawave_node_install import parse_sse_event  # noqa: E402
from app.services.ssh_target import SSHTarget  # noqa: E402

CERT = "SSL_CERT=eyJub2RlQ2VydFBlbSI6IJERTIFIRSTLINE\neySECONDLINEOFCERT"


class RemnawaveInstallCommandTests(unittest.TestCase):
    def tearDown(self):
        update_channel.set_current_branch(update_channel.STABLE_BRANCH)

    def test_installs_only_remnawave(self):
        command = build_remnawave_install_command(CERT)
        self.assertIn("MON_INSTALL_REMNAWAVE=1", command)
        self.assertNotIn("MON_INSTALL_NODE", command)
        self.assertNotIn("NODE_SECRET", command)
        self.assertTrue(command.endswith("--unattended"))

    def test_cert_newlines_are_escaped(self):
        command = build_remnawave_install_command("line1\r\nline2\nline3")
        self.assertIn("line1\\nline2\\nline3", command)
        self.assertNotIn("line1\r\nline2", command)

    def test_cert_never_reaches_node_log_prefix(self):
        # host_executor логирует command[:100] — секрет должен быть дальше
        command = build_remnawave_install_command(CERT)
        self.assertNotIn(CERT[:20], command[:100])
        self.assertTrue(command[:100].startswith("curl -fsSL"))

    def test_dev_channel_is_propagated(self):
        update_channel.set_current_branch(update_channel.DEV_BRANCH)
        command = build_remnawave_install_command(CERT)
        self.assertIn("MON_BRANCH=dev", command)
        self.assertIn("/dev/install.sh", command)

    def test_stable_channel_has_no_branch_env(self):
        command = build_remnawave_install_command(CERT)
        self.assertNotIn("MON_BRANCH", command)
        self.assertIn("/main/install.sh", command)


class TunnelCommandTests(unittest.TestCase):
    TUNNEL = "http://127.0.0.1:41234"

    def test_installer_is_downloaded_through_the_tunnel(self):
        command = build_remnawave_install_command(CERT, self.TUNNEL)
        self.assertTrue(command.startswith(f"curl -fsSL --proxy {self.TUNNEL} https://"))

    def test_installer_gets_temporary_proxy(self):
        command = build_remnawave_install_command(CERT, self.TUNNEL)
        self.assertIn(f"MON_PROXY_URL={self.TUNNEL}", command)
        self.assertIn("MON_PROXY_TEMPORARY=1", command)

    def test_without_tunnel_there_is_no_proxy(self):
        command = build_remnawave_install_command(CERT)
        self.assertNotIn("--proxy", command)
        self.assertNotIn("MON_PROXY", command)


class ParseSseEventTests(unittest.TestCase):
    def test_stdout_and_stderr_become_log(self):
        self.assertEqual(
            parse_sse_event("stdout", '{"line": "Installing..."}'),
            {"type": "log", "line": "Installing..."},
        )
        self.assertEqual(
            parse_sse_event("stderr", '{"line": "warn"}'),
            {"type": "log", "line": "warn"},
        )

    def test_done_carries_exit_code(self):
        event = parse_sse_event("done", '{"exit_code": 2, "success": false}')
        self.assertEqual(event, {"type": "_exit", "code": 2, "success": False})

    def test_error_event(self):
        event = parse_sse_event("error", '{"message": "boom"}')
        self.assertEqual(event, {"type": "error", "message": "boom"})

    def test_unknown_event_and_broken_json(self):
        self.assertIsNone(parse_sse_event("ping", "{}"))
        self.assertEqual(parse_sse_event("stdout", "not-json"), {"type": "log", "line": ""})


class _PasswordSshServer(asyncssh.SSHServer):
    allow_forwarding = True

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == "root" and password == "pw"

    def server_requested(self, listen_host: str, listen_port: int) -> bool:
        return self.allow_forwarding


class _NoForwardingSshServer(_PasswordSshServer):
    allow_forwarding = False


async def _run_command(process: asyncssh.SSHServerProcess) -> None:
    process.stdout.write(f"ran: {process.command}\n")
    process.exit(3)


class InstallViaPanelTests(unittest.IsolatedAsyncioTestCase):
    """Живой asyncssh-сервер в процессе: команда получает адрес туннеля,
    а поток событий подходит менеджеру фоновых установок."""

    async def start_ssh(self, server_class) -> int:
        acceptor = await asyncssh.create_server(
            server_class, "127.0.0.1", 0,
            server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
            process_factory=_run_command,
        )
        self.addAsyncCleanup(acceptor.wait_closed)
        self.addCleanup(acceptor.close)
        return acceptor.sockets[0].getsockname()[1]

    async def collect(self, port: int) -> list[dict]:
        target = SSHTarget(host="127.0.0.1", port=port, password="pw")
        return [
            event async for event in install_via_panel(
                target, None, lambda proxy: f"install --proxy {proxy}"
            )
        ]

    async def test_command_runs_with_tunnel_proxy_and_reports_exit_code(self):
        events = await self.collect(await self.start_ssh(_PasswordSshServer))

        lines = [e["line"] for e in events if e["type"] == "log"]
        self.assertTrue(any("ran: install --proxy http://127.0.0.1:" in line for line in lines))
        self.assertTrue(any("качает всё через панель" in line for line in lines))
        self.assertEqual(events[-1], {"type": "done", "exit_code": 3})

    async def test_forwarding_denied_is_a_clear_error(self):
        events = await self.collect(await self.start_ssh(_NoForwardingSshServer))

        self.assertEqual(events[-1], {"type": "error", "message": TUNNEL_DENIED_MESSAGE})
        self.assertFalse(any(e["type"] == "done" for e in events))

    async def test_wrong_password(self):
        port = await self.start_ssh(_PasswordSshServer)
        target = SSHTarget(host="127.0.0.1", port=port, password="wrong")
        events = [e async for e in install_via_panel(target, None, lambda proxy: "true")]

        self.assertEqual(events, [{"type": "error", "message": "SSH: неверный логин, пароль или ключ"}])


if __name__ == "__main__":
    unittest.main()
