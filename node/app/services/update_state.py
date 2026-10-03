"""Итог последнего обновления агента, переживающий перезапуск его процесса.

При успешном обновлении apply-update.sh пересоздаёт контейнеры, и процесс,
запустивший апдейтер, до итога не доживает. Поэтому попытка пишется на диск до
старта апдейтера, контейнер апдейтера помечается её идентификатором, а итог
записывает тот, кто его увидел: прежний процесс, если дождался апдейтера, или
новый — по коду выхода оставшегося контейнера. Панель по идентификатору отличает
итог своей попытки от чужой и решает, нужен ли запасной путь через SSH.
"""

import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Именованный том агента: переживает и рестарт, и пересоздание контейнера
STATE_FILE = Path("/var/lib/monitoring/update-state.json")
ATTEMPT_LABEL = "monitoring.update-attempt"

# apply-update.sh: образ не достался из реестра, а локальная сборка запрещена
EXIT_IMAGE_UNAVAILABLE = 20
ERROR_TAIL_CHARS = 1000

# Этапы обновления по порядку. Строки «[STAGE] <этап>» печатают в лог апдейтера
# скрипт апдейтера (download) и apply-update.sh (files, images, restart) — по
# последней из них панель показывает, на каком шаге обновление
UPDATE_STAGES = ("download", "files", "images", "restart")
_STAGE_MARKER = re.compile(r"^\[STAGE\] (\w+)\s*$", re.MULTILINE)


class UpdateResult(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True)
class UpdateOutcome:
    result: UpdateResult
    reason: Optional[str] = None  # image_unavailable | interrupted
    error: Optional[str] = None


@dataclass(frozen=True)
class UpdateAttempt:
    attempt_id: str
    target_ref: str
    from_version: str
    started_at: str
    result: UpdateResult = UpdateResult.RUNNING
    reason: Optional[str] = None
    error: Optional[str] = None
    finished_at: Optional[str] = None

    @property
    def running(self) -> bool:
        return self.result is UpdateResult.RUNNING


@dataclass(frozen=True)
class UpdaterContainer:
    """То, что осталось от апдейтера в докере."""
    running: bool
    exit_code: Optional[int]
    attempt_id: Optional[str]
    logs: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_attempt(target_ref: str, from_version: str) -> UpdateAttempt:
    return UpdateAttempt(
        attempt_id=uuid.uuid4().hex,
        target_ref=target_ref,
        from_version=from_version,
        started_at=_now(),
    )


def finish(attempt: UpdateAttempt, outcome: UpdateOutcome) -> UpdateAttempt:
    return replace(
        attempt,
        result=outcome.result,
        reason=outcome.reason,
        error=outcome.error,
        finished_at=_now(),
    )


def outcome_from_exit(exit_code: Optional[int], logs: str) -> UpdateOutcome:
    if exit_code == 0:
        return UpdateOutcome(UpdateResult.SUCCESS)
    if exit_code == EXIT_IMAGE_UNAVAILABLE:
        return UpdateOutcome(
            UpdateResult.FAILED,
            reason="image_unavailable",
            error="Образ недоступен из реестра, сборка отключена — нужна доставка образа с панели",
        )
    return UpdateOutcome(UpdateResult.FAILED, error=f"Exit code: {exit_code}\n{logs[-ERROR_TAIL_CHARS:]}")


def resolve_orphaned(attempt: UpdateAttempt, container: Optional[UpdaterContainer]) -> UpdateAttempt:
    """Итог попытки, процесс-владелец которой уже не ждёт апдейтер."""
    if not attempt.running:
        return attempt
    if container is not None and container.attempt_id == attempt.attempt_id:
        if container.running:
            return attempt
        return finish(attempt, outcome_from_exit(container.exit_code, container.logs))
    # Контейнера этой попытки нет: агент упал между записью попытки и стартом
    # апдейтера, либо контейнер удалили руками — итог неизвестен
    return finish(attempt, UpdateOutcome(
        UpdateResult.FAILED,
        reason="interrupted",
        error="Апдейтер завершился, не оставив итога",
    ))


def current_stage(logs: str) -> Optional[str]:
    """Последний пройденный этап по меткам в логе апдейтера."""
    stages = [stage for stage in _STAGE_MARKER.findall(logs) if stage in UPDATE_STAGES]
    return stages[-1] if stages else None


def status_payload(
    attempt: Optional[UpdateAttempt], in_progress: bool, stage: Optional[str], version: str,
) -> dict:
    return {
        "in_progress": in_progress,
        "stage": stage,
        "last_result": None if attempt is None or attempt.running else attempt.result.value,
        "last_error": attempt.error if attempt else None,
        "reason": attempt.reason if attempt else None,
        "attempt_id": attempt.attempt_id if attempt else None,
        "target_ref": attempt.target_ref if attempt else None,
        "started_at": attempt.started_at if attempt else None,
        "finished_at": attempt.finished_at if attempt else None,
        "version": version,
    }


class UpdateStateStore:
    def __init__(self, path: Path = STATE_FILE):
        self._path = path

    def load(self) -> Optional[UpdateAttempt]:
        try:
            raw = json.loads(self._path.read_text())
            return UpdateAttempt(**{**raw, "result": UpdateResult(raw["result"])})
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError) as e:
            logger.warning(f"Unreadable update state {self._path}: {e}")
            return None

    def save(self, attempt: UpdateAttempt) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**asdict(attempt), "result": attempt.result.value}))
        os.replace(tmp, self._path)

    def record(self, attempt_id: str, outcome: UpdateOutcome) -> None:
        """Записать итог, если файл всё ещё про эту попытку — опоздавший итог
        старой попытки не должен затереть уже запущенную новую."""
        current = self.load()
        if current is None or current.attempt_id != attempt_id:
            return
        self.save(finish(current, outcome))
