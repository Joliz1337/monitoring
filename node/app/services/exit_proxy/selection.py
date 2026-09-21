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
    # Провалы именно Google (страна, капча, Gemini) — ради них прокси и существует
    google_failed: int = 0
    # False — трасса через выход не прошла, в интернет он не ходит
    reachable: bool = True

    @property
    def verdict(self) -> Optional[bool]:
        """True — здоров, False — подтверждённый блок, None — не знаем."""
        if self.failed:
            return False
        if self.unknown:
            return None
        return True

    @property
    def rank(self) -> tuple[int, int, int]:
        """Меньше — лучше: больше пройденных, затем меньше провалов Google, затем меньше провалов вообще."""
        return (-self.passed, self.google_failed, self.failed)

    @property
    def urgent(self) -> bool:
        """С такого текущего выхода уходят сразу, без подтверждения: он не отвечает или его режет Google."""
        return not self.reachable or self.google_failed > 0


NO_RESULT = CheckTally(unknown=1, reachable=False)


@dataclass(frozen=True)
class DiscoveredIp:
    address: str
    primary: bool = False
    managed: bool = False


@dataclass(frozen=True)
class Decision:
    candidate: Optional[str]
    reason: str
    # Кандидат лучше текущего, но смена ждёт подтверждения следующим прогоном
    pending: Optional[str] = None


def ip_candidate_id(address: str) -> str:
    return f"ip:{address}"


def merge_candidates(
    discovered: list[DiscoveredIp],
    warp_present: bool,
    order: list[str],
    disabled: list[str],
) -> list[Candidate]:
    """Живые адреса ноды в порядке приоритета пользователя.

    Порядок из конфига сохраняется. WARP, которого пользователь не расставлял,
    встаёт первым: при равном счёте он предпочтительнее — через него Google не
    запоминает трафик за собственными адресами ноды. Новые IP встают в конец,
    исчезнувшие с интерфейса пропадают из списка.
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

    warp_unplaced = WARP_CANDIDATE_ID in pool and WARP_CANDIDATE_ID not in known
    merged = ([pool[WARP_CANDIDATE_ID]] if warp_unplaced else []) + ordered + new_ips

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

    passed = failed = unknown = google_failed = 0
    if builtin.google_country:
        if not result.country:
            unknown += 1
        elif result.country in blocked_countries:
            failed += 1
            google_failed += 1
        else:
            passed += 1
    if builtin.google_captcha:
        if result.captcha:
            failed += 1
            google_failed += 1
        else:
            passed += 1
    if builtin.gemini:
        if result.gemini == "blocked":
            failed += 1
            google_failed += 1
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
    return CheckTally(passed=passed, failed=failed, unknown=unknown, google_failed=google_failed)


def choose_exit(
    candidates: list[Candidate],
    tallies: dict[str, CheckTally],
    current: Optional[str],
    mode: SelectMode,
    pinned: Optional[str],
    pending: Optional[str] = None,
) -> Decision:
    """Выход с наибольшим числом пройденных проверок; при равном счёте — первый по приоритету.

    Ранг — (пройдено ↓, провалов Google ↑, провалов ↑): 3 из 3 бьёт 2 из 3, а при
    равном числе пройденных таймаут («не знаем») лучше подтверждённого блока.
    Лучший при равном ранге — первый по приоритету пользователя (WARP по умолчанию).

    Смена сразу — если текущий не отвечает или его режет Google, а лучший строго
    лучше по рангу. Иначе (текущий проиграл по своим проверкам, либо равный ему
    выше по приоритету) смена ждёт подтверждения: `pending` из прошлого прогона
    должен совпасть с лучшим — одна флаки-проверка выход не дёргает. Равный
    ранг среди нездоровых выход не меняет. Никто не прошёл всё — всё равно
    берётся лучший по счёту, а причина `no_healthy` держит событие «нет здоровых».
    """
    enabled_ids = [candidate.id for candidate in candidates if candidate.enabled]
    if not enabled_ids:
        return Decision(None, REASON_NO_CANDIDATES)
    if mode == "manual" and pinned in enabled_ids:
        return Decision(pinned, REASON_PINNED)

    def rank(candidate_id: str) -> tuple[int, int, int]:
        return tallies.get(candidate_id, NO_RESULT).rank

    best = min(enabled_ids, key=lambda candidate_id: (rank(candidate_id), enabled_ids.index(candidate_id)))
    best_verdict = tallies.get(best, NO_RESULT).verdict
    reason_no_healthy = best_verdict is False
    if best == current:
        return Decision(current, REASON_NO_HEALTHY if reason_no_healthy else REASON_KEEP)

    switch_reason = REASON_NO_HEALTHY if reason_no_healthy else (REASON_SWITCHED if best_verdict else REASON_UNKNOWN)
    if current not in enabled_ids:
        return Decision(best, switch_reason)
    current_tally = tallies.get(current, NO_RESULT)
    strictly_better = rank(best) < current_tally.rank
    if strictly_better and current_tally.urgent:
        return Decision(best, switch_reason)
    if not strictly_better and best_verdict is not True:
        return Decision(current, REASON_NO_HEALTHY if reason_no_healthy else REASON_KEEP)
    if pending == best:
        return Decision(best, switch_reason)
    return Decision(current, REASON_NO_HEALTHY if reason_no_healthy else REASON_KEEP, pending=best)
