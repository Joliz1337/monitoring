"""Кэш дорогого расчёта: срок жизни по цене расчёта и один расчёт на всех.

Постоянный срок жизни годится, пока расчёт дёшев. Разбор /proc/net/tcp на ноде
с сотнями тысяч сокетов — доли секунды чистого Python, и при сроке в 5 с он шёл
бы на каждый опрос панели. Здесь результат живёт в TTL_COST_MULTIPLIER раз
дольше, чем считался: расчёт не отнимает больше ~5% одного ядра, пока срок не
упрётся в MAX_TTL_SEC. Результат дешёвого расчёта живёт MIN_TTL_SEC — тихая
нода остаётся такой же свежей, как при постоянном сроке.

Опросы приходят из тред-пула и пересекаются. Второй расчёт поверх идущего не
запускается: вызов получает прошлое значение, пусть и просроченное, а если
значения ещё нет — ждёт идущий расчёт.
"""

import threading
import time
from dataclasses import dataclass
from typing import Callable, Generic, Optional, TypeVar

T = TypeVar("T")

MIN_TTL_SEC = 5.0
MAX_TTL_SEC = 60.0
TTL_COST_MULTIPLIER = 20


def ttl_for(compute_seconds: float) -> float:
    return min(max(compute_seconds * TTL_COST_MULTIPLIER, MIN_TTL_SEC), MAX_TTL_SEC)


@dataclass(frozen=True)
class _Entry(Generic[T]):
    value: T
    fresh_until: float


class AdaptiveTtlCache(Generic[T]):
    def __init__(self, compute: Callable[[], T], clock: Callable[[], float] = time.monotonic):
        self._compute = compute
        self._clock = clock
        self._entry: Optional[_Entry[T]] = None
        self._computing = False
        self._state = threading.Condition()

    def get(self) -> T:
        with self._state:
            # Ждёт только тот, кому отдать нечего: остальным прошлое значение
            # полезнее, чем ожидание чужого расчёта
            while self._computing and self._entry is None:
                self._state.wait()
            entry = self._entry
            if entry is not None and (self._computing or self._clock() < entry.fresh_until):
                return entry.value
            self._computing = True
        return self._compute_and_store()

    def _compute_and_store(self) -> T:
        fresh: Optional[_Entry[T]] = None
        try:
            started = self._clock()
            value = self._compute()
            finished = self._clock()
            # Срок идёт от конца расчёта: между двумя расчётами всегда есть пауза,
            # даже если сам расчёт оказался длиннее срока жизни
            fresh = _Entry(value, finished + ttl_for(finished - started))
            return value
        finally:
            # Флаг снимается и при ошибке расчёта: иначе ждущие зависли бы, а
            # прошлое значение отдавалось бы как обновляемое до перезапуска агента
            with self._state:
                if fresh is not None:
                    self._entry = fresh
                self._computing = False
                self._state.notify_all()
