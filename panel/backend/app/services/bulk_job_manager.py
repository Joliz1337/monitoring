"""Фоновые задачи массовых действий (Bulk Actions).

Массовая операция выполняется в фоновой asyncio-задаче, не привязанной к
HTTP-соединению клиента: обрыв связи или закрытие вкладки браузера выполнение
не прерывают. Конкурентность запросов к нодам ограничена семафором, фронт
опрашивает прогресс и результаты по job_id.

Ограничение: задачи живут в памяти процесса backend — перезапуск контейнера
теряет их вместе с результатами.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, TypeVar

from pydantic import BaseModel

from app.models import Server

logger = logging.getLogger(__name__)

# Максимум одновременных запросов к нодам в рамках одной массовой операции
NODE_CONCURRENCY = 20
# Завершённые задачи держим для переподключения и просмотра результата
FINISHED_TTL_SECONDS = 600

T = TypeVar("T")

BulkExecutor = Callable[[Server], Awaitable[BaseModel]]


async def run_bulk(servers: list[Server], executor: Callable[[Server], Awaitable[T]]) -> list[T]:
    """Выполняет executor на каждом сервере, не более NODE_CONCURRENCY одновременно."""
    semaphore = asyncio.Semaphore(NODE_CONCURRENCY)

    async def guarded(server: Server) -> T:
        async with semaphore:
            return await executor(server)

    return list(await asyncio.gather(*[guarded(s) for s in servers]))


@dataclass
class BulkJob:
    id: str
    action: str
    total: int
    status: str = "running"  # running | completed
    done: int = 0
    results: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    task: Optional[asyncio.Task] = None


class BulkJobManager:
    """In-memory реестр фоновых массовых операций."""

    def __init__(self) -> None:
        self._jobs: dict[str, BulkJob] = {}

    def _cleanup_finished(self) -> None:
        now = time.time()
        expired = [
            jid for jid, job in self._jobs.items()
            if job.finished_at is not None and now - job.finished_at > FINISHED_TTL_SECONDS
        ]
        for jid in expired:
            self._jobs.pop(jid, None)

    def get(self, job_id: str) -> Optional[BulkJob]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        """Активные и недавно завершённые задачи — для восстановления на фронте."""
        self._cleanup_finished()
        return [
            self.job_summary(job)
            for job in sorted(self._jobs.values(), key=lambda j: j.started_at)
        ]

    @staticmethod
    def job_summary(job: BulkJob) -> dict:
        return {
            "job_id": job.id,
            "action": job.action,
            "status": job.status,
            "total": job.total,
            "done": job.done,
        }

    @classmethod
    def job_state(cls, job: BulkJob) -> dict:
        return {**cls.job_summary(job), "results": list(job.results)}

    def start(self, action: str, servers: list[Server], executor: BulkExecutor) -> str:
        self._cleanup_finished()
        job = BulkJob(id=uuid.uuid4().hex, action=action, total=len(servers))
        self._jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, servers, executor))
        return job.id

    async def _run(self, job: BulkJob, servers: list[Server], executor: BulkExecutor) -> None:
        async def track_one(server: Server) -> None:
            try:
                result = (await executor(server)).model_dump()
            except Exception as exc:  # noqa: BLE001 — сбой одной ноды не роняет задачу
                logger.error(
                    "Bulk job %s: action %s failed on server %s: %s",
                    job.id, job.action, server.id, exc,
                )
                result = {
                    "server_id": server.id,
                    "server_name": server.name,
                    "success": False,
                    "message": str(exc),
                }
            job.results.append(result)
            job.done += 1

        try:
            await run_bulk(servers, track_one)
        finally:
            job.status = "completed"
            job.finished_at = time.time()


_manager = BulkJobManager()


def get_bulk_job_manager() -> BulkJobManager:
    return _manager
