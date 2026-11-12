"""HTTP-ответы фоновой раскатки — общие для четырёх роутеров профилей
(firewall, HAProxy, DNAT, nginx Remnawave)."""

from fastapi import HTTPException

from app.services.profile_rollouts import (
    RolloutInProgressError,
    RolloutRunner,
    get_rollout_registry,
)

ROLLOUT_IN_PROGRESS_MESSAGE = "Раскатка этого профиля уже идёт — дождитесь её окончания"


def start_profile_rollout(kind: str, profile_id: int, runner: RolloutRunner) -> dict:
    try:
        rollout = get_rollout_registry().start(kind, profile_id, runner)
    except RolloutInProgressError:
        raise HTTPException(409, ROLLOUT_IN_PROGRESS_MESSAGE)
    return {"job": rollout.to_dict()}


def profile_rollout_state(kind: str, profile_id: int) -> dict:
    rollout = get_rollout_registry().latest(kind, profile_id)
    return {"job": rollout.to_dict() if rollout else None}
