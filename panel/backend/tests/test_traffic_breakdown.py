"""Разбивка скорости для окна на дашборде (app/services/traffic_breakdown.py).

Главное, что здесь проверяется, — пометка «упал в N раз» у адреса: она должна
загораться, когда клиенты ушли с заблокированного IP, и молчать на обычном
ночном спаде, иначе оператор перестанет ей верить.
"""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # локальный прогон без установленного рантайма панели
    from app.services.traffic_breakdown import (  # noqa: E402
        SPARKLINE_HOURS,
        HourBucket,
        build_ip_rows,
        build_rows,
        detect_drop,
        sparkline,
    )
    from app.services.traffic_ingest import ObservedSpeeds  # noqa: E402
except ImportError as e:  # pragma: no cover
    raise unittest.SkipTest(f"traffic breakdown requires the panel runtime: {e}")

NOW_HOUR = datetime(2026, 11, 6, 12)
MBIT = 125_000  # байт/с


def steady(rate_per_hour: dict[int, float]) -> dict[datetime, HourBucket]:
    """История из полных часов: {часов назад: байт/с}."""
    return {
        NOW_HOUR - timedelta(hours=ago): HourBucket(bytes_total=int(rate * 3600), covered_seconds=3600)
        for ago, rate in rate_per_hour.items()
    }


def day_of(rate_by_ago) -> dict[datetime, HourBucket]:
    return steady({ago: rate_by_ago(ago) for ago in range(27)})


class SparklineTests(unittest.TestCase):
    def test_one_point_per_hour_ending_with_the_current_hour(self):
        points = sparkline(steady({0: 10.0, 23: 5.0}), NOW_HOUR)

        self.assertEqual(len(points), SPARKLINE_HOURS)
        self.assertEqual(points[-1], 10.0)
        self.assertEqual(points[0], 5.0)

    def test_hour_without_observations_is_a_gap_not_zero(self):
        history = steady({0: 10.0})
        history[NOW_HOUR - timedelta(hours=1)] = HourBucket(bytes_total=0, covered_seconds=0)

        points = sparkline(history, NOW_HOUR)

        self.assertIsNone(points[-2])
        self.assertIsNone(points[-3])

    def test_partial_current_hour_is_averaged_over_observed_time(self):
        history = {NOW_HOUR: HourBucket(bytes_total=600_000, covered_seconds=300)}

        self.assertEqual(sparkline(history, NOW_HOUR)[-1], 2_000.0)


class DropDetectionTests(unittest.TestCase):
    def test_clients_leaving_a_banned_address_is_flagged(self):
        # 40 Мбит/с сутки назад в это же время, 2 Мбит/с сейчас
        history = day_of(lambda ago: 2 * MBIT if ago < 3 else 40 * MBIT)

        self.assertEqual(detect_drop(history, NOW_HOUR), {"ratio": 20.0})

    def test_night_dip_against_the_evening_peak_is_not_flagged(self):
        # Сейчас в 10 раз тише, чем было большую часть суток, но вчера в эти часы было так же
        def diurnal(ago: int) -> float:
            hour = (NOW_HOUR - timedelta(hours=ago)).hour
            return 3 * MBIT if hour in (10, 11, 12) else 30 * MBIT

        self.assertIsNone(detect_drop(day_of(diurnal), NOW_HOUR))

    def test_threefold_drop_is_below_the_threshold(self):
        history = day_of(lambda ago: 10 * MBIT if ago < 3 else 30 * MBIT)

        self.assertIsNone(detect_drop(history, NOW_HOUR))

    def test_quiet_address_has_nothing_to_drop_from(self):
        history = day_of(lambda ago: 0.01 * MBIT if ago < 3 else 0.5 * MBIT)

        self.assertIsNone(detect_drop(history, NOW_HOUR))

    def test_traffic_gone_entirely_has_no_ratio(self):
        history = day_of(lambda ago: 0.0 if ago < 3 else 10 * MBIT)

        self.assertEqual(detect_drop(history, NOW_HOUR), {"ratio": None})

    def test_address_without_yesterday_history_is_not_judged(self):
        # Нода только обновилась: сравнивать не с чем
        history = steady({0: 0.0, 1: 0.0, 2: 0.0})

        self.assertIsNone(detect_drop(history, NOW_HOUR))


