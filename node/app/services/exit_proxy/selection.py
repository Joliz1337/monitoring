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

@dataclass(frozen=True)
class CheckTally:
    """Счёт кандидата по включённым проверкам: сколько прошёл, провалил и не смог проверить."""

    passed: int = 0
    failed: int = 0
    unknown: int = 0

    @property
    def verdict(self) -> Optional[bool]:
        """True — здоров, False — подтверждённый блок, None — не знаем."""
        if self.failed:
            return False
        if self.unknown:
            return None
        return True

    @property
    def rank(self) -> tuple[int, int]:
        """Меньше — лучше: сначала больше пройденных, при равенстве меньше проваленных."""
        return (-self.passed, self.failed)


NO_RESULT = CheckTally(unknown=1)


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


def tally(
    result: Optional[CheckResult],
    blocked_countries: list[str],
    builtin: BuiltinChecks,
) -> CheckTally:
    """Счёт по кандидату.

    Проверки не было или трасса не прошла — всё «не знаем»: одиночный сетевой
    сбой не должен сам по себе переключать выход и рвать пользователям сессии.
    Провал — только подтверждённый блок: страна из чёрного списка, капча,
    отказ Gemini, сработавшая пользовательская проверка.
    """
    if result is None or not result.ok:
        return NO_RESULT

    passed = failed = unknown = 0
    if builtin.google_country:
        if not result.country:
            unknown += 1
        elif result.country in blocked_countries:
            failed += 1
        else:
            passed += 1
    if builtin.google_captcha:
        if result.captcha:
            failed += 1
        else:
            passed += 1
    if builtin.gemini:
        if result.gemini == "blocked":
            failed += 1
        elif result.gemini == "error":
            unknown += 1
        else:
            passed += 1
    for item in result.checks:
        if item.status is None:
            unknown += 1
        elif item.ok:
            passed += 1
        else:
            failed += 1
    return CheckTally(passed=passed, failed=failed, unknown=unknown)


def choose_exit(
    candidates: list[Candidate],
    tallies: dict[str, CheckTally],
    current: Optional[str],
    mode: SelectMode,
    pinned: Optional[str],
) -> Decision:
    """Выход, прошедший больше всего проверок; при равном счёте выбор липкий.

    Ранг — (пройдено ↓, провалено ↑): 3 из 3 бьёт 2 из 3, а при равном числе
    пройденных таймаут («не знаем») лучше подтверждённого блока. Текущий
    остаётся, пока никто не набрал ранг строго лучше; среди равных новых —
    первый по приоритету. Никто не прошёл всё — всё равно берётся лучший по
    счёту, а причина `no_healthy` держит событие «нет здоровых».
    """
    enabled_ids = [candidate.id for candidate in candidates if candidate.enabled]
    if not enabled_ids:
        return Decision(None, REASON_NO_CANDIDATES)
    if mode == "manual" and pinned in enabled_ids:
        return Decision(pinned, REASON_PINNED)

    def rank(candidate_id: str) -> tuple[int, int]:
        return tallies.get(candidate_id, NO_RESULT).rank

    best = min(rank(candidate_id) for candidate_id in enabled_ids)
    if current in enabled_ids and rank(current) == best:
        chosen = current
    else:
        chosen = next(candidate_id for candidate_id in enabled_ids if rank(candidate_id) == best)
    verdict = tallies.get(chosen, NO_RESULT).verdict
    if verdict is False:
        return Decision(chosen, REASON_NO_HEALTHY)
    if chosen == current:
        return Decision(chosen, REASON_KEEP)
    return Decision(chosen, REASON_SWITCHED if verdict is True else REASON_UNKNOWN)
