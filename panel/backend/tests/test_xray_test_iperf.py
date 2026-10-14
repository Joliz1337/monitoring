"""Тесты замера скорости через iperf3 по пробросу порта в ядре прокси.

Голый unittest, без сети: запуск iperf3 и ядра подменяются.

Закачка публичных файлов упирается в сами серверы: Selectel отдаёт одному IP
~2,3 Гбит/с, Яндекс ~4,7, OVH ~6 и после этого отвечает 429. iperf3-серверы
на тех же условиях давали 7,6–8,5 Гбит/с, поэтому основной замер идёт через
них, а файлы остаются запасным путём. У iperf3 нет поддержки SOCKS: трафик в
ключ заводится пробросом локального порта на iperf3-сервер через то же ядро.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.xray_test import probes, runner  # noqa: E402
from app.services.xray_test.config_builder import (  # noqa: E402
    FORWARD_HOST_TOKEN,
    FORWARD_PORT_TOKEN,
    build_forward,
    build_forward_template,
)
from app.services.xray_test.config_builder.batch import INBOUND_TAG, OUTBOUND_TAG  # noqa: E402
from app.services.xray_test.models import Core, FailReason  # noqa: E402
from app.services.xray_test.parsers import parse_link  # noqa: E402

UUID = "11111111-2222-3333-4444-555555555555"

DONE = """[SUM]   0.00-6.00   sec  5.96 GBytes  8533 Mbits/sec    0             sender
[SUM]   0.00-6.00   sec  5.96 GBytes  8532 Mbits/sec                  receiver

