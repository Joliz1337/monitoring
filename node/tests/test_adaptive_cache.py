"""AdaptiveTtlCache: дорогой расчёт один на всех, а результат живёт тем дольше, чем дороже он обошёлся.

Разбор /proc/net/tcp на ноде с сотнями тысяч сокетов — доли секунды чистого
Python на каждый опрос панели, а пересекающиеся опросы запускали его ещё и
параллельно. Тихой ноде при этом нужна прежняя свежесть в 5 секунд.

Запуск без psutil и без Linux, из node/:  python -m unittest discover -s tests -p "test_*.py"
"""

import itertools
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.adaptive_cache import AdaptiveTtlCache  # noqa: E402

# Предел ожидания потока: зависший вызов валит тест, а не подвешивает прогон
WAIT_SEC = 5.0
# Сколько даётся второму потоку, чтобы дойти до кэша, прежде чем разбор отпустят
SETTLE_SEC = 0.1


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class FakeParse:
    """Разбор, который длится заданное время по подставным часам; отдаёт свой порядковый номер."""

    def __init__(self, clock: FakeClock, seconds: float):
        self._clock = clock
        self.seconds = seconds
        self.runs = 0

    def __call__(self) -> int:
        self._clock.now += self.seconds
        self.runs += 1
        return self.runs


class LifetimeTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()

    def cache_of(self, parse_seconds: float) -> AdaptiveTtlCache:
        return AdaptiveTtlCache(FakeParse(self.clock, parse_seconds), clock=self.clock)

    def test_cheap_parse_is_repeated_after_five_seconds(self):
        cache = self.cache_of(parse_seconds=0.125)
        self.assertEqual(cache.get(), 1)

        self.clock.now += 4.75
        self.assertEqual(cache.get(), 1)

        self.clock.now += 0.25
        self.assertEqual(cache.get(), 2)

    def test_one_second_parse_is_not_repeated_for_twenty_seconds(self):
        cache = self.cache_of(parse_seconds=1.0)
        self.assertEqual(cache.get(), 1)

        self.clock.now += 19.5
        self.assertEqual(cache.get(), 1)

        self.clock.now += 0.5
        self.assertEqual(cache.get(), 2)

    def test_lifetime_of_a_very_slow_parse_is_capped_at_a_minute(self):
        cache = self.cache_of(parse_seconds=10.0)
        self.assertEqual(cache.get(), 1)

        self.clock.now += 59.5
        self.assertEqual(cache.get(), 1)

        self.clock.now += 0.5
        self.assertEqual(cache.get(), 2)

    def test_lifetime_follows_the_latest_parse(self):
        # Сокетов стало меньше, разбор подешевел — свежесть возвращается к пяти секундам
        parse = FakeParse(self.clock, seconds=1.0)
        cache = AdaptiveTtlCache(parse, clock=self.clock)
        cache.get()
        parse.seconds = 0.125
        self.clock.now += 20.0
        self.assertEqual(cache.get(), 2)

        self.clock.now += 5.0

        self.assertEqual(cache.get(), 3)


class BlockingParse:
    """Разбор, который стоит, пока тест его не отпустит; отдаёт свой порядковый номер."""

    def __init__(self):
        self._numbers = itertools.count(1)
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self) -> int:
        number = next(self._numbers)
        self.started.set()
        self.release.wait(WAIT_SEC)
        return number


class SingleFlightTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        self.parse = BlockingParse()
        self.cache = AdaptiveTtlCache(self.parse, clock=self.clock)
        # Упавший тест не должен оставить потоки висеть на разборе
        self.addCleanup(self.parse.release.set)

    def get_in_thread(self) -> tuple[threading.Thread, list[int]]:
        received: list[int] = []
        thread = threading.Thread(target=lambda: received.append(self.cache.get()), daemon=True)
        thread.start()
        return thread, received

    def test_concurrent_first_calls_share_one_parse(self):
        first, first_received = self.get_in_thread()
        self.assertTrue(self.parse.started.wait(WAIT_SEC))

        second, second_received = self.get_in_thread()
        # Отдать второму пока нечего: он ждёт идущий разбор, а не запускает свой
        second.join(SETTLE_SEC)
        self.assertTrue(second.is_alive())

        self.parse.release.set()
        first.join(WAIT_SEC)
        second.join(WAIT_SEC)
        self.assertEqual(first_received + second_received, [1, 1])

    def test_stale_value_is_served_while_a_refresh_is_running(self):
        self.parse.release.set()
        self.assertEqual(self.cache.get(), 1)
        self.parse.release.clear()
        self.parse.started.clear()
        self.clock.now += 5.0

        refresher, refreshed = self.get_in_thread()
        self.assertTrue(self.parse.started.wait(WAIT_SEC))
        # Опрос панели не ждёт чужой разбор и не запускает третий: берёт прошлый срез
        self.assertEqual(self.cache.get(), 1)

        self.parse.release.set()
        refresher.join(WAIT_SEC)
        self.assertEqual(refreshed, [2])
        self.assertEqual(self.cache.get(), 2)


class FailedParseTests(unittest.TestCase):

    def test_failed_refresh_is_reported_and_retried_by_the_next_call(self):
        clock = FakeClock()
        outcomes = iter(["first table", OSError("/proc/net/tcp is unreadable"), "third table"])

        def parse() -> str:
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        cache = AdaptiveTtlCache(parse, clock=clock)
        self.assertEqual(cache.get(), "first table")
        clock.now += 5.0

        with self.assertRaises(OSError):
            cache.get()

        # Ошибка не запоминается, и несостоявшийся разбор не считается идущим:
        # иначе прошлый срез отдавался бы как свежий до перезапуска агента
        self.assertEqual(cache.get(), "third table")


if __name__ == "__main__":
    unittest.main()
