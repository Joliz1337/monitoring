"""Автоотключение адресов назначения на релеях: когда выводить и когда возвращать.

Релей меряет потери до своих бэкендов и считает их падения (loss_registry).
Адрес, до которого релей не доходит, HAProxy держит UP и продолжает слать на
него клиентов. Здесь решается, какие пары «релей → адрес» вывести из
балансировки; исполняет нода (backend_quarantine_runner → её stats socket).

Пара, а не адрес: фильтрация на пути зависит от релея, с другого релея тот же
адрес работает.

- Отключить — потери не ниже порога дольше удержания либо падений за полчаса
  не меньше порога.
- Решение ещё не отключение: пара `pending`, пока нода не ответила, `disabled`
  — когда применила, `blocked` — когда отказала (последний сервер правила,
  предел доли) или до неё не достучаться. Срок, счёт отключений и сообщение —
  с подтверждения ноды.
- Вернуть — минимальный срок вышел и потери ниже половины порога всё затишье.
  Срок растёт с каждым отключением пары за сутки: мигающий адрес не должен
  ходить туда-обратно каждые десять минут.
- Беда релея — он теряет больше половины своих адресов разом: дело в его
  сети, отключать нечего. Проверяется до удержания, а после выхода таймеры
  идут с нуля — иначе оставшиеся плохие адреса отключились бы залпом.

Модуль чистый: без базы и сети, время приходит снаружи.
"""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional

from app.services.loss_alerts import MIN_SAMPLES, RECOVERY_RATIO
from app.services.loss_registry import (
    STALE_AFTER_SEC,
    TOTAL_LOSS_PCT,
    LossReading,
    RelaySnapshot,
    TargetMode,
    TargetRules,
)

TERMS_SEC = (600, 1800, 7200, 21600)
STRIKE_WINDOW_SEC = 86400
MANUAL_HOLD_SEC = 3600
NO_DATA_CLOSE_SEC = 3600
# Один ответ «адреса нет» бывает и сразу после reload — запись закрывает второй подряд
MISSING_ANSWERS_TO_CLOSE = 2
RELAY_TROUBLE_MIN_PAIRS = 3
RELAY_TROUBLE_ENTER_SHARE = 0.5
RELAY_TROUBLE_EXIT_SHARE = 0.25

PairKey = tuple[int, str]


class Mode(str, Enum):
    OFF = "off"
    OBSERVE = "observe"
    ENFORCE = "enforce"


class PairState(str, Enum):
    PENDING = "pending"
    DISABLED = "disabled"
    BLOCKED = "blocked"
    WOULD_DISABLE = "would_disable"
    # Сейчас не отключена: запись хранит счёт отключений и паузу после ручного возврата
    IDLE = "idle"


class ReasonKind(str, Enum):
    LOSS = "loss"
    DOWNS = "downs"


class EventKind(str, Enum):
    DISABLED = "disabled"
    RESTORED = "restored"
    CHRONIC = "chronic"
    RELAY_PAUSED = "relay_paused"


class RestoreCause(str, Enum):
    CALM = "calm"
    EXEMPT = "exempt"
    MANUAL = "manual"
    GUARD = "guard"
    MODE = "mode"


class NodeFailure(str, Enum):
    UNSUPPORTED = "unsupported"
    DENIED = "denied"
    OFFLINE = "offline"
    ERROR = "error"


NOT_FOUND = "not_found"
WANTED_STATES = (PairState.PENDING, PairState.DISABLED, PairState.BLOCKED)


@dataclass(frozen=True)
class QuarantinePolicy:
    loss_threshold: float = 10.0
    sustained_sec: float = 180
    downs_threshold: int = 3
    calm_sec: float = 600
    chronic_strikes: int = 4
    max_share_pct: int = 50
    drop_sessions: bool = True


@dataclass(frozen=True)
class Exemptions:
    """Что автоматика не трогает: релеи целиком, адреса целиком и отдельные ip:порт."""
    server_ids: frozenset[int] = frozenset()
    ips: frozenset[str] = frozenset()
    endpoints: frozenset[tuple[str, int]] = frozenset()

    def covers(self, relay_id: int, reading: LossReading) -> bool:
        return (
            relay_id in self.server_ids
            or reading.ip in self.ips
            or (reading.ip, reading.port) in self.endpoints
        )


