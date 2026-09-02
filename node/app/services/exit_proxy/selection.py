"""Чистая логика выбора выхода: слияние кандидатов, вердикт «здоров», липкий выбор.

Без сети и состояния — всё, что здесь, проверяется юнит-тестами напрямую.
"""

from dataclasses import dataclass
from typing import Optional

from app.services.exit_proxy.models import (
    WARP_CANDIDATE_ID,
    WARP_SOCKS_HOST,
    WARP_SOCKS_PORT,
    BuiltinChecks,
    Candidate,
    CheckResult,
    SelectMode,
)

REASON_KEEP = "keep"
REASON_PINNED = "pinned"
REASON_SWITCHED = "switched"
REASON_UNKNOWN = "unknown"
REASON_NO_HEALTHY = "no_healthy"
REASON_NO_CANDIDATES = "no_candidates"

SCORE_HEALTHY = 0
SCORE_UNKNOWN = 1
SCORE_UNHEALTHY = 2


@dataclass(frozen=True)
class DiscoveredIp:
    address: str
    primary: bool = False
    managed: bool = False


@dataclass(frozen=True)
class Decision:
    candidate: Optional[str]
    reason: str


def ip_candidate_id(address: str) -> str:
    return f"ip:{address}"


def merge_candidates(
    discovered: list[DiscoveredIp],
    warp_present: bool,
    order: list[str],
    disabled: list[str],
) -> list[Candidate]:
    """Живые адреса ноды в порядке приоритета пользователя.

    Порядок из конфига сохраняется; новые IP встают перед WARP (он в пуле —
    запасной выход), исчезнувшие с интерфейса пропадают из списка.
    """
    pool: dict[str, Candidate] = {}
    for ip in discovered:
        pool[ip_candidate_id(ip.address)] = Candidate(
            id=ip_candidate_id(ip.address), kind="ip", address=ip.address,
            primary=ip.primary, managed=ip.managed,
        )
    if warp_present:
        pool[WARP_CANDIDATE_ID] = Candidate(
            id=WARP_CANDIDATE_ID, kind="warp", address=f"{WARP_SOCKS_HOST}:{WARP_SOCKS_PORT}",
        )

    ordered: list[Candidate] = []
    for candidate_id in order:
        candidate = pool.get(candidate_id)
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    known = {candidate.id for candidate in ordered}

    new_ips = [candidate for candidate in pool.values() if candidate.id not in known and candidate.kind == "ip"]
    # Основной адрес — самый очевидный дефолт, он первым среди новых
    new_ips.sort(key=lambda candidate: (not candidate.primary, candidate.address))

    warp_position = next((index for index, candidate in enumerate(ordered) if candidate.kind == "warp"), None)
    if warp_position is None:
        merged = ordered + new_ips
        if WARP_CANDIDATE_ID in pool and WARP_CANDIDATE_ID not in known:
            merged.append(pool[WARP_CANDIDATE_ID])
    else:
        merged = ordered[:warp_position] + new_ips + ordered[warp_position:]

    disabled_ids = set(disabled)
    return [
        candidate.model_copy(update={"enabled": candidate.id not in disabled_ids, "priority": index})
        for index, candidate in enumerate(merged)
    ]


def health(
    result: Optional[CheckResult],
    blocked_countries: list[str],
    builtin: BuiltinChecks,
) -> Optional[bool]:
    """Вердикт по кандидату: True — здоров, False — Google его режет, None — не знаем.

    «Не знаем» — когда проверки не было или отдельный запрос не дошёл (таймаут):
    одиночный сетевой сбой не должен переключать выход и рвать пользователям
    сессии. Переключает только подтверждённый блок: страна из чёрного списка,
    капча, отказ Gemini, сработавшая пользовательская проверка.
    """
    if result is None or not result.ok:
        return None

    unknown = False
    if builtin.google_country:
        if not result.country:
            unknown = True
        elif result.country in blocked_countries:
            return False
    if builtin.google_captcha and result.captcha:
        return False
    if builtin.gemini:
        if result.gemini == "blocked":
            return False
        if result.gemini == "error":
            unknown = True
    for item in result.checks:
        if item.status is None:
            unknown = True
            continue
        if not item.ok:
            return False
    return None if unknown else True


def _score(verdict: Optional[bool]) -> int:
    if verdict is True:
        return SCORE_HEALTHY
    if verdict is None:
        return SCORE_UNKNOWN
    return SCORE_UNHEALTHY


def choose_exit(
    candidates: list[Candidate],
    health_by_id: dict[str, Optional[bool]],
    current: Optional[str],
    mode: SelectMode,
    pinned: Optional[str],
) -> Decision:
    """Липкий выбор: текущий выход остаётся, пока он не хуже лучшего из доступных.

    Здоровых нет вовсе — первый включённый по приоритету (решение владельца:
    хоть какой-то выход лучше никакого).
    """
    enabled_ids = [candidate.id for candidate in candidates if candidate.enabled]
    if not enabled_ids:
        return Decision(None, REASON_NO_CANDIDATES)
    if mode == "manual" and pinned in enabled_ids:
        return Decision(pinned, REASON_PINNED)

    best = min(_score(health_by_id.get(candidate_id)) for candidate_id in enabled_ids)
    if best == SCORE_UNHEALTHY:
        return Decision(enabled_ids[0], REASON_NO_HEALTHY)
    if current in enabled_ids and _score(health_by_id.get(current)) == best:
        return Decision(current, REASON_KEEP)
    chosen = next(candidate_id for candidate_id in enabled_ids if _score(health_by_id.get(candidate_id)) == best)
    return Decision(chosen, REASON_SWITCHED if best == SCORE_HEALTHY else REASON_UNKNOWN)
