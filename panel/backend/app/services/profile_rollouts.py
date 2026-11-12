"""Раскатка профиля на привязанные серверы как фоновое задание.

«Синхронизировать всё» обходит серверы профиля с ограниченной параллельностью и на
сотнях нод идёт минутами — дольше, чем живёт HTTP-запрос: браузер ждёт 30 секунд,
nginx панели 60. Задание к соединению не привязано: кнопка сразу получает его
состояние, а страница спрашивает ход раскатки отдельным запросом.

У профиля одна раскатка за раз: вторая поверх первой слала бы тем же серверам тот
же конфиг ещё раз. Задания живут в памяти единственного процесса бэкенда — его
перезапуск теряет их, а недообработанные серверы остаются в статусе «pending».
"""

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

# Столько завершённая раскатка видна странице: подхватить итог после перезагрузки вкладки
FINISHED_TTL_SECONDS = 600
INTERRUPTED_MESSAGE = "Раскатка прервана остановкой панели"


class RolloutStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RolloutInProgressError(Exception):
    """У профиля уже идёт раскатка."""


class RolloutProgress(Protocol):
    def expect(self, total: int) -> None: ...

    def record(self, result: Any) -> None: ...


class _NobodyWatches:
    def expect(self, total: int) -> None:
        pass

    def record(self, result: Any) -> None:
        pass


# Раскатка, за ходом которой не следят: фоновая после правки профиля, повтор из очереди
NO_PROGRESS: RolloutProgress = _NobodyWatches()

RolloutRunner = Callable[[RolloutProgress], Awaitable[None]]


@dataclass
class Rollout:
    id: str
    kind: str
    profile_id: int
    started_at: float
    status: RolloutStatus = RolloutStatus.RUNNING
    total: Optional[int] = None
    results: list[dict] = field(default_factory=list)
    error: Optional[str] = None
    finished_at: Optional[float] = None
    task: Optional[asyncio.Task] = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.status is RolloutStatus.RUNNING

    def expect(self, total: int) -> None:
        self.total = total

    def record(self, result: Any) -> None:
        self.results.append(asdict(result))

    def to_dict(self) -> dict:
        state = {
            "job_id": self.id,
            "status": self.status.value,
            "total": self.total,
            "done": len(self.results),
            "error": self.error,
        }
        # Итоги по серверам отдаются один раз, в конце: на 2000 серверов это сотни
        # килобайт, и слать их на каждый опрос хода незачем
        if not self.running:
            state["results"] = self.results
        return state


class RolloutRegistry:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._rollouts: dict[tuple[str, int], Rollout] = {}

    def start(self, kind: str, profile_id: int, runner: RolloutRunner) -> Rollout:
        self._forget_expired()
        key = (kind, profile_id)
        current = self._rollouts.get(key)
        if current is not None and current.running:
            raise RolloutInProgressError(f"{kind} profile {profile_id}")

        rollout = Rollout(id=uuid.uuid4().hex, kind=kind, profile_id=profile_id, started_at=self._clock())
        self._rollouts[key] = rollout
        rollout.task = asyncio.create_task(self._run(rollout, runner))
        return rollout

    def latest(self, kind: str, profile_id: int) -> Optional[Rollout]:
        self._forget_expired()
        return self._rollouts.get((kind, profile_id))

    def _forget_expired(self) -> None:
        now = self._clock()
        expired = [
            key for key, rollout in self._rollouts.items()
            if rollout.finished_at is not None and now - rollout.finished_at > FINISHED_TTL_SECONDS
        ]
        for key in expired:
            del self._rollouts[key]

    async def _run(self, rollout: Rollout, runner: RolloutRunner) -> None:
        try:
            await runner(rollout)
        except Exception as exc:
            rollout.status = RolloutStatus.FAILED
            rollout.error = str(exc) or type(exc).__name__
            logger.exception(
                "profile_rollout_failed kind=%s profile_id=%s", rollout.kind, rollout.profile_id,
            )
        else:
            rollout.status = RolloutStatus.COMPLETED
        finally:
            # Отменённая задача сюда приходит ещё «идущей»: без этого профиль остался бы
            # занятым навсегда и новую раскатку было бы не запустить
            if rollout.running:
                rollout.status = RolloutStatus.FAILED
                rollout.error = INTERRUPTED_MESSAGE
            if rollout.total is None:
                rollout.total = len(rollout.results)
            rollout.finished_at = self._clock()
            logger.info(
                "profile_rollout_finished kind=%s profile_id=%s status=%s servers=%s seconds=%.0f",
                rollout.kind, rollout.profile_id, rollout.status.value, len(rollout.results),
                rollout.finished_at - rollout.started_at,
            )


_registry = RolloutRegistry()


def get_rollout_registry() -> RolloutRegistry:
    return _registry