@dataclass
class Pair:
    relay_id: int
    relay_name: str
    target: str
    state: PairState
    reason_kind: ReasonKind
    loss_pct: float
    downs: int
    decided_at: float
    last_data_at: float
    # Подтверждение ноды; держится до конца эпизода, даже если нода на время сняла отключение
    disabled_at: Optional[float] = None
    until: Optional[float] = None
    calm_since: Optional[float] = None
    strikes: list[float] = field(default_factory=list)
    hold_until: Optional[float] = None
    # Почему не отключено: {"error": код}; у отключённой пары пусто
    node: dict = field(default_factory=dict)
    missing: int = 0
    sessions: int = 0
    chronic_notified: bool = False

    def to_dict(self) -> dict:
        return {**asdict(self), "state": self.state.value, "reason_kind": self.reason_kind.value}

    @classmethod
    def from_dict(cls, data: dict) -> "Pair":
        return cls(**{**data, "state": PairState(data["state"]), "reason_kind": ReasonKind(data["reason_kind"])})


@dataclass(frozen=True)
class Event:
    kind: EventKind
    relay_id: int
    relay_name: str
    target: str = ""
    reason_kind: Optional[ReasonKind] = None
    loss_pct: float = 0.0
    downs: int = 0
    # Сколько ещё пара пробудет отключённой самое малое
    term_sec: float = 0
    sessions: int = 0
    strikes: int = 0
    cause: Optional[RestoreCause] = None
    observe: bool = False
    # Релеи, на которых адрес отключается постоянно (только CHRONIC)
    relays: tuple[str, ...] = ()


@dataclass
class RelayPause:
    name: str
    since: float
    last_seen: float
    calm_since: Optional[float] = None


def _trigger_loss(mode: TargetMode, policy: QuarantinePolicy) -> float:
    return TOTAL_LOSS_PCT if mode is TargetMode.TOTAL_ONLY else policy.loss_threshold


def _recovery_bound(mode: TargetMode, policy: QuarantinePolicy) -> float:
    # На адресе «только полные потери» частичные потери — норма: возврат уже ниже самого порога
    return TOTAL_LOSS_PCT if mode is TargetMode.TOTAL_ONLY else policy.loss_threshold * RECOVERY_RATIO


def _counts_downs(mode: TargetMode) -> bool:
    return mode is not TargetMode.TOTAL_ONLY


def _is_bad(reading: LossReading, mode: TargetMode, policy: QuarantinePolicy) -> bool:
    if reading.loss_pct >= _trigger_loss(mode, policy):
        return True
    return _counts_downs(mode) and (reading.downs or 0) >= policy.downs_threshold


@dataclass
class _Observed:
    key: PairKey
    reading: LossReading
    mode: TargetMode


