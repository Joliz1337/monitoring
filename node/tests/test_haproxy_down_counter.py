"""Tests for the HAProxy server down counter (services/haproxy_down_counter).

Runnable with plain stdlib:  python -m unittest discover -s node/tests

По числу падений панель выводит сервер из балансировки. Лишнее падение —
отключённый здоровый сервер, пропущенное — клиенты на сервере, который то и
дело отваливается.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.haproxy_down_counter import DOWNS_WINDOW_SEC, DownCounter  # noqa: E402

ROW = ("backend_tcp_lb", "srv1")
COPY = ("backend_tcp_lb", "srv1_o2")
OTHER_RULE = ("backend_tcp_other", "srv1")
TARGET = ("10.0.0.5", 443)
NEIGHBOUR_ROW = ("backend_tcp_lb", "srv2")
NEIGHBOUR = ("10.0.0.6", 443)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class DownCounterTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.counter = DownCounter(clock=self.clock)

    def see(self, chkdown, row=ROW, target=TARGET):
        self.counter.observe([(row, target, chkdown)])

    def test_first_sight_is_a_baseline_not_downs(self):
        self.see(7)
        self.assertEqual(self.counter.downs(TARGET), 0)

    def test_growth_counts_every_transition(self):
        self.see(2)
        self.see(4)
        self.assertEqual(self.counter.downs(TARGET), 2)

    def test_unchanged_counter_adds_nothing(self):
        self.see(2)
        self.see(3)
        self.see(3)
        self.assertEqual(self.counter.downs(TARGET), 1)

    def test_counter_reset_after_reload_keeps_history(self):
        self.see(5)
        self.see(6)
        self.see(1)
        self.assertEqual(self.counter.downs(TARGET), 1)
        self.see(2)
        self.assertEqual(self.counter.downs(TARGET), 2)

    def test_downs_older_than_window_expire(self):
        self.see(0)
        self.see(1)
        self.clock.now += DOWNS_WINDOW_SEC - 1
        self.assertEqual(self.counter.downs(TARGET), 1)
        self.clock.now += 2
        self.assertEqual(self.counter.downs(TARGET), 0)

    def test_window_is_thirty_minutes(self):
        self.see(0)
        self.see(1)
        self.clock.now += 29 * 60
        self.assertEqual(self.counter.downs(TARGET), 1)
        self.clock.now += 2 * 60
        self.assertEqual(self.counter.downs(TARGET), 0)

    def test_rows_of_one_address_give_max_not_sum(self):
        self.counter.observe([(ROW, TARGET, 0), (COPY, TARGET, 0), (OTHER_RULE, TARGET, 0)])
        self.counter.observe([(ROW, TARGET, 2), (COPY, TARGET, 3), (OTHER_RULE, TARGET, 1)])
        self.assertEqual(self.counter.downs(TARGET), 3)

    def test_addresses_are_counted_apart(self):
        self.counter.observe([(ROW, TARGET, 0), (NEIGHBOUR_ROW, NEIGHBOUR, 0)])
        self.counter.observe([(ROW, TARGET, 2), (NEIGHBOUR_ROW, NEIGHBOUR, 0)])
        self.assertEqual((self.counter.downs(TARGET), self.counter.downs(NEIGHBOUR)), (2, 0))

    def test_row_without_counter_is_ignored(self):
        self.see(None)
        self.see(None)
        self.assertEqual(self.counter.downs(TARGET), 0)

    def test_unknown_address_has_no_downs(self):
        self.assertEqual(self.counter.downs(("10.9.9.9", 443)), 0)

    def test_reset_forgets_history_but_not_the_baseline(self):
        self.see(0)
        self.see(3)
        self.counter.reset(TARGET)
        self.assertEqual(self.counter.downs(TARGET), 0)
        # База осталась на 3: прежние падения не всплывают как новые
        self.see(3)
        self.assertEqual(self.counter.downs(TARGET), 0)
        self.see(4)
        self.assertEqual(self.counter.downs(TARGET), 1)

    def test_reset_leaves_other_addresses(self):
        self.counter.observe([(ROW, TARGET, 0), (NEIGHBOUR_ROW, NEIGHBOUR, 0)])
        self.counter.observe([(ROW, TARGET, 1), (NEIGHBOUR_ROW, NEIGHBOUR, 2)])
        self.counter.reset(TARGET)
        self.assertEqual(self.counter.downs(NEIGHBOUR), 2)

    def test_skip_next_swallows_one_transition_of_that_row(self):
        self.counter.observe([(ROW, TARGET, 0), (COPY, TARGET, 0)])
        self.counter.skip_next(ROW)
        self.counter.observe([(ROW, TARGET, 1), (COPY, TARGET, 1)])
        # Строка, которую перевели в обслуживание сами, не считается; копия упала сама
        self.assertEqual(self.counter.downs(TARGET), 1)
        self.counter.reset(TARGET)
        self.counter.observe([(ROW, TARGET, 2), (COPY, TARGET, 1)])
        self.assertEqual(self.counter.downs(TARGET), 1)

    def test_skip_next_survives_a_stale_reading(self):
        # show stat кэшируется: первое чтение после команды может быть ещё старым
        self.see(0)
        self.counter.skip_next(ROW)
        self.see(0)
        self.clock.now += 10
        self.see(1)
        self.assertEqual(self.counter.downs(TARGET), 0)

    def test_skip_next_does_not_outlive_its_command(self):
        # HAProxy мог и не посчитать перевод в обслуживание падением
        self.see(0)
        self.counter.skip_next(ROW)
        self.clock.now += 60
        self.see(1)
        self.assertEqual(self.counter.downs(TARGET), 1)

    def test_vanished_row_keeps_address_history(self):
        self.counter.observe([(ROW, TARGET, 0), (NEIGHBOUR_ROW, NEIGHBOUR, 0)])
        self.counter.observe([(ROW, TARGET, 2), (NEIGHBOUR_ROW, NEIGHBOUR, 0)])
        self.counter.observe([(NEIGHBOUR_ROW, NEIGHBOUR, 0)])
        self.assertEqual(self.counter.downs(TARGET), 2)

    def test_row_back_in_config_starts_from_new_baseline(self):
        self.see(1)
        self.counter.observe([(NEIGHBOUR_ROW, NEIGHBOUR, 0)])
        self.see(4)
        self.assertEqual(self.counter.downs(TARGET), 0)

    def test_row_moved_to_another_address_starts_clean(self):
        self.see(0)
        self.see(2)
        self.see(2, target=NEIGHBOUR)
        self.assertEqual(self.counter.downs(NEIGHBOUR), 0)


if __name__ == "__main__":
    unittest.main()
