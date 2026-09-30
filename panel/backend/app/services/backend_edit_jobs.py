"""Задания правки бэкендов со страницы «Потери».

Замена или удаление адресов — это правка профилей, а потом раскатка каждого
изменённого профиля на его ноды, которая на большом парке идёт минутами. Задание
делает оба шага по очереди и отдаёт своё состояние полоске прогресса страницы:
что сейчас раскатывается и сколько нод уже получили конфиг. Прогресс раскатки
считается по статусам нод в базе, а не рисуется. Каждый профиль раскатывается
один раз, сколько бы адресов в нём ни поменялось.

Задания живут в памяти единственного процесса бэкенда; завершённые хранятся
час — полоска подхватывает их и после перезагрузки страницы.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

from app.database import async_session_maker
from app.services.backend_address import (
    AddressEdit,
    BatchPlan,
    Outcome,
    ProfileChange,
    ProfileRef,
    plan_batch,
    sync_profile,
)

logger = logging.getLogger(__name__)

JOB_TTL_SEC = 3600
MAX_JOBS = 20


class JobStage(str, Enum):
    EDITING = "editing"
    ROLLOUT = "rollout"
    DONE = "done"
    FAILED = "failed"


def _item_summary(changes: list[ProfileChange]) -> dict:
    outcomes = [rule.outcome for change in changes for rule in change.rules]
    return {
        "changed": sum(outcome is not Outcome.SKIPPED for outcome in outcomes),
        "skipped": sum(outcome is Outcome.SKIPPED for outcome in outcomes),
    }


@dataclass
class EditJob:
    id: str
    edits: list[AddressEdit]
    created_at: float
    stage: JobStage = JobStage.EDITING
    items: list[dict] = field(default_factory=list)
    changed: list[ProfileRef] = field(default_factory=list)
    current: Optional[str] = None
    failures: list[str] = field(default_factory=list)
    error: Optional[str] = None
    finished_at: Optional[float] = None

    @property
    def running(self) -> bool:
        return self.stage in (JobStage.EDITING, JobStage.ROLLOUT)

    def to_dict(self, progress: list[dict]) -> dict:
        return {
            "id": self.id,
            "stage": self.stage.value,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "current": self.current,
            "error": self.error,
            "failures": self.failures,
            "edits": [
                {"ip": e.ip, "port": e.port, "all_ports": e.all_ports, "action": e.action.value,
                 "new_ip": e.new_ip, "new_port": e.new_port}
                for e in self.edits
            ],
            "items": self.items,
            "profiles": progress,
        }


class EditJobManager:
    def __init__(
        self,
        session_factory=async_session_maker,
        planner: Callable[..., Awaitable[BatchPlan]] = plan_batch,
        syncer: Callable[[ProfileRef], Awaitable[None]] = sync_profile,
        clock: Callable[[], float] = time.time,
    ):
        self._session_factory = session_factory
        self._planner = planner
        self._syncer = syncer
        self._clock = clock
        self._jobs: dict[str, EditJob] = {}

    def create(self, edits: list[AddressEdit]) -> EditJob:
        self._prune()
        job = EditJob(id=uuid.uuid4().hex, edits=edits, created_at=self._clock())
        self._jobs[job.id] = job
        return job

    def recent(self) -> list[EditJob]:
        self._prune()
        return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)

    def _prune(self) -> None:
        now = self._clock()
        finished = sorted(
            (job for job in self._jobs.values() if not job.running),
            key=lambda job: job.created_at, reverse=True,
        )
        for index, job in enumerate(finished):
            if index >= MAX_JOBS or now - (job.finished_at or job.created_at) > JOB_TTL_SEC:
                del self._jobs[job.id]

    async def run(self, job: EditJob) -> None:
        try:
            async with self._session_factory() as db:
                plan = await self._planner(db, job.edits, save=True)
                await db.commit()
            job.items = [_item_summary(changes) for changes in plan.items]
            job.changed = plan.changed
            job.stage = JobStage.ROLLOUT
            for ref in plan.changed:
                job.current = ref.profile_name
                try:
                    await self._syncer(ref)
                except Exception as exc:
                    job.failures.append(ref.profile_name)
                    logger.error("backend_edit_rollout_failed job=%s kind=%s profile_id=%s error=%s",
                                 job.id, ref.kind, ref.profile_id, exc)
            job.stage = JobStage.DONE
            logger.info("backend_edit_job_done job=%s edits=%s profiles=%s failures=%s",
                        job.id, len(job.edits), len(job.changed), len(job.failures))
        except Exception as exc:
            job.stage = JobStage.FAILED
            job.error = str(exc)
            logger.error("backend_edit_job_failed job=%s error=%s", job.id, exc, exc_info=True)
        finally:
            job.current = None
            job.finished_at = self._clock()


_manager: Optional[EditJobManager] = None


def get_edit_jobs() -> EditJobManager:
    global _manager
    if _manager is None:
        _manager = EditJobManager()
    return _manager