class BuildRowsTests(unittest.TestCase):
    def test_rows_are_ordered_by_current_speed_then_by_volume(self):
        rates = {"10.0.0.1": ObservedSpeeds(100.0, 100.0), "10.0.0.2": ObservedSpeeds(5_000.0, 5_000.0)}
        history = {"10.0.0.3": steady({1: 50 * MBIT})}

        rows = build_rows(rates, history, NOW_HOUR)

        self.assertEqual([row["key"] for row in rows], ["10.0.0.2", "10.0.0.1", "10.0.0.3"])
        silent = rows[-1]
        self.assertIsNone(silent["rx_bytes_per_sec"])
        self.assertEqual(silent["sparkline"][-2], 50 * MBIT)

    def test_key_seen_only_before_the_sparkline_window_is_hidden(self):
        # Бакет 26 часов назад нужен детекту падения, но в окне за сутки строка была бы пустой
        rows = build_rows({}, {"10.0.0.9": steady({26: MBIT})}, NOW_HOUR)

        self.assertEqual(rows, [])


ADDRESS = "194.55.239.32"


class BuildIpRowsTests(unittest.TestCase):
    def test_address_row_is_the_whole_traffic_with_clients_under_it(self):
        # Скриншот VPN-ноды: клиенты 73/494 Мбит/с, сам сервер к сайтам 461/54 —
        # вместе ровно интерфейс, 534/548
        clients = {ADDRESS: ObservedSpeeds(73 * MBIT, 494 * MBIT)}
        own = {ADDRESS: ObservedSpeeds(461 * MBIT, 54 * MBIT)}

        [row] = build_ip_rows(clients, own, {}, {}, NOW_HOUR)

        self.assertEqual((row["rx_bytes_per_sec"], row["tx_bytes_per_sec"]), (534 * MBIT, 548 * MBIT))
        self.assertEqual(
            (row["clients"]["rx_bytes_per_sec"], row["clients"]["tx_bytes_per_sec"]), (73 * MBIT, 494 * MBIT),
        )

    def test_drop_is_judged_on_clients_not_on_the_whole_traffic(self):
        # Клиенты ушли с заблокированного адреса, а сервер продолжает ходить с него
        # в интернет за клиентов нового: весь трафик ровный, падение — только у клиентов
        client_history = {ADDRESS: day_of(lambda ago: 0.0 if ago < 3 else 10 * MBIT)}
        own_history = {ADDRESS: day_of(lambda ago: 30 * MBIT)}

        [row] = build_ip_rows({}, {}, client_history, own_history, NOW_HOUR)

        self.assertEqual(row["clients"]["drop"], {"ratio": None})
        self.assertNotIn("drop", row)
        self.assertEqual(row["sparkline"][-1], 30 * MBIT)

    def test_whole_traffic_sums_bytes_but_not_coverage(self):
        # Оба ряда сняты одними замерами: сложенное покрытие вдвое занизило бы скорость
        client_history = {ADDRESS: steady({0: 10 * MBIT})}
        own_history = {ADDRESS: steady({0: 30 * MBIT})}

        [row] = build_ip_rows({}, {}, client_history, own_history, NOW_HOUR)

        self.assertEqual(row["sparkline"][-1], 40 * MBIT)
        self.assertEqual(row["clients"]["sparkline"][-1], 10 * MBIT)

    def test_address_without_clients_is_still_listed(self):
        # Адрес из пула исходящих: клиентов на нём нет, но трафик сервера с него идёт
        [row] = build_ip_rows({}, {ADDRESS: ObservedSpeeds(MBIT, MBIT)}, {}, {}, NOW_HOUR)

        self.assertEqual(row["rx_bytes_per_sec"], MBIT)
        self.assertIsNone(row["clients"]["rx_bytes_per_sec"])
        self.assertIsNone(row["clients"]["drop"])


if __name__ == "__main__":
    unittest.main()
