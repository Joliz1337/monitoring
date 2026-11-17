"""Проверка SSH-доступа: панель пробует войти на серверы заранее.

Без неё о неверном пароле узнают, когда нода уже не обновилась через агента и
запасной путь по SSH не сработал. Проверка только входит и выходит — команд на
сервере не выполняет. Неверный пароль даёт ровно одну неудачную попытку: на
нодах стоит fail2ban, и панель не должна попасть под его бан.

Идёт фоновой задачей в памяти, одна на панель: закрытая вкладка её не
прерывает, результат живёт CHECK_RESULT_TTL_SECONDS.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Union

import asyncssh

from app.services.ssh_target import (
    HostKeyChangedError,
    SSHTarget,
    SSHTargetError,
    connect_error_text,
    open_ssh_routes,
    ssh_connect_kwargs,
)

logger = logging.getLogger(__name__)

# Проверка — это только рукопожатие и вход, поэтому подключений больше, чем у
# доставки образа, а ждать молчащий сервер дольше CHECK_CONNECT_TIMEOUT незачем
CHECK_CONCURRENCY = 30
CHECK_CONNECT_TIMEOUT = 15
CHECK_RESULT_TTL_SECONDS = 600
# Сколько неуспешных серверов отдаётся списком; счётчики — по всем
FAILED_RESULTS_LIMIT = 500


class CheckStatus(str, Enum):
    OK = "ok"
    AUTH_FAILED = "auth_failed"
    UNREACHABLE = "unreachable"
    HOST_KEY_CHANGED = "host_key_changed"
    BAD_KEY = "bad_key"
    # Доступ не собрался ещё до подключения — причины SSHTargetError
    NO_CREDS = "no_creds"
    NOT_ROOT = "not_root"
    NO_HOST = "no_host"


class JobStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class CheckOutcome:
    status: CheckStatus
    detail: Optional[str] = None  # причина для неуспешных
    via_proxy: bool = False  # основной путь не подключился, вход удался запасным — через SSH-прокси


# id сервера, имя и его SSH-цель — либо отказ, если доступ не собрался
CheckSubject = tuple[int, str, Union[SSHTarget, SSHTargetError]]


class CheckAlreadyRunning(Exception):
    """Проверка одна на панель: вторая удвоила бы подключения к тем же серверам."""


async def check_target(target: SSHTarget) -> CheckOutcome:
    """Войти на сервер и выйти."""
    try:
        connect_kwargs = {**ssh_connect_kwargs(target), "connect_timeout": CHECK_CONNECT_TIMEOUT}
    except (asyncssh.KeyImportError, ValueError) as exc:
        return CheckOutcome(CheckStatus.BAD_KEY, str(exc) or None)

    try:
        opened = await open_ssh_routes(connect_kwargs, target.routes, target.server_id)
    except asyncssh.PermissionDenied:
        return CheckOutcome(CheckStatus.AUTH_FAILED)
    except HostKeyChangedError:
        return CheckOutcome(CheckStatus.HOST_KEY_CHANGED)
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
        return CheckOutcome(CheckStatus.UNREACHABLE, connect_error_text(connect_kwargs, exc))

    opened.conn.close()
    await opened.conn.wait_closed()
    # Прокси в режиме «всегда» и прокси из карточки сервера — путь по замыслу, а не запасной
    return CheckOutcome(CheckStatus.OK, via_proxy=bool(opened.skipped))


@dataclass
class _CheckJob:
    id: str
    total: int
    status: JobStatus = JobStatus.RUNNING
    done: int = 0
    counts: Counter = field(default_factory=Counter)
    ok_via_proxy: int = 0
    failed: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    task: Optional[asyncio.Task] = None


class SshAccessCheckManager:
    """Последняя проверка доступа и её ход."""

    def __init__(self) -> None:
        self._job: Optional[_CheckJob] = None

    @property
    def is_running(self) -> bool:
        return self._job is not None and self._job.status is JobStatus.RUNNING

    def start(self, subjects: list[CheckSubject]) -> str:
        if self.is_running:
            raise CheckAlreadyRunning()
        job = _CheckJob(id=uuid.uuid4().hex, total=len(subjects))
        self._job = job
        if subjects:
            job.task = asyncio.create_task(self._run(job, subjects))
        else:
            self._finish(job, JobStatus.DONE)
        return job.id

    def cancel(self) -> bool:
        job = self._job
        if job is None or job.status is not JobStatus.RUNNING:
            return False
        self._finish(job, JobStatus.CANCELLED)
        if job.task is not None:
            job.task.cancel()
        return True

    def state(self) -> Optional[dict]:
        job = self._job
        if job is None:
            return None
        if job.finished_at is not None and time.time() - job.finished_at > CHECK_RESULT_TTL_SECONDS:
            self._job = None
            return None
        return {
            "job_id": job.id,
            "status": job.status.value,
            "total": job.total,
            "done": job.done,
            "counts": dict(job.counts),
            "ok_via_proxy": job.ok_via_proxy,
            "failed": list(job.failed),
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }

    @staticmethod
    def _finish(job: _CheckJob, status: JobStatus) -> None:
        job.status = status
        job.finished_at = time.time()

    async def _run(self, job: _CheckJob, subjects: list[CheckSubject]) -> None:
        slots = asyncio.Semaphore(CHECK_CONCURRENCY)

        async def check_one(subject: CheckSubject) -> None:
            async with slots:
                outcome = await _check_subject(subject)
            self._record(job, subject, outcome)

        await asyncio.gather(*(check_one(subject) for subject in subjects))
        if job.status is JobStatus.RUNNING:
            self._finish(job, JobStatus.DONE)
            logger.info("SSH access check finished: total=%d counts=%s", job.total, dict(job.counts))

    @staticmethod
    def _record(job: _CheckJob, subject: CheckSubject, outcome: CheckOutcome) -> None:
        if job.status is not JobStatus.RUNNING:
            return
        server_id, name, target = subject
        job.done += 1
        job.counts[outcome.status.value] += 1
        if outcome.status is CheckStatus.OK:
            job.ok_via_proxy += outcome.via_proxy
            return
        if len(job.failed) >= FAILED_RESULTS_LIMIT:
            return
        level = target.login_level if isinstance(target, SSHTarget) else None
        job.failed.append({
            "server_id": server_id,
            "name": name,
            "status": outcome.status.value,
            "detail": outcome.detail,
            "login_level": level.value if level else None,
        })


async def _check_subject(subject: CheckSubject) -> CheckOutcome:
    server_id, _name, target = subject
    if isinstance(target, SSHTargetError):
        return CheckOutcome(CheckStatus(target.reason))
    try:
        return await check_target(target)
    except Exception as exc:  # noqa: BLE001 — граница фоновой задачи: сбой одного сервера не срывает проверку остальных
        logger.error("SSH access check of server %s failed: %s", server_id, exc)
        return CheckOutcome(CheckStatus.UNREACHABLE, type(exc).__name__)


_manager = SshAccessCheckManager()


def get_ssh_access_check_manager() -> SshAccessCheckManager:
    return _manager
