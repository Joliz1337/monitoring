"""Тесты замера скорости через проверяемый прокси.

Голый unittest, без сети: закачка подменяется моком.

Сервер для замера выбирается по стране выхода. Cloudflare в РФ режется ТСПУ —
после первых 16 КБ соединение замирает, — поэтому ключ с выходом в России мерит
до российского сервера, остальные — до Cloudflare. Сервер, не отдавший данных,
пропускается: иначе ключ с рабочим каналом получил бы прочерк вместо скорости.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest
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
        self.assertIsNone(probes._mbps(probes.SPEED_BYTES, 0.0))


class PanelConcurrencyTest(unittest.TestCase):
    """Проверка с замером не должна ждать очереди на закачку внутри своего таймаута.

    Раньше 128 проверок панели стояли в очереди к двум слотам замера, и почти
    все падали по таймауту ячейки, так и не начав качать.
    """

    def test_checks_in_flight_match_speed_slots(self):
        r = runner.LocalCoreRunner(measure_speed=True)
        self.assertLessEqual(r.workers * r.batch_size, probes.SPEED_CONCURRENCY)

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
