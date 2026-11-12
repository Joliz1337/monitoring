"""Эпизод «значение держится выше порога» с парным «вернулось в норму».

Общий для алертов, где важна длительность, а не скачок: steal на ноде,
нагрузка и диск хоста самой панели. Тревога — значение не ниже порога дольше
удержания, повторяется на каждой проверке, пока держится (реже её делает
cooldown алертера). «В норме» — только после сообщённой тревоги и только когда
значение продержалось ниже порога столько же: мигающее значение не шлёт пар
«есть / нет».
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ThresholdEvent(Enum):
    HIGH = "high"
    RECOVERED = "recovered"


@dataclass(frozen=True)
class ThresholdPolicy:
    threshold: float
    sustained_sec: float


@dataclass
class ThresholdWatch:
    """Состояние одного значения.

    `alerted` — о тревоге сообщили, а «в норме» ещё не было: только
    тогда затишье заканчивается сообщением.
    """

    high_since: Optional[float] = None
    calm_since: Optional[float] = None
    alerted: bool = False

    def evaluate(self, value: float, now: float, policy: ThresholdPolicy) -> Optional[ThresholdEvent]:
        if value >= policy.threshold:
            self.calm_since = None
            if self.high_since is None:
                self.high_since = now
            if now - self.high_since < policy.sustained_sec:
                return None
            return ThresholdEvent.HIGH

        self.high_since = None
        if not self.alerted:
            return None
        if self.calm_since is None:
            self.calm_since = now
        if now - self.calm_since < policy.sustained_sec:
            return None
        self.alerted = False
        self.calm_since = None
        return ThresholdEvent.RECOVERED

    def high_for(self, now: float) -> float:
        return 0.0 if self.high_since is None else now - self.high_since

    def reset(self) -> None:
        self.high_since = None
        self.calm_since = None
        self.alerted = False