@dataclass
class QuarantineTracker:
    pairs: dict[PairKey, Pair] = field(default_factory=dict)
    # Пара → с какого момента потери держатся не ниже порога
    _above: dict[PairKey, float] = field(default_factory=dict)
    _pauses: dict[int, RelayPause] = field(default_factory=dict)

    def evaluate(self, snapshots: list[RelaySnapshot], rules: TargetRules, exempt: Exemptions,
                 policy: QuarantinePolicy, mode: Mode, now: float) -> list[Event]:
        events: list[Event] = []
        seen: set[PairKey] = set()
        for snapshot in snapshots:
            observed = []
            for reading in snapshot.readings:
                key = (snapshot.server_id, reading.key)
                target_mode = rules.mode(reading)
                if target_mode is TargetMode.HIDDEN or exempt.covers(snapshot.server_id, reading):
                    self._above.pop(key, None)
                    events += self._release(key, RestoreCause.EXEMPT)
                    continue
                # downs=None — адреса нет в HAProxy релея (цель DNAT) или нода старая: отключать нечем
                if reading.downs is None or reading.samples < MIN_SAMPLES:
                    continue
                observed.append(_Observed(key, reading, target_mode))
            paused = self._relay_paused(snapshot, observed, policy, now, events)
            for item in observed:
                seen.add(item.key)
                events += self._step(item, snapshot.name, policy, mode, paused, now)
        self._close_silent(seen, now)
        self._forget_old(now)
        return events + self._chronic(policy, mode)

    def desired(self) -> dict[int, list[str]]:
        """Релей → адреса, которые он должен держать отключёнными; худшие первыми —
        нода раздаёт слоты доли в этом порядке."""
        wanted: dict[int, list[Pair]] = {}
        for pair in self.pairs.values():
            if pair.state in WANTED_STATES:
                wanted.setdefault(pair.relay_id, []).append(pair)
        return {
            relay_id: [pair.target for pair in sorted(pairs, key=lambda p: (-p.loss_pct, p.target))]
            for relay_id, pairs in wanted.items()
        }

    def apply_node_results(self, relay_id: int, results: list[dict] | NodeFailure,
                           policy: QuarantinePolicy, now: float) -> list[Event]:
        wanted = [pair for pair in self.pairs.values() if pair.relay_id == relay_id and pair.state in WANTED_STATES]
        if isinstance(results, NodeFailure):
            # Уже отключённые остаются: нода держит их до срока и сама вернёт, если панель до неё не дойдёт
            for pair in wanted:
                if pair.state is not PairState.DISABLED:
                    pair.state, pair.node = PairState.BLOCKED, {"error": results.value}
            return []
        answers = {f"{item.get('ip')}:{item.get('port')}": item for item in results}
        events: list[Event] = []
        for pair in wanted:
            answer = answers.get(pair.target)
            if answer is not None:
                events += self._apply_answer(pair, answer, now)
        return events + self._chronic(policy, Mode.ENFORCE)

    def release(self, relay_id: int, target: str, now: float) -> list[Event]:
        """Ручной возврат: пара какое-то время не отключается заново, иначе
        следующий же цикл вернул бы её обратно."""
        key = (relay_id, target)
        pair = self.pairs.get(key)
        if pair is None or pair.state is PairState.IDLE:
            return []
        events = self._release(key, RestoreCause.MANUAL)
        pair.hold_until = now + MANUAL_HOLD_SEC
        self._above.pop(key, None)
        return events

    def clear(self, now: float, cause: RestoreCause) -> list[Event]:
        """Режим сменили: всё отключённое возвращается, счёт отключений остаётся."""
        events: list[Event] = []
        for key in list(self.pairs):
            events += [event for event in self._release(key, cause) if not event.observe]
        self._above.clear()
        self._pauses.clear()
        self._forget_old(now)
        return events

    def paused_relays(self) -> dict[int, RelayPause]:
        return dict(self._pauses)

    def chronic_targets(self, now: float, policy: QuarantinePolicy) -> dict[str, int]:
        """Адрес → наибольшее число отключений одной его пары за сутки."""
        edge = now - STRIKE_WINDOW_SEC
        chronic: dict[str, int] = {}
        for pair in self.pairs.values():
            recent = sum(1 for moment in pair.strikes if moment > edge)
            if recent >= policy.chronic_strikes:
                chronic[pair.target] = max(chronic.get(pair.target, 0), recent)
        return chronic

    def _relay_paused(self, snapshot: RelaySnapshot, observed: list[_Observed], policy: QuarantinePolicy,
                      now: float, events: list[Event]) -> bool:
        bad = sum(1 for item in observed if self._counts_as_bad(item, policy))
        pause = self._pauses.get(snapshot.server_id)
        if pause is None:
            if bad < RELAY_TROUBLE_MIN_PAIRS or bad <= len(observed) * RELAY_TROUBLE_ENTER_SHARE:
                return False
            self._pauses[snapshot.server_id] = RelayPause(snapshot.name, now, now)
            events.append(Event(EventKind.RELAY_PAUSED, snapshot.server_id, snapshot.name))
        else:
            pause.last_seen = now
            if bad > len(observed) * RELAY_TROUBLE_EXIT_SHARE:
                pause.calm_since = None
            else:
                pause.calm_since = pause.calm_since if pause.calm_since is not None else now
                if now - pause.calm_since >= policy.sustained_sec:
                    del self._pauses[snapshot.server_id]
                    return False
        for item in observed:
            self._above.pop(item.key, None)
        return True

    def _counts_as_bad(self, item: _Observed, policy: QuarantinePolicy) -> bool:
        if _is_bad(item.reading, item.mode, policy):
            return True
        pair = self.pairs.get(item.key)
        still_out = pair is not None and pair.state is PairState.DISABLED
        return still_out and item.reading.loss_pct >= _recovery_bound(item.mode, policy)

    def _step(self, item: _Observed, relay_name: str, policy: QuarantinePolicy, mode: Mode,
              paused: bool, now: float) -> list[Event]:
        pair = self.pairs.get(item.key)
        if pair is not None:
            if now - pair.last_data_at > STALE_AFTER_SEC:
                # Что было, пока данных не было, неизвестно: затишье считается заново
                pair.calm_since = None
            pair.last_data_at = now
            pair.relay_name = relay_name
        if pair is None or pair.state is PairState.IDLE:
            return self._step_idle(item, pair, relay_name, policy, mode, paused, now)
        return self._step_active(item, pair, policy, now)

    def _step_idle(self, item: _Observed, pair: Optional[Pair], relay_name: str, policy: QuarantinePolicy,
                   mode: Mode, paused: bool, now: float) -> list[Event]:
        held = pair is not None and pair.hold_until is not None and now < pair.hold_until
        if paused or held:
            self._above.pop(item.key, None)
            return []
        reason = self._reason(item, policy, now)
        if reason is None:
            return []
        self._above.pop(item.key, None)
        if pair is None:
            pair = Pair(
                relay_id=item.key[0], relay_name=relay_name, target=item.key[1], state=PairState.PENDING,
                reason_kind=reason, loss_pct=item.reading.loss_pct, downs=item.reading.downs or 0,
                decided_at=now, last_data_at=now,
            )
            self.pairs[item.key] = pair
        else:
            pair.state, pair.reason_kind = PairState.PENDING, reason
            pair.loss_pct, pair.downs, pair.decided_at = item.reading.loss_pct, item.reading.downs or 0, now
            pair.calm_since, pair.node, pair.missing = None, {}, 0
        if mode is not Mode.OBSERVE:
            return []
        pair.state, pair.disabled_at = PairState.WOULD_DISABLE, now
        self._strike(pair, now)
        return [self._event(EventKind.DISABLED, pair, observe=True)]

    def _reason(self, item: _Observed, policy: QuarantinePolicy, now: float) -> Optional[ReasonKind]:
        if item.reading.loss_pct >= _trigger_loss(item.mode, policy):
            since = self._above.setdefault(item.key, now)
            if now - since >= policy.sustained_sec:
                return ReasonKind.LOSS
        else:
            self._above.pop(item.key, None)
        if _counts_downs(item.mode) and (item.reading.downs or 0) >= policy.downs_threshold:
            return ReasonKind.DOWNS
        return None

    def _step_active(self, item: _Observed, pair: Pair, policy: QuarantinePolicy, now: float) -> list[Event]:
        quiet = item.reading.loss_pct < _recovery_bound(item.mode, policy)
        if pair.state is PairState.WOULD_DISABLE and _counts_downs(item.mode):
            # В наблюдении сервер не в обслуживании — его падения видны и держат пару в списке
            quiet = quiet and (item.reading.downs or 0) < policy.downs_threshold
        if not quiet:
            pair.calm_since = None
            return []
        if pair.calm_since is None:
            pair.calm_since = now
        if now - pair.calm_since < policy.calm_sec:
            return []
        if pair.state is PairState.DISABLED and pair.until is not None and now < pair.until:
            return []
        return self._release(item.key, RestoreCause.CALM)

    def _apply_answer(self, pair: Pair, answer: dict, now: float) -> list[Event]:
        applied = answer.get("applied") or []
        if applied:
            pair.missing, pair.node = 0, {}
            if pair.state is PairState.DISABLED:
                return []
            pair.state = PairState.DISABLED
            if pair.disabled_at is None:
                pair.disabled_at = now
                self._strike(pair, now)
                pair.until = now + TERMS_SEC[min(len(pair.strikes), len(TERMS_SEC)) - 1]
                pair.sessions = sum(int(row.get("sessions") or 0) for row in applied)
            return [self._event(EventKind.DISABLED, pair, term_sec=max(0.0, (pair.until or now) - now))]
        if not answer.get("found"):
            pair.missing += 1
            if pair.missing >= MISSING_ANSWERS_TO_CLOSE:
                self._idle(pair)
            elif pair.state is not PairState.DISABLED:
                pair.state, pair.node = PairState.BLOCKED, {"error": NOT_FOUND}
            return []
        pair.missing = 0
        skipped = answer.get("skipped") or []
        reason = str(skipped[0].get("reason")) if skipped else NodeFailure.ERROR.value
        events = []
        if pair.state is PairState.DISABLED:
            # Нода сняла отключение сама: сервер остался последним живым в правиле
            events.append(self._event(EventKind.RESTORED, pair, cause=RestoreCause.GUARD))
        pair.state, pair.node = PairState.BLOCKED, {"error": reason}
        return events

    def _release(self, key: PairKey, cause: RestoreCause) -> list[Event]:
        pair = self.pairs.get(key)
        if pair is None or pair.state is PairState.IDLE:
            return []
        reported = pair.state in (PairState.DISABLED, PairState.WOULD_DISABLE)
        event = self._event(EventKind.RESTORED, pair, cause=cause, observe=pair.state is PairState.WOULD_DISABLE)
        self._idle(pair)
        return [event] if reported else []

    @staticmethod
    def _idle(pair: Pair) -> None:
        pair.state = PairState.IDLE
        pair.disabled_at = pair.until = pair.calm_since = None
        pair.node, pair.missing = {}, 0

    def _close_silent(self, seen: set[PairKey], now: float) -> None:
        """Без данных час — запись закрывается молча: что с адресом, неизвестно, а
        держать его отключённым вслепую нельзя. Удержание без данных тоже не идёт."""
        for key, pair in self.pairs.items():
            if key not in seen and pair.state is not PairState.IDLE and now - pair.last_data_at > NO_DATA_CLOSE_SEC:
                self._idle(pair)
        self._above = {key: since for key, since in self._above.items() if key in seen}
        self._pauses = {
            relay_id: pause for relay_id, pause in self._pauses.items()
            if now - pause.last_seen <= NO_DATA_CLOSE_SEC
        }

    def _forget_old(self, now: float) -> None:
        edge = now - STRIKE_WINDOW_SEC
        for key, pair in list(self.pairs.items()):
            pair.strikes = [moment for moment in pair.strikes if moment > edge]
            held = pair.hold_until is not None and now < pair.hold_until
            if pair.state is PairState.IDLE and not pair.strikes and not held:
                del self.pairs[key]

    def _chronic(self, policy: QuarantinePolicy, mode: Mode) -> list[Event]:
        """Адрес, пара которого отключается снова и снова, — одно сообщение на адрес."""
        by_target: dict[str, list[Pair]] = {}
        for pair in self.pairs.values():
            if len(pair.strikes) >= policy.chronic_strikes:
                by_target.setdefault(pair.target, []).append(pair)
            else:
                pair.chronic_notified = False
        events = []
        for target, pairs in sorted(by_target.items()):
            known = any(pair.chronic_notified for pair in pairs)
            for pair in pairs:
                pair.chronic_notified = True
            if known:
                continue
            worst = max(pairs, key=lambda p: len(p.strikes))
            events.append(Event(
                EventKind.CHRONIC, worst.relay_id, worst.relay_name, target,
                strikes=len(worst.strikes), observe=mode is Mode.OBSERVE,
                relays=tuple(sorted(pair.relay_name for pair in pairs)),
            ))
        return events

    @staticmethod
    def _strike(pair: Pair, now: float) -> None:
        pair.strikes = [moment for moment in pair.strikes if moment > now - STRIKE_WINDOW_SEC] + [now]

    @staticmethod
    def _event(kind: EventKind, pair: Pair, term_sec: float = 0, cause: Optional[RestoreCause] = None,
               observe: bool = False) -> Event:
        return Event(
            kind, pair.relay_id, pair.relay_name, pair.target, pair.reason_kind, pair.loss_pct, pair.downs,
            term_sec=term_sec, sessions=pair.sessions, strikes=len(pair.strikes), cause=cause, observe=observe,
        )
