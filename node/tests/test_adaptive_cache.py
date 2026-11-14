"""AdaptiveTtlCache: дорогой расчёт один на всех, живёт тем дольше, чем дороже обошёлся, а пересчитывается в фоне.

Разбор /proc/net/tcp на ноде с сотнями тысяч сокетов — доли секунды чистого
Python на каждый опрос панели, а на миллионе сокетов — секунды: опрос с прошлым
срезом на руках ждать их не должен. Тихой ноде при этом нужна прежняя свежесть
в 5 секунд.

Запуск без psutil и без Linux, из node/:  python -m unittest discover -s tests -p "test_*.py"
"""

import itertools
import os
import sys
import threading
import unittest
from typing import Callable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.adaptive_cache import AdaptiveTtlCache  # noqa: E402

# Предел ожидания потока: зависший вызов валит тест, а не подвешивает прогон
WAIT_SEC = 5.0
# Сколько даётся второму потоку, чтобы дойти до кэша, прежде чем разбор отпустят
SETTLE_SEC = 0.1


def run_at_once(job: Callable[[], None]) -> None:
    """Фоновый пересчёт без потока: к возврату из get() он уже отработал."""
    job()


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
    """Просроченный срез вызов всё равно получает — срок жизни виден по числу разборов."""

    def setUp(self):
        self.clock = FakeClock()

    def cached(self, parse_seconds: float) -> tuple[AdaptiveTtlCache, FakeParse]:
        parse = FakeParse(self.clock, parse_seconds)
        return AdaptiveTtlCache(parse, clock=self.clock, spawn=run_at_once), parse

    def test_cheap_parse_is_repeated_after_five_seconds(self):
        cache, parse = self.cached(parse_seconds=0.125)
        self.assertEqual(cache.get(), 1)

        self.clock.now += 4.75
        self.assertEqual((cache.get(), parse.runs), (1, 1))

        self.clock.now += 0.25
        self.assertEqual((cache.get(), parse.runs), (1, 2))
        self.assertEqual(cache.get(), 2)

    def test_one_second_parse_is_not_repeated_for_twenty_seconds(self):
        cache, parse = self.cached(parse_seconds=1.0)
        self.assertEqual(cache.get(), 1)

        self.clock.now += 19.5
        self.assertEqual((cache.get(), parse.runs), (1, 1))

        self.clock.now += 0.5
        self.assertEqual((cache.get(), parse.runs), (1, 2))
        self.assertEqual(cache.get(), 2)

    def test_lifetime_of_a_very_slow_parse_is_capped_at_a_minute(self):
        cache, parse = self.cached(parse_seconds=10.0)
        self.assertEqual(cache.get(), 1)

        self.clock.now += 59.5
        self.assertEqual((cache.get(), parse.runs), (1, 1))

        self.clock.now += 0.5
        self.assertEqual((cache.get(), parse.runs), (1, 2))
        self.assertEqual(cache.get(), 2)

    def test_lifetime_follows_the_latest_parse(self):
        # Сокетов стало меньше, разбор подешевел — свежесть возвращается к пяти секундам
        cache, parse = self.cached(parse_seconds=1.0)
        cache.get()
        parse.seconds = 0.125
        self.clock.now += 20.0
        cache.get()
        self.assertEqual(parse.runs, 2)

        self.clock.now += 4.75
        self.assertEqual((cache.get(), parse.runs), (2, 2))

        self.clock.now += 0.25
        cache.get()
        self.assertEqual(parse.runs, 3)


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
        self.refreshes: list[threading.Thread] = []
        self.cache = AdaptiveTtlCache(self.parse, clock=self.clock, spawn=self.refresh_in_thread)
        # Упавший тест не должен оставить потоки висеть на разборе
        self.addCleanup(self.parse.release.set)

    def refresh_in_thread(self, job: Callable[[], None]) -> None:
        thread = threading.Thread(target=job, daemon=True)
        self.refreshes.append(thread)
        thread.start()

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
        # Первый вызов считает сам: фоновому пересчёту нечего было бы отдать ждущим
        self.assertEqual(self.refreshes, [])

    def test_expired_value_is_returned_at_once_while_the_parse_runs_in_the_background(self):
        self.parse.release.set()
        self.assertEqual(self.cache.get(), 1)
        self.parse.release.clear()
        self.parse.started.clear()
        self.clock.now += 5.0

        # Разбор стоит до release — вызов, считай он сам, простоял бы вместе с ним
        self.assertEqual(self.cache.get(), 1)
        self.assertTrue(self.parse.started.wait(WAIT_SEC))
        # Следующий опрос не запускает второй разбор поверх идущего
        self.assertEqual(self.cache.get(), 1)
        self.assertEqual(len(self.refreshes), 1)

        self.parse.release.set()
        self.refreshes[0].join(WAIT_SEC)
        self.assertEqual(self.cache.get(), 2)


class FailedParseTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()

    def cache_of(self, *outcomes: object, spawn: Callable = run_at_once) -> AdaptiveTtlCache:
        remaining = iter(outcomes)

        def parse() -> object:
            outcome = next(remaining)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return AdaptiveTtlCache(parse, clock=self.clock, spawn=spawn)

    def test_failed_first_parse_is_raised_and_retried_by_the_next_call(self):
        cache = self.cache_of(OSError("/proc/net/tcp is unreadable"), "second table")

        with self.assertRaises(OSError):
            cache.get()

        self.assertEqual(cache.get(), "second table")

    def test_failed_refresh_keeps_the_previous_value_and_is_retried_by_the_next_call(self):
        cache = self.cache_of("first table", OSError("/proc/net/tcp is unreadable"), "third table")
        self.assertEqual(cache.get(), "first table")
        self.clock.now += 5.0

        # Ошибку фонового разбора отдать некому — она уходит в журнал
        with self.assertLogs("app.services.adaptive_cache", level="ERROR"):
            self.assertEqual(cache.get(), "first table")

        # Несостоявшийся разбор не считается идущим: иначе прошлый срез
        # отдавался бы как обновляемый до перезапуска агента
        self.assertEqual(cache.get(), "first table")
        self.assertEqual(cache.get(), "third table")

    def test_refresh_that_could_not_start_is_retried_by_the_next_call(self):
        attempts: list[Callable[[], None]] = []

        def spawn(job: Callable[[], None]) -> None:
            attempts.append(job)
            if len(attempts) == 1:
                raise RuntimeError("can't start new thread")
            job()

        cache = self.cache_of("first table", "second table", spawn=spawn)
        self.assertEqual(cache.get(), "first table")
        self.clock.now += 5.0

        with self.assertLogs("app.services.adaptive_cache", level="ERROR"):
            self.assertEqual(cache.get(), "first table")

        self.assertEqual(cache.get(), "first table")
        self.assertEqual(cache.get(), "second table")


if __name__ == "__main__":
    unittest.main()
