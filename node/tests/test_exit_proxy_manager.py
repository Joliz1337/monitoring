"""Exit-прокси: менеджер — прогон проверок, выбор, персистентность, переключение.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"

Хост подменён FakeExecutor: скрипт «уже установлен» (sha256 совпадает), probe
и selftest отвечают заготовленным JSON. Socks-сервер поднимается настоящий, на
свободном порту loopback.
"""

import asyncio
import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.exit_proxy import manager as manager_module  # noqa: E402
from app.services.exit_proxy.manager import (  # noqa: E402
    EVENT_MANUAL_SWITCH,
    EVENT_NO_HEALTHY,
    EVENT_STARTED,
    EVENT_SWITCHED,
    FIELD_SEPARATOR,
    HOST_SCRIPT,
    ExitProxyManager,
    ExitProxyValidationError,
)
from app.services.exit_proxy.models import CustomCheck, ExitProxyConfig  # noqa: E402
from app.services.exit_proxy.selection import DiscoveredIp  # noqa: E402

SCRIPT_DIGEST = hashlib.sha256(HOST_SCRIPT.encode("utf-8")).hexdigest()
PRIMARY = "5.255.127.33"
EXTRA = "5.255.127.34"


@dataclass
class FakeResult:
    success: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str = ""


def probe_json(ip: str, country: str = "NL", captcha: bool = False, gemini: str = "ok", checks: str = "[]") -> str:
    return json.dumps({
        "ok": True, "ip": ip, "country": country, "country_confirm": None, "captcha": captcha,
        "gemini": gemini, "warp": "off", "checks": json.loads(checks), "error": None, "elapsed_ms": 5,
    })


