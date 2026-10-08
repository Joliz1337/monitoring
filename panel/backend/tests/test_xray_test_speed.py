"""Тесты замера скорости через проверяемый прокси.

Голый unittest, без внешней сети: выбор сервера проверяется моком закачки,
потоки и зависание — локальным HTTP-сервером.

Сервер для замера выбирается по стране выхода. Cloudflare в РФ режется ТСПУ —
после первых 16 КБ соединение замирает, — поэтому ключ с выходом в России мерит
до российского сервера, остальные — до Cloudflare. Сервер, не отдавший данных,
пропускается: иначе ключ с рабочим каналом получил бы прочерк вместо скорости.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.xray_test import probes, runner  # noqa: E402
from app.services.xray_test.probes import ProbeOptions  # noqa: E402


class SpeedServersTest(unittest.TestCase):
    def test_russian_exit_measures_to_russian_server(self):
        servers = probes.speed_servers_for("RU")
        self.assertEqual(servers[0].name, "Selectel")
        self.assertIn(servers[0], probes.SPEED_SERVERS_RU)

    def test_foreign_exit_measures_to_cloudflare(self):
        self.assertEqual(probes.speed_servers_for("NL")[0].name, "Cloudflare")

    def test_unknown_exit_treated_as_foreign(self):
        self.assertEqual(probes.speed_servers_for(None)[0].name, "Cloudflare")

    def test_country_case_ignored(self):
        self.assertEqual(probes.speed_servers_for("ru"), probes.speed_servers_for("RU"))

    def test_other_region_kept_as_last_resort(self):
        """Страна выхода могла не определиться — тогда спасает сервер другой группы."""
        self.assertIn(probes.speed_servers_for("RU")[-1], probes.SPEED_SERVERS_WORLD)
        self.assertIn(probes.speed_servers_for("NL")[-1], probes.SPEED_SERVERS_RU)

    def test_attempts_bounded(self):
        for country in ("RU", "NL", None):
            self.assertLessEqual(len(probes.speed_servers_for(country)), probes.SPEED_ATTEMPTS)

    def test_no_duplicates(self):
        for country in ("RU", "NL"):
            servers = probes.speed_servers_for(country)
            self.assertEqual(len(servers), len(set(servers)))


class MbpsTest(unittest.TestCase):
    def test_counts_megabits(self):
        self.assertEqual(probes._mbps(12_500_000, 1.0), 100.0)

    def test_too_little_data_is_not_a_measurement(self):
        """16 КБ до зависания под ТСПУ — не скорость канала, а признак блокировки."""
        self.assertIsNone(probes._mbps(16 * 1024, 0.5))

    def test_zero_time_rejected(self):
        self.assertIsNone(probes._mbps(probes.SPEED_STREAM_BYTES, 0.0))


class AggregateTest(unittest.TestCase):
    """Скорость потоков — общая, по окну от первого байта до последнего.

    Сумма скоростей отдельных потоков завышала бы цифру: поток, закончивший
    за секунду, и поток, качавший четыре, вместе одновременно не шли.
    """

    def test_union_window(self):
        streams = [
            probes._StreamProgress(received=12_500_000, first_byte_at=0.0, last_byte_at=1.0),
            probes._StreamProgress(received=12_500_000, first_byte_at=0.5, last_byte_at=2.0),
        ]
        self.assertEqual(probes._aggregate_mbps(streams), 100.0)

    def test_silent_streams_ignored(self):
        """Поток, не получивший ни байта, не сдвигает начало окна в ноль."""
        streams = [
            probes._StreamProgress(received=12_500_000, first_byte_at=5.0, last_byte_at=6.0),
            probes._StreamProgress(),
        ]
        self.assertEqual(probes._aggregate_mbps(streams), 100.0)

    def test_nothing_received(self):
        self.assertIsNone(probes._aggregate_mbps([probes._StreamProgress()]))


class _Handler(BaseHTTPRequestHandler):
    requests = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).requests += 1
        size = 1_000_000 if self.path == "/fast" else 16 * 1024
        self.send_response(206)
        self.send_header("Content-Length", str(size if self.path == "/fast" else 25_000_000))
        self.end_headers()
        try:
            self.wfile.write(b"x" * size)
            self.wfile.flush()
            if self.path == "/stall":
                time.sleep(3)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class DownloadStreamsTest(unittest.IsolatedAsyncioTestCase):
    """Настоящие закачки с локального сервера: потоки, окно и зависание."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    async def test_streams_run_in_parallel(self):
        _Handler.requests = 0
        mbps = await probes._download_mbps(None, f"{self.base}/fast")

        self.assertIsNotNone(mbps)
        self.assertEqual(_Handler.requests, probes.SPEED_STREAMS)

    async def test_stalled_server_dropped_quickly(self):
        """Так выглядит Cloudflare под ТСПУ: 16 КБ на поток и тишина."""
        started = time.perf_counter()
        with mock.patch.object(probes, "SPEED_STALL_SECONDS", 0.5):
            mbps = await probes._download_mbps(None, f"{self.base}/stall")

        self.assertIsNone(mbps)
        self.assertLess(time.perf_counter() - started, 2.5)


class PanelConcurrencyTest(unittest.TestCase):
    """Замер идёт по одному ключу за раз и не ждёт очереди внутри своего таймаута.

    Раньше 128 проверок панели стояли в очереди к двум слотам замера, и почти
    все падали по таймауту ячейки, так и не начав качать. А две одновременные
    многопоточные закачки делили бы канал точки, и каждая мерила бы свою долю.
    """

    def test_one_measured_check_at_a_time(self):
        r = runner.LocalCoreRunner(measure_speed=True)
        self.assertEqual(r.workers * r.batch_size, 1)

    def test_plain_run_keeps_full_parallelism(self):
        r = runner.LocalCoreRunner()
        self.assertGreater(r.workers * r.batch_size, probes.SPEED_CONCURRENCY)

    def test_cell_timeout_covers_every_attempt(self):
        plain = runner._cell_timeout(ProbeOptions())
        with_speed = runner._cell_timeout(ProbeOptions(speed=True))
        self.assertGreaterEqual(with_speed - plain, probes.SPEED_BUDGET)

    def test_core_outlives_slowest_cell(self):
        """Иначе сборщик утечек убьёт ядро посреди замера."""
        self.assertGreater(
            runner.CORE_MAX_LIFETIME, runner._cell_timeout(ProbeOptions(speed=True))
        )


class DownloadSpeedTest(unittest.IsolatedAsyncioTestCase):
    async def _measure(self, answers: dict, country="NL"):
        async def fake(proxy, url):
            return answers.get(url)

        with mock.patch.object(probes, "_download_mbps", new=fake):
            return await probes.download_speed(7501, country)

    async def test_first_server_answers(self):
        first = probes.speed_servers_for("NL")[0]
        result = await self._measure({first.url: 93.4})

        self.assertEqual(result.mbps, 93.4)
        self.assertEqual(result.server, first.name)

    async def test_falls_back_to_next_server(self):
        servers = probes.speed_servers_for("RU")
        result = await self._measure({servers[1].url: 41.0}, country="RU")

        self.assertEqual(result.mbps, 41.0)
        self.assertEqual(result.server, servers[1].name)

    async def test_all_servers_failed(self):
        result = await self._measure({})

        self.assertIsNone(result.mbps)
        self.assertIsNone(result.server)


if __name__ == "__main__":
    unittest.main()
