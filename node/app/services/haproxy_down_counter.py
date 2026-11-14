"""Сколько раз сервер HAProxy падал за последние полчаса.

Потери до адреса — среднее по окну в две минуты: сервер, который раз в пять
минут пропадает на двадцать секунд, по ним выглядит почти здоровым, а клиентов
при каждом падении выкидывает. HAProxy сам считает переходы UP→DOWN в колонке
`chkdown` — здесь прирост этого счётчика раскладывается по времени.

Адрес бывает в нескольких строках (несколько правил, копии под выходные IP):
один обрыв сети роняет их все, поэтому у адреса берётся строка с наибольшим
числом падений, а не сумма.
"""

import threading
import time
from collections import deque
from typing import Callable, Iterable, Optional

DOWNS_WINDOW_SEC = 1800
# Собственный перевод строки в обслуживание HAProxy может посчитать падением.
# Срок длиннее паузы между чтениями show stat (10 с) и его кэша (2 с): первое
# чтение после команды бывает ещё старым
SKIP_VALID_SEC = 15

RowKey = tuple[str, str]
Target = tuple[str, int]


class DownCounter:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._last: dict[RowKey, int] = {}
        self._events: dict[RowKey, deque[float]] = {}
        self._targets: dict[RowKey, Target] = {}
        self._skip: dict[RowKey, float] = {}
        # Наблюдения идут из тред-пула проверки потерь, чтение — из сборщика метрик
        self._lock = threading.Lock()

    def observe(self, rows: Iterable[tuple[RowKey, Target, Optional[int]]]) -> None:
        with self._lock:
            now = self._clock()
            self._skip = {row: deadline for row, deadline in self._skip.items() if deadline >= now}
            seen = set()
            for row, target, chkdown in rows:
                if chkdown is None:
                    continue
                seen.add(row)
                if self._targets.get(row) != target:
                    # Имя сервера переехало на другой адрес — история осталась у прежнего
                    self._targets[row] = target
                    self._events.pop(row, None)
                previous = self._last.get(row)
                self._last[row] = chkdown
                # Первое чтение — точка отсчёта; меньше прежнего — счётчик обнулил reload
                if previous is None or chkdown <= previous:
                    continue
                grown = chkdown - previous
                if self._skip.pop(row, None) is not None:
                    grown -= 1
                self._events.setdefault(row, deque()).extend([now] * grown)
            self._expire(now)
            self._forget_removed(seen)

    def downs(self, target: Target) -> int:
        with self._lock:
            edge = self._clock() - DOWNS_WINDOW_SEC
            return max(
                (sum(1 for moment in events if moment > edge)
                 for row, events in self._events.items() if self._targets.get(row) == target),
                default=0,
            )

    def reset(self, target: Target) -> None:
        """Адрес вернули из отключения: падения до него уже учтены решением панели."""
        with self._lock:
            for row, row_target in self._targets.items():
                if row_target == target:
                    self._events.pop(row, None)

    def skip_next(self, row: RowKey) -> None:
        with self._lock:
            self._skip[row] = self._clock() + SKIP_VALID_SEC

    def _expire(self, now: float) -> None:
        edge = now - DOWNS_WINDOW_SEC
        for row in list(self._events):
            events = self._events[row]
            while events and events[0] <= edge:
                events.popleft()
            if not events:
                del self._events[row]

    def _forget_removed(self, seen: set[RowKey]) -> None:
        """Строка ушла из конфига: её счётчик при возвращении начнётся заново.
        Пока у строки есть падения в окне, её адрес ещё нужен для downs()."""
        for row in [row for row in self._last if row not in seen and row not in self._events]:
            del self._last[row]
            self._targets.pop(row, None)


_counter: Optional[DownCounter] = None


def get_down_counter() -> DownCounter:
    global _counter
    if _counter is None:
        _counter = DownCounter()
    return _counter