iperf Done."""
BUSY = "iperf3: error - the server is busy running a test. try again later"
UNREACHABLE = "iperf3: error - control socket has closed unexpectedly"


class IperfServersTest(unittest.TestCase):
    def test_russian_exit_measures_to_russian_server(self):
        self.assertIn(probes.iperf_servers_for("RU")[0], probes.IPERF_SERVERS_RU)

    def test_foreign_exit_measures_to_european_server(self):
        self.assertIn(probes.iperf_servers_for("NL")[0], probes.IPERF_SERVERS_WORLD)
        self.assertIn(probes.iperf_servers_for(None)[0], probes.IPERF_SERVERS_WORLD)

    def test_other_region_kept_as_last_resort(self):
        self.assertIn(probes.iperf_servers_for("RU")[-1], probes.IPERF_SERVERS_WORLD)
        self.assertIn(probes.iperf_servers_for("NL")[-1], probes.IPERF_SERVERS_RU)

    def test_attempts_bounded(self):
        for country in ("RU", "NL"):
            self.assertLessEqual(
                len(probes.iperf_servers_for(country)), probes.IPERF_SERVER_ATTEMPTS
            )


class IperfOutputTest(unittest.TestCase):
    def test_receiver_line_is_the_result(self):
        """Меряется скачивание (-R): итог — строка receiver, не sender."""
        run = probes.parse_iperf_output(DONE)
        self.assertEqual(run.mbps, 8532.0)
        self.assertFalse(run.busy)

    def test_busy_server(self):
        run = probes.parse_iperf_output(BUSY)
        self.assertTrue(run.busy)
        self.assertIsNone(run.mbps)

    def test_failure(self):
        run = probes.parse_iperf_output(UNREACHABLE)
        self.assertFalse(run.busy)
        self.assertIsNone(run.mbps)

    def test_command_downloads_through_forward(self):
        command = probes.iperf_command(15201)
        self.assertEqual(command[:5], ["iperf3", "-c", "127.0.0.1", "-p", "15201"])
        self.assertIn("-R", command)
        self.assertEqual(command[command.index("-P") + 1], str(probes.SPEED_STREAMS))
        # Разгон соединения отбрасывается, иначе он занижал бы итог
        self.assertIn("-O", command)


class IperfSpeedTest(unittest.IsolatedAsyncioTestCase):
    """Перебор занятых портов и недоступных серверов."""

    async def _measure(self, answers, country="NL", forward_ok=True):
        opened: list[tuple[str, int]] = []

        @asynccontextmanager
        async def open_forward(host, port):
            opened.append((host, port))
            yield 15201 if forward_ok else None

        async def fake_run(local_port):
            host, port = opened[-1]
            return probes.parse_iperf_output(answers.get((host, port), UNREACHABLE))

        with mock.patch.object(probes, "_iperf_run", new=fake_run), \
                mock.patch.object(probes.shutil, "which", return_value="/usr/bin/iperf3"):
            result = await probes.iperf_speed(country, open_forward)
        return result, opened

    async def test_busy_port_skipped(self):
        server = probes.iperf_servers_for("NL")[0]
        answers = {(server.host, server.ports[0]): BUSY, (server.host, server.ports[1]): DONE}
        result, opened = await self._measure(answers)

        self.assertEqual(result.mbps, 8532.0)
        self.assertIn(server.name, result.server)
        self.assertIn("iperf3", result.server)
        self.assertEqual(opened, [(server.host, server.ports[0]), (server.host, server.ports[1])])

    async def test_unreachable_server_skipped_without_trying_its_ports(self):
        first, second = probes.iperf_servers_for("NL")[:2]
        result, opened = await self._measure({(second.host, second.ports[0]): DONE})

        self.assertIn(second.name, result.server)
        self.assertEqual([host for host, _ in opened].count(first.host), 1)

    async def test_ports_per_server_bounded(self):
        server = probes.iperf_servers_for("NL")[0]
        answers = {(server.host, port): BUSY for port in server.ports}
        _, opened = await self._measure(answers)

        self.assertLessEqual(
            [host for host, _ in opened].count(server.host), probes.IPERF_PORT_ATTEMPTS
        )

    async def test_nothing_measured(self):
        result, _ = await self._measure({})
        self.assertIsNone(result)

    async def test_forward_not_started(self):
        result, opened = await self._measure({}, forward_ok=False)
        self.assertIsNone(result)
        self.assertEqual(len(opened), 1)

    async def test_no_iperf_binary(self):
        with mock.patch.object(probes.shutil, "which", return_value=None):
            result = await probes.iperf_speed("NL", None)
        self.assertIsNone(result)


class ForwardConfigTest(unittest.TestCase):
    """Проброс: вместо socks — порт, ведущий на iperf3-сервер через тот же ключ."""

    def test_xray_forward(self):
        endpoint = parse_link(f"vless://{UUID}@h.io:443?security=tls#k")
        config = build_forward(endpoint, Core.XRAY, 15201, "iperf.example", 5203)

        inbound = config["inbounds"][0]
        self.assertEqual(inbound["protocol"], "dokodemo-door")
        self.assertEqual(inbound["listen"], "127.0.0.1")
        self.assertEqual(inbound["port"], 15201)
        self.assertEqual(
            inbound["settings"], {"address": "iperf.example", "port": 5203, "network": "tcp"}
        )
        self.assertEqual(inbound["tag"], INBOUND_TAG)
        self.assertEqual(config["routing"]["rules"][0]["outboundTag"], OUTBOUND_TAG)

    def test_singbox_forward(self):
        endpoint = parse_link("hysteria2://pw@h.io:443#hy")
        config = build_forward(endpoint, Core.SINGBOX, 15201, "iperf.example", 5203)

        inbound = config["inbounds"][0]
        self.assertEqual(inbound["type"], "direct")
        self.assertEqual(inbound["listen"], "127.0.0.1")
        self.assertEqual(inbound["listen_port"], 15201)
        self.assertEqual(inbound["override_address"], "iperf.example")
        self.assertEqual(inbound["override_port"], 5203)
        self.assertEqual(inbound["network"], "tcp")

    def test_template_tokens_survive_json(self):
        """Ноде уходит шаблон: адрес и порт сервера она подставляет сама."""
        for link, core, key in (
            (f"vless://{UUID}@h.io:443?security=tls#k", Core.XRAY, ("settings", "port")),
            ("hysteria2://pw@h.io:443#hy", Core.SINGBOX, ("override_port",)),
        ):
            text = build_forward_template(parse_link(link), core, 15201)
            filled = text.replace(FORWARD_HOST_TOKEN, "iperf.example").replace(
                FORWARD_PORT_TOKEN, "5203"
            )
            inbound = json.loads(filled)["inbounds"][0]
            port = inbound
            for part in key:
                port = port[part]
            self.assertEqual(port, 5203)
            self.assertIn("iperf.example", filled)


class PanelForwardTest(unittest.IsolatedAsyncioTestCase):
    """Проброс на панели: своё ядро на один запуск iperf3, потом гасится."""

    def _opener(self, panel_runner):
        endpoint = parse_link(f"vless://{UUID}@h.io:443?security=tls#k")
        return panel_runner._forward_opener(endpoint, Core.XRAY, Path("/bin/xray"))

    async def test_core_stopped_after_run(self):
        panel_runner = runner.LocalCoreRunner(measure_speed=True)
        spawn = mock.AsyncMock(return_value="launched")
        shutdown = mock.AsyncMock()
        with mock.patch.object(panel_runner, "_spawn", new=spawn), \
                mock.patch.object(runner, "_shutdown_core", new=shutdown):
            async with self._opener(panel_runner)("iperf.example", 5203) as port:
                self.assertIsInstance(port, int)

        shutdown.assert_awaited_once_with("launched")
        settings = spawn.call_args.args[0]["inbounds"][0]["settings"]
        self.assertEqual((settings["address"], settings["port"]), ("iperf.example", 5203))

    async def test_failed_core_gives_no_port(self):
        panel_runner = runner.LocalCoreRunner(measure_speed=True)

        async def fail(*args, **kwargs):
            raise runner._CoreStartError(FailReason.CORE_START_FAILED, "не поднялось")

        with mock.patch.object(panel_runner, "_spawn", new=fail):
            async with self._opener(panel_runner)("iperf.example", 5203) as port:
                self.assertIsNone(port)


if __name__ == "__main__":
    unittest.main()
