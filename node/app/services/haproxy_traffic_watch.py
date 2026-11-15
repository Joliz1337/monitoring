"""Идут ли от сервера HAProxy ответы клиентам.

Сервер, заблокированный на пути, нередко принимает подключения: рукопожатие TCP
проходит, проверка HAProxy зелёная, потерь почти нет — а данные обратно не
идут. В `show stat` у такого сервера новые сессии и присланное клиентами растут,
а отданное стоит на нуле. Здесь прирост этих счётчиков считается за окно.

Признак намеренно узкий — «ответов нет совсем»: частичные проблемы видны по
потерям и падениям. Условий на ответы два, потому что по одному каждое
ошибается на живом сервере: заливка большого файла даёт долю ответов меньше
процента, а протокол с короткими репликами — десятки байт ответа на сессию.

Адрес бывает в нескольких строках (несколько правил, копии под выходные IP):
трафик адреса — сумма по его строкам.
"""

import threading
import time
from collections import deque
from typing import Callable, Iterable, NamedTuple, Optional

from app.services.haproxy_down_counter import RowKey, Target

WINDOW_SEC = 180
# Меньше этого за окно — судить не по чему
MIN_SESSIONS = 20
MIN_CLIENT_BYTES = 100 * 1024
STALLED_REPLY_SHARE = 0.05
STALLED_REPLY_BYTES_PER_SESSION = 50
# show stat читается раз в 10 с; не читается (HAProxy остановлен) — вердикт устарел
VERDICT_VALID_SEC = 30


class Counters(NamedTuple):
    """Накопительные счётчики строки сервера: stot, bin, bout."""
    sessions: int
    client_bytes: int
    server_bytes: int


class TrafficWatch:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._samples: dict[RowKey, deque[tuple[float, Counters]]] = {}
        self._targets: dict[RowKey, Target] = {}
        self._verdicts: dict[Target, bool] = {}
        self._observed_at = float("-inf")
        # Наблюдения идут из тред-пула проверки потерь, чтение — из сборщика метрик
        self._lock = threading.Lock()

    def observe(self, rows: Iterable[tuple[RowKey, Target, Counters]]) -> None:
        with self._lock:
            now = self._clock()
            edge = now - WINDOW_SEC
            seen: dict[RowKey, Target] = {}
            samples_by_row: dict[RowKey, deque[tuple[float, Counters]]] = {}
            for row, target, counters in rows:
                samples = self._samples.get(row)
                # Имя сервера переехало на другой адрес либо счётчик пошёл назад
                # (reload обнулил) — прежний прирост к этой строке не относится
                if samples is None or self._targets.get(row) != target or _went_back(counters, samples[-1][1]):
                    samples = deque()
                samples.append((now, counters))
                while samples[0][0] < edge:
                    samples.popleft()
                seen[row] = target
                samples_by_row[row] = samples
            # Строка, ушедшая из конфига, при возвращении начнёт с новой точки отсчёта
            self._samples, self._targets = samples_by_row, seen
            self._verdicts = self._judge()
            self._observed_at = now

    def stalled(self, target: Target) -> Optional[bool]:
        """True — клиенты шлют, сервер не отвечает; None — судить не по чему."""
        with self._lock:
            if self._clock() - self._observed_at > VERDICT_VALID_SEC:
                return None
            return self._verdicts.get(target)

    def _judge(self) -> dict[Target, bool]:
        growth: dict[Target, Counters] = {}
        for row, samples in self._samples.items():
            target = self._targets[row]
            first, last = samples[0][1], samples[-1][1]
            total = growth.get(target, Counters(0, 0, 0))
            growth[target] = Counters(*(have + new - old for have, new, old in zip(total, last, first)))
        return {
            target: (
                traffic.server_bytes < traffic.client_bytes * STALLED_REPLY_SHARE
                and traffic.server_bytes < traffic.sessions * STALLED_REPLY_BYTES_PER_SESSION
            )
            for target, traffic in growth.items()
            if traffic.sessions >= MIN_SESSIONS and traffic.client_bytes >= MIN_CLIENT_BYTES
        }


def _went_back(current: Counters, previous: Counters) -> bool:
    return any(now < before for now, before in zip(current, previous))


_watch: Optional[TrafficWatch] = None


def get_traffic_watch() -> TrafficWatch:
    global _watch
    if _watch is None:
        _watch = TrafficWatch()
    return _watch