class FakeExecutor:
    """Ответ по подстроке команды: `probe ip <addr>` → JSON кандидата, selftest → трасса."""

    def __init__(self):
        self.probes: dict[str, str] = {}
        self.selftest_ip = PRIMARY
        self.commands: list[str] = []

    async def execute(self, command: str, timeout: int = 30, shell: str = "sh") -> FakeResult:
        self.commands.append(command)
        if "sha256sum" in command:
            return FakeResult(stdout=SCRIPT_DIGEST)
        if " probe " in command:
            for address, answer in self.probes.items():
                if f" {address} " in command:
                    return FakeResult(stdout=answer)
            return FakeResult(success=False, exit_code=1, stderr="curl: (7) failed")
        if " selftest " in command:
            return FakeResult(stdout=json.dumps({"ok": True, "ip": self.selftest_ip, "loc": "NL", "warp": "off", "error": None}))
        return FakeResult(success=False, exit_code=1, stderr="no answer")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def start_echo() -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(4096):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def open_through_socks(port: int, target_port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Полный CONNECT через локальный socks: только такое соединение сервер учитывает как активное."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(bytes([5, 1, 0]))
    await writer.drain()
    await reader.readexactly(2)
    writer.write(bytes([5, 1, 0, 1]) + socket.inet_aton("127.0.0.1") + target_port.to_bytes(2, "big"))
    await writer.drain()
    reply = await reader.readexactly(10)
    assert reply[1] == 0, f"socks reply {reply[1]}"
    return reader, writer


class ManagerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "exit_proxy.json"
        self.executor = FakeExecutor()
        self.executor.probes = {PRIMARY: probe_json(PRIMARY), EXTRA: probe_json(EXTRA)}
        self.discovered = [DiscoveredIp(PRIMARY, primary=True), DiscoveredIp(EXTRA, managed=True)]
        self.warp = False
        self.manager = self._new_manager()
        self.echo = await start_echo()

    async def asyncTearDown(self):
        await self.manager.stop()
        self.echo.close()
        await self.echo.wait_closed()
        self.tmp.cleanup()

    def _new_manager(self) -> ExitProxyManager:
        async def discover():
            return self.discovered

        async def warp_probe():
            return self.warp

        return ExitProxyManager(self.executor, state_path=self.state_path, discover_ips=discover, warp_probe=warp_probe)

    def _config(self, **overrides) -> ExitProxyConfig:
        base = dict(enabled=True, port=free_port())
        base.update(overrides)
        return ExitProxyConfig(**base)

    async def test_enable_starts_socks_and_picks_primary_before_any_check(self):
        status = await self.manager.apply_config(self._config())
        self.assertTrue(status.listening)
        self.assertEqual(status.current, f"ip:{PRIMARY}")
        self.assertTrue(status.check_in_progress)
        self.assertEqual(status.events[-1].kind, EVENT_STARTED)

    async def test_run_checks_stores_results_and_keeps_healthy_primary(self):
        await self.manager.apply_config(self._config())
        await self.manager.run_checks()
        status = self.manager.status()
        self.assertEqual(status.current, f"ip:{PRIMARY}")
        self.assertEqual([c.healthy for c in status.candidates], [True, True])
        self.assertTrue(status.self_test and status.self_test.ok)
        self.assertEqual(status.self_test.expected, PRIMARY)
        self.assertFalse(status.check_in_progress)

    async def test_sick_primary_switches_and_drops_its_connections(self):
        await self.manager.apply_config(self._config())
        await self.manager.run_checks()
        echo_port = self.echo.sockets[0].getsockname()[1]

        # Адресов ноды на тестовой машине нет — исходящий bind идёт на loopback
        async def loopback_connect(host, port, bind_ip):
            return await asyncio.open_connection(host, port, local_addr=("127.0.0.1", 0))

        with unittest.mock.patch.object(manager_module, "connect_direct", loopback_connect):
            reader, writer = await open_through_socks(self.manager.config.port, echo_port)

        self.executor.probes[PRIMARY] = probe_json(PRIMARY, country="RU")
        self.executor.selftest_ip = EXTRA
        await self.manager.run_checks()

        status = self.manager.status()
        self.assertEqual(status.current, f"ip:{EXTRA}")
        switch = next(event for event in status.events if event.kind == EVENT_SWITCHED)
        self.assertEqual((switch.from_candidate, switch.to_candidate), (f"ip:{PRIMARY}", f"ip:{EXTRA}"))
        self.assertEqual(await reader.read(), b"")
        self.assertTrue(status.self_test.ok)
        writer.close()

    async def test_nobody_healthy_uses_first_and_reports_once(self):
        await self.manager.apply_config(self._config())
        self.executor.probes = {PRIMARY: probe_json(PRIMARY, captcha=True), EXTRA: probe_json(EXTRA, gemini="blocked")}
        await self.manager.run_checks()
        await self.manager.run_checks()
        events = [event.kind for event in self.manager.events(50)]
        self.assertEqual(events.count(EVENT_NO_HEALTHY), 1)
        self.assertEqual(self.manager.current, f"ip:{PRIMARY}")

    async def test_probe_failure_is_unknown_and_confirmed_healthy_alternative_wins(self):
        await self.manager.apply_config(self._config())
        await self.manager.run_checks()
        self.executor.probes = {EXTRA: probe_json(EXTRA)}
        await self.manager.run_checks()
        status = self.manager.status()
        self.assertEqual(status.current, f"ip:{EXTRA}")
        primary = next(c for c in status.candidates if c.address == PRIMARY)
        self.assertIsNone(primary.healthy)
        self.assertIn("probe failed", primary.last_check.error)

    async def test_state_survives_restart(self):
        await self.manager.apply_config(self._config())
        await self.manager.run_checks()
        await self.manager.stop()

        reloaded = self._new_manager()
        reloaded.load_state()
        self.assertEqual(reloaded.current, f"ip:{PRIMARY}")
        self.assertEqual(len(reloaded.results), 2)
        self.assertTrue(reloaded.config.enabled)
        self.assertEqual(reloaded.status().events[-1].kind, EVENT_STARTED)

    async def test_disable_stops_server_and_port_change_restarts(self):
        first = await self.manager.apply_config(self._config())
        self.assertTrue(first.listening)
        moved = await self.manager.apply_config(self._config(port=free_port()))
        self.assertTrue(moved.listening)
        self.assertNotEqual(moved.port, first.port)
        stopped = await self.manager.apply_config(self._config(enabled=False, port=moved.port))
        self.assertFalse(stopped.listening)
        self.assertEqual(stopped.events[0].kind, "stopped")

    async def test_manual_switch_pins_in_manual_mode(self):
        await self.manager.apply_config(self._config(select_mode="manual"))
        await self.manager.run_checks()
        self.executor.selftest_ip = EXTRA
        status = await self.manager.switch(f"ip:{EXTRA}")
        self.assertEqual(status.current, f"ip:{EXTRA}")
        self.assertEqual(status.pinned_candidate, f"ip:{EXTRA}")
        self.assertEqual(status.events[0].kind, EVENT_MANUAL_SWITCH)
        await self.manager.run_checks()
        self.assertEqual(self.manager.current, f"ip:{EXTRA}")

    async def test_switch_validates_candidate(self):
        await self.manager.apply_config(self._config(candidates_disabled=[f"ip:{EXTRA}"]))
        with self.assertRaises(ExitProxyValidationError):
            await self.manager.switch("ip:9.9.9.9")
        with self.assertRaises(ExitProxyValidationError):
            await self.manager.switch(f"ip:{EXTRA}")

    async def test_warp_candidate_expects_warp_on_in_self_test(self):
        self.warp = True
        self.executor.probes["127.0.0.1:9091"] = json.dumps({
            "ok": True, "ip": "104.28.1.1", "country": "NL", "captcha": False, "gemini": "ok",
            "warp": "on", "checks": [], "error": None, "elapsed_ms": 3,
        })
        await self.manager.apply_config(self._config(candidates_disabled=[f"ip:{PRIMARY}", f"ip:{EXTRA}"]))
        original = self.executor.execute

        async def execute(command, timeout=30, shell="sh"):
            if " selftest " in command:
                return FakeResult(stdout=json.dumps({"ok": True, "ip": "104.28.1.1", "loc": "NL", "warp": "on"}))
            return await original(command, timeout, shell)

        self.executor.execute = execute
        await self.manager.run_checks()
        status = self.manager.status()
        self.assertEqual(status.current, "warp")
        self.assertEqual(status.self_test.expected, "warp=on")
        self.assertTrue(status.self_test.ok)

    async def test_custom_failure_switches_only_after_the_next_run_confirms(self):
        await self.manager.apply_config(self._config())
        await self.manager.run_checks()
        failed = json.dumps([{"name": "Claude", "ok": False, "status": 403, "detail": "blocked: status 403"}])
        self.executor.probes[PRIMARY] = probe_json(PRIMARY, checks=failed)

        await self.manager.run_checks()
        status = self.manager.status()
        self.assertEqual(status.current, f"ip:{PRIMARY}")
        self.assertEqual(status.pending_switch, f"ip:{EXTRA}")
        deferred = [event for event in status.events if event.kind == manager_module.EVENT_DEFERRED]
        self.assertEqual([(e.from_candidate, e.to_candidate) for e in deferred], [(f"ip:{PRIMARY}", f"ip:{EXTRA}")])
        reloaded = self._new_manager()
        reloaded.load_state()
        self.assertEqual(reloaded.status().pending_switch, f"ip:{EXTRA}")

        self.executor.selftest_ip = EXTRA
        await self.manager.run_checks()
        status = self.manager.status()
        self.assertEqual(status.current, f"ip:{EXTRA}")
        self.assertIsNone(status.pending_switch)
        self.assertEqual(sum(1 for e in status.events if e.kind == manager_module.EVENT_DEFERRED), 1)

    async def test_settings_change_does_not_confirm_a_pending_switch(self):
        await self.manager.apply_config(self._config())
        await self.manager.run_checks()
        failed = json.dumps([{"name": "Claude", "ok": False, "status": 403, "detail": "blocked: status 403"}])
        self.executor.probes[PRIMARY] = probe_json(PRIMARY, checks=failed)
        await self.manager.run_checks()
        self.assertEqual(self.manager.status().pending_switch, f"ip:{EXTRA}")

        await self.manager.apply_config(self._config(blocked_countries=["RU", "BY"]))
        self.assertEqual(self.manager.current, f"ip:{PRIMARY}")
        self.assertEqual(self.manager.status().pending_switch, f"ip:{EXTRA}")

    async def test_check_payload_carries_builtin_flags_and_enabled_custom_checks(self):
        checks = [
            CustomCheck(id="claude", name="Claude", url="https://claude.ai/login", block_url_regex="unavailable"),
            CustomCheck(id="off", name="Off", url="https://example.com/", enabled=False),
        ]
        await self.manager.apply_config(self._config(custom_checks=checks, builtin_checks={"gemini": False}))
        lines = self.manager._check_payload().splitlines()
        self.assertEqual(lines[0].split(FIELD_SEPARATOR), ["BUILTIN", "1", "1", "0"])
        self.assertEqual(lines[1].split(FIELD_SEPARATOR), ["CHECK", "Claude", "https://claude.ai/login", "", "", "unavailable", ""])
        self.assertEqual(len(lines), 2)

    async def test_custom_check_failure_marks_candidate_sick_but_switches_only_when_confirmed(self):
        checks = [CustomCheck(id="claude", name="Claude", url="https://claude.ai/login", block_url_regex="unavailable")]
        await self.manager.apply_config(self._config(custom_checks=checks))
        failed = '[{"name":"Claude","ok":false,"status":302,"detail":"blocked: redirected"}]'
        self.executor.probes[PRIMARY] = probe_json(PRIMARY, checks=failed)
        await self.manager.run_checks()
        status = self.manager.status()
        primary = next(c for c in status.candidates if c.address == PRIMARY)
        self.assertIs(primary.healthy, False)
        self.assertEqual((status.current, status.pending_switch), (f"ip:{PRIMARY}", f"ip:{EXTRA}"))
        self.executor.selftest_ip = EXTRA
        await self.manager.run_checks()
        self.assertEqual(self.manager.current, f"ip:{EXTRA}")

    async def test_script_is_installed_when_host_hash_differs(self):
        installed: list[str] = []

        async def fake_write(path, content, mode=None):
            installed.append(path)
            return True

        original_execute = self.executor.execute

        async def execute(command, timeout=30, shell="sh"):
            if "sha256sum" in command:
                return FakeResult(stdout="stale")
            return await original_execute(command, timeout, shell)

        self.executor.execute = execute
        with unittest.mock.patch.object(manager_module, "write_host_file", fake_write):
            await self.manager.apply_config(self._config())
            await self.manager.run_checks()
        self.assertEqual(installed, [manager_module.HOST_SCRIPT_PATH])
        self.assertTrue(self.manager.status().script_installed)


class FakeSiteHandler(BaseHTTPRequestHandler):
    """Сайт для проверок: трасса, челлендж Cloudflare, честный 403 и обычная страница."""

    flaky_hits = 0
    PAGES = {
        "/trace": (200, "ip=127.0.0.1\nwarp=off\n"),
        "/challenge": (403, "<html><title>Just a moment...</title><script>window._cf_chl_opt={}</script></html>"),
        "/blocked": (403, "forbidden"),
        "/fine": (200, "hello"),
    }

    def do_GET(self):  # noqa: N802 — имя задаёт http.server
        status, body = self.PAGES.get(self.path, (404, "nope"))
        if self.path == "/flaky-trace":
            # Первый запрос — «занято», как у выхода под нагрузкой; второй отвечает трассой
            FakeSiteHandler.flaky_hits += 1
            status, body = (503, "busy") if FakeSiteHandler.flaky_hits == 1 else self.PAGES["/trace"]
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


class HostScriptTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bash"), "bash not available")
    def test_script_has_valid_bash_syntax(self):
        # Скрипт уходит байтами: текстовый stdin на Windows подменил бы LF на CRLF
        check = subprocess.run(["bash", "-n"], input=HOST_SCRIPT.encode("utf-8"), capture_output=True)
        self.assertEqual(check.returncode, 0, check.stderr.decode("utf-8", "replace"))

    @unittest.skipUnless(shutil.which("bash") and shutil.which("curl"), "bash and curl required")
    @unittest.skipIf(sys.platform == "win32", "the script's curl --interface and coreutils need a POSIX host")
    def test_trace_is_retried_once(self):
        site = ThreadingHTTPServer(("127.0.0.1", 0), FakeSiteHandler)
        threading.Thread(target=site.serve_forever, daemon=True).start()
        self.addCleanup(site.shutdown)
        FakeSiteHandler.flaky_hits = 0
        base = f"http://127.0.0.1:{site.server_address[1]}"
        payload = FIELD_SEPARATOR.join(["BUILTIN", "0", "0", "0"]) + "\n"
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        run = subprocess.run(
            ["bash", "-s", "probe", "ip", "127.0.0.1", "5", encoded],
            input=HOST_SCRIPT.encode("utf-8"), capture_output=True,
            env={**os.environ, "EXIT_PROXY_TRACE_URL": f"{base}/flaky-trace"},
        )
        self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8", "replace"))
        result = json.loads(run.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertTrue(result["ok"])
        self.assertEqual(result["ip"], "127.0.0.1")
        self.assertEqual(FakeSiteHandler.flaky_hits, 2)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("curl"), "bash and curl required")
    @unittest.skipIf(sys.platform == "win32", "the script's curl --interface and coreutils need a POSIX host")
    def test_cloudflare_challenge_is_untested_not_blocked(self):
        site = ThreadingHTTPServer(("127.0.0.1", 0), FakeSiteHandler)
        threading.Thread(target=site.serve_forever, daemon=True).start()
        self.addCleanup(site.shutdown)
        base = f"http://127.0.0.1:{site.server_address[1]}"
        payload = "\n".join([
            FIELD_SEPARATOR.join(["BUILTIN", "0", "0", "0"]),
            FIELD_SEPARATOR.join(["CHECK", "Challenge", f"{base}/challenge", "403", "", "", ""]),
            FIELD_SEPARATOR.join(["CHECK", "Blocked", f"{base}/blocked", "403", "", "", ""]),
            FIELD_SEPARATOR.join(["CHECK", "Fine", f"{base}/fine", "403", "", "", ""]),
        ]) + "\n"
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        run = subprocess.run(
            ["bash", "-s", "probe", "ip", "127.0.0.1", "5", encoded],
            input=HOST_SCRIPT.encode("utf-8"), capture_output=True,
            env={**os.environ, "EXIT_PROXY_TRACE_URL": f"{base}/trace"},
        )
        self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8", "replace"))
        result = json.loads(run.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertTrue(result["ok"])
        self.assertEqual(result["ip"], "127.0.0.1")
        by_name = {item["name"]: item for item in result["checks"]}
        self.assertEqual(
            by_name["Challenge"], {"name": "Challenge", "ok": False, "status": None, "detail": "cloudflare challenge, not tested"},
        )
        self.assertEqual(by_name["Blocked"], {"name": "Blocked", "ok": False, "status": 403, "detail": "blocked: status 403"})
        self.assertEqual(by_name["Fine"], {"name": "Fine", "ok": True, "status": 200, "detail": "status 200"})


if __name__ == "__main__":
    unittest.main()
