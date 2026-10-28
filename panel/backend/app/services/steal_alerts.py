"""Steal — процессорное время, которое гипервизор хостера отдаёт другим машинам.

Изнутри ВМ его не убрать: можно только заметить и попросить хостера перенести
машину на другой физический сервер. Поэтому у алерта два события — steal
держится выше порога и steal ушёл (держится ниже порога столько же). Cooldown
повторов ведёт алертер — общий с остальными алертами и восстанавливаемый из
истории после рестарта.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class StealEvent(Enum):
    HIGH = "steal_high"
    RECOVERED = "steal_recovered"


@dataclass(frozen=True)
class StealPolicy:
    threshold: float
    sustained_sec: float


@dataclass
class StealWatch:
    """Состояние одного сервера.

    `alerted` — о высоком steal сообщили, а «в норме» ещё не было: только
    тогда затишье заканчивается сообщением.
    """

    high_since: Optional[float] = None
    calm_since: Optional[float] = None
    alerted: bool = False

    def evaluate(self, steal: float, now: float, policy: StealPolicy) -> Optional[StealEvent]:
        """HIGH — steal выше порога дольше удержания (на каждой проверке, пока держится);
        RECOVERED — после сообщённого эпизода steal ниже порога дольше удержания."""
        if steal >= policy.threshold:
            self.calm_since = None
            if self.high_since is None:
                self.high_since = now
            if now - self.high_since < policy.sustained_sec:
                return None
            return StealEvent.HIGH

        self.high_since = None
        if not self.alerted:
            return None
        if self.calm_since is None:
            self.calm_since = now
        if now - self.calm_since < policy.sustained_sec:
            return None
        self.alerted = False
        self.calm_since = None
        return StealEvent.RECOVERED

    def high_for(self, now: float) -> float:
        return 0.0 if self.high_since is None else now - self.high_since

    def reset(self) -> None:
        self.high_since = None
        self.calm_since = None
        self.alerted = False
