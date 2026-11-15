"""Tests for the "no replies come back" signal (services/haproxy_traffic_watch).

Runnable with plain stdlib:  python -m unittest discover -s node/tests

По этому признаку панель выводит сервер из балансировки даже при нулевых
потерях. Ложное «да» — отключённый здоровый сервер и оборванные клиенты,
ложное «нет» — клиенты на сервере, от которого не приходит ни байта.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.haproxy_traffic_watch import (  # noqa: E402
    MIN_CLIENT_BYTES,
    MIN_SESSIONS,
    VERDICT_VALID_SEC,
    WINDOW_SEC,
    Counters,
    TrafficWatch,
    counts_bytes_continuously,
)

ROW = ("backend_tcp_lb", "srv1")
COPY = ("backend_tcp_lb", "srv1_o2")
TARGET = ("10.0.0.5", 443)
NEIGHBOUR_ROW = ("backend_tcp_lb", "srv2")
NEIGHBOUR = ("10.0.0.6", 443)
READ_EVERY_SEC = 10
KB = 1024
ZERO = Counters(0, 0, 0)
# Полсотни клиентов прислали по 4 КБ и не получили ни байта
SILENT = Counters(sessions=50, client_bytes=200 * KB, server_bytes=0)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TrafficWatchTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.watch = TrafficWatch(clock=self.clock)

    def see(self, counters: Counters, row=ROW, target=TARGET):
        self.watch.observe([(row, target, counters)])

    def later(self, counters: Counters, row=ROW, target=TARGET):
        self.clock.now += READ_EVERY_SEC
        self.see(counters, row, target)

    def grown(self, counters: Counters):
        """Вердикт адреса, счётчики которого выросли от нуля до заданных."""
        self.see(ZERO)
        self.later(counters)
        return self.watch.stalled(TARGET)

    def test_first_sight_is_a_baseline_without_a_verdict(self):
        self.see(SILENT)
        self.assertIsNone(self.watch.stalled(TARGET))

    def test_sessions_without_replies_are_stalled(self):
        self.assertIs(self.grown(SILENT), True)

    def test_answering_server_is_not_stalled(self):
        self.assertIs(self.grown(SILENT._replace(server_bytes=2000 * KB)), False)

    def test_too_few_sessions_give_no_verdict(self):
        self.assertIsNone(self.grown(SILENT._replace(sessions=MIN_SESSIONS - 1)))

    def test_enough_sessions_give_a_verdict(self):
        self.assertIs(self.grown(SILENT._replace(sessions=MIN_SESSIONS)), True)

    def test_too_little_client_traffic_gives_no_verdict(self):
        self.assertIsNone(self.grown(SILENT._replace(client_bytes=MIN_CLIENT_BYTES - 1)))

    def test_enough_client_traffic_gives_a_verdict(self):
        self.assertIs(self.grown(SILENT._replace(client_bytes=MIN_CLIENT_BYTES)), True)

    def test_replies_under_five_percent_of_requests_are_stalled(self):
        busy = Counters(sessions=2000, client_bytes=1_000_000, server_bytes=49_999)
        self.assertIs(self.grown(busy), True)

    def test_replies_of_five_percent_are_not_stalled(self):
        busy = Counters(sessions=2000, client_bytes=1_000_000, server_bytes=50_000)
        self.assertIs(self.grown(busy), False)

    def test_big_upload_through_an_answering_server_is_not_stalled(self):
        # Один клиент заливает гигабайт: ответов меньше процента, но каждой
        # новой сессии сервер ответил — он жив
        upload = Counters(sessions=20, client_bytes=1024 * 1024 * KB, server_bytes=20 * 2 * KB)
        self.assertIs(self.grown(upload), False)

    def test_under_fifty_reply_bytes_per_session_is_stalled(self):
        upload = Counters(sessions=20, client_bytes=1024 * 1024 * KB, server_bytes=20 * 50 - 1)
        self.assertIs(self.grown(upload), True)

    def test_fifty_reply_bytes_per_session_is_not_stalled(self):
        upload = Counters(sessions=20, client_bytes=1024 * 1024 * KB, server_bytes=20 * 50)
        self.assertIs(self.grown(upload), False)

    def test_verdict_counts_growth_not_totals(self):
        # До окна сервер отдал гигабайты — это не делает его живым сейчас
        before = Counters(sessions=90_000, client_bytes=10**9, server_bytes=10**10)
        self.see(before)
        self.later(Counters(before.sessions + 50, before.client_bytes + 200 * KB, before.server_bytes))
        self.assertIs(self.watch.stalled(TARGET), True)

    def test_traffic_stays_in_the_window_for_three_minutes(self):
        self.see(ZERO)
        self.later(SILENT)
        for _ in range(WINDOW_SEC // READ_EVERY_SEC - 1):
            self.later(SILENT)
        self.assertIs(self.watch.stalled(TARGET), True)

    def test_traffic_older_than_the_window_is_forgotten(self):
        self.see(ZERO)
        self.later(SILENT)
        for _ in range(WINDOW_SEC // READ_EVERY_SEC):
            self.later(SILENT)
        # Сервер вывели из балансировки: новых сессий нет, судить не по чему
        self.assertIsNone(self.watch.stalled(TARGET))

    def test_recovered_server_stops_being_stalled(self):
        self.see(ZERO)
        self.later(SILENT)
        answered = SILENT
        for _ in range(WINDOW_SEC // READ_EVERY_SEC + 1):
            answered = Counters(answered.sessions + 5, answered.client_bytes + 20 * KB,
                                answered.server_bytes + 400 * KB)
            self.later(answered)
        self.assertIs(self.watch.stalled(TARGET), False)

    def test_counter_reset_restarts_the_window(self):
        self.see(Counters(sessions=5000, client_bytes=9000 * KB, server_bytes=90_000 * KB))
        # reload: счётчики начались заново; падение счётчика — не «минус трафик»
        self.later(Counters(sessions=30, client_bytes=150 * KB, server_bytes=0))
        self.assertIsNone(self.watch.stalled(TARGET))
        self.later(Counters(sessions=60, client_bytes=300 * KB, server_bytes=0))
        self.assertIs(self.watch.stalled(TARGET), True)

    def test_rows_of_one_address_are_summed(self):
        half = Counters(sessions=10, client_bytes=60 * KB, server_bytes=0)
        self.watch.observe([(ROW, TARGET, ZERO), (COPY, TARGET, ZERO)])
        self.clock.now += READ_EVERY_SEC
        self.watch.observe([(ROW, TARGET, half), (COPY, TARGET, half)])
        self.assertIs(self.watch.stalled(TARGET), True)

    def test_replies_on_any_row_make_the_address_alive(self):
        self.watch.observe([(ROW, TARGET, ZERO), (COPY, TARGET, ZERO)])
        self.clock.now += READ_EVERY_SEC
        self.watch.observe([(ROW, TARGET, SILENT), (COPY, TARGET, SILENT._replace(server_bytes=900 * KB))])
        self.assertIs(self.watch.stalled(TARGET), False)

    def test_addresses_are_judged_apart(self):
        self.watch.observe([(ROW, TARGET, ZERO), (NEIGHBOUR_ROW, NEIGHBOUR, ZERO)])
        self.clock.now += READ_EVERY_SEC
        self.watch.observe([
            (ROW, TARGET, SILENT),
            (NEIGHBOUR_ROW, NEIGHBOUR, SILENT._replace(server_bytes=900 * KB)),
        ])
        self.assertEqual((self.watch.stalled(TARGET), self.watch.stalled(NEIGHBOUR)), (True, False))

    def test_unknown_address_has_no_verdict(self):
        self.assertIsNone(self.watch.stalled(("10.9.9.9", 443)))

    def test_verdict_expires_when_haproxy_stops_answering(self):
        self.assertIs(self.grown(SILENT), True)
        self.clock.now += VERDICT_VALID_SEC
        self.assertIs(self.watch.stalled(TARGET), True)
        self.clock.now += 1
        self.assertIsNone(self.watch.stalled(TARGET))

    def test_row_moved_to_another_address_starts_clean(self):
        self.see(ZERO)
        self.later(SILENT, target=NEIGHBOUR)
        self.assertIsNone(self.watch.stalled(NEIGHBOUR))
        self.assertIsNone(self.watch.stalled(TARGET))

    def test_row_removed_from_config_takes_its_verdict_along(self):
        self.watch.observe([(ROW, TARGET, ZERO), (NEIGHBOUR_ROW, NEIGHBOUR, ZERO)])
        self.clock.now += READ_EVERY_SEC
        self.watch.observe([(ROW, TARGET, SILENT), (NEIGHBOUR_ROW, NEIGHBOUR, ZERO)])
        self.clock.now += READ_EVERY_SEC
        self.watch.observe([(NEIGHBOUR_ROW, NEIGHBOUR, ZERO)])
        self.assertIsNone(self.watch.stalled(TARGET))

    def test_row_back_in_config_starts_from_a_new_baseline(self):
        self.see(ZERO)
        self.clock.now += READ_EVERY_SEC
        self.watch.observe([(NEIGHBOUR_ROW, NEIGHBOUR, ZERO)])
        self.later(SILENT)
        self.assertIsNone(self.watch.stalled(TARGET))


class ContinuousCountersTests(unittest.TestCase):
    """Без `option contstats` HAProxy прибавляет байты сессии в show stat только при её
    закрытии: сервер с долгими живыми соединениями выглядел бы молчащим."""

    def test_option_in_defaults_counts(self):
        config = "global\n    maxconn 100\n\ndefaults\n    mode tcp\n    option contstats\n\nfrontend fe\n    bind :443\n"
        self.assertTrue(counts_bytes_continuously(config))

    def test_config_without_the_option_does_not(self):
        self.assertFalse(counts_bytes_continuously("defaults\n    mode tcp\n    option redispatch\n"))

    def test_empty_config_does_not(self):
        self.assertFalse(counts_bytes_continuously(""))

    def test_commented_option_does_not_count(self):
        self.assertFalse(counts_bytes_continuously("defaults\n    mode tcp\n    # option contstats\n"))

    def test_trailing_comment_is_ignored(self):
        self.assertTrue(counts_bytes_continuously("defaults\n    option contstats  # счётчики по ходу\n"))

    def test_option_in_one_frontend_is_not_enough(self):
        # Остальные входы считали бы по-старому, а вердикт — один на адрес
        config = "defaults\n    mode tcp\n\nfrontend fe\n    bind :443\n    option contstats\n"
        self.assertFalse(counts_bytes_continuously(config))

    def test_option_switched_off_anywhere_does_not_count(self):
        config = "defaults\n    option contstats\n\nlisten relay\n    bind :443\n    no option contstats\n"
        self.assertFalse(counts_bytes_continuously(config))


if __name__ == "__main__":
    unittest.main()
