"""Автоотключение адресов назначения на релеях: когда выводить и когда возвращать.

Релей меряет потери до своих бэкендов и считает их падения (loss_registry).
Адрес, до которого релей не доходит, HAProxy держит UP и продолжает слать на
него клиентов. Здесь решается, какие пары «релей → адрес» вывести из
балансировки; исполняет нода (backend_quarantine_runner → её stats socket).

Пара, а не адрес: фильтрация на пути зависит от релея, с другого релея тот же
адрес работает.

- Отключить — одно из трёх: сервер не отвечает клиентам дольше удержания
  (`stalled`), потери не ниже порога дольше удержания, падений за полчаса не
  меньше порога. Потери ниже LOSS_ALONE_PCT считаются, только когда их
  подтверждает второй признак — падение или `stalled`: сервер, до которого
  теряется часть подключений, бывает рабочим, и снимать его незачем.
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
# Столько нода помнит падения сервера (DOWNS_WINDOW_SEC счётчика падений на ноде)
DOWNS_WINDOW_SEC = 1800
# С таких потерь адрес отключается и без второго признака
LOSS_ALONE_PCT = 50.0
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
    STALLED = "stalled"


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
    # Почему не отключено: {"error": код}; отключено не во всех правилах: {"partial": код}
    node: dict = field(default_factory=dict)
    missing: int = 0
    sessions: int = 0
    chronic_notified: bool = False
    # Нода уже снимала отключение в этом эпизоде (сосед упал) — второй раз об этом не пишется
    guard_reported: bool = False

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


def _usable_downs(reading: LossReading, mode: TargetMode, trusted: bool) -> int:
    return (reading.downs or 0) if trusted and _counts_downs(mode) else 0


def _loss_counts(reading: LossReading, mode: TargetMode, policy: QuarantinePolicy, downs: int) -> bool:
    trigger = _trigger_loss(mode, policy)
    if reading.loss_pct < trigger:
        return False
    return reading.loss_pct >= max(trigger, LOSS_ALONE_PCT) or bool(reading.stalled) or downs > 0


def _is_bad(reading: LossReading, mode: TargetMode, policy: QuarantinePolicy, downs_trusted: bool) -> bool:
    """Плохая сейчас: хотя бы один повод отключать налицо (удержание ещё не прошло)."""
    downs = _usable_downs(reading, mode, downs_trusted)
    return bool(reading.stalled) or downs >= policy.downs_threshold or _loss_counts(reading, mode, policy, downs)


@dataclass
class _Observed:
    key: PairKey
    reading: LossReading
    mode: TargetMode


@dataclass
class QuarantineTracker:
    pairs: dict[PairKey, Pair] = field(default_factory=dict)
    # Пара → с какого момента потери дают повод отключать
    _above: dict[PairKey, float] = field(default_factory=dict)
    # Пара → с какого момента сервер не отвечает клиентам
    _stalled: dict[PairKey, float] = field(default_factory=dict)
    _pauses: dict[int, RelayPause] = field(default_factory=dict)
    # Релей → до какого момента его падения не повод отключать: пока у релея не было
    # сети, HAProxy ронял все серверы, и эти падения лежат в окне ноды ещё полчаса
    _downs_ignored_until: dict[int, float] = field(default_factory=dict)

    def evaluate(self, snapshots: list[RelaySnapshot], rules: TargetRules, exempt: Exemptions,
                 policy: QuarantinePolicy, mode: Mode, now: float) -> list[Event]:
        events: list[Event] = []
        seen: set[PairKey] = set()
        # Без правил и исключений каждый адрес учитывается как обычный — на парке в
        # тысячи релеев это десятки тысяч сэкономленных проверок за цикл
        plain = not (rules.ips or rules.endpoints or exempt.server_ids or exempt.ips or exempt.endpoints)
        for snapshot in snapshots:
            touched = []
            considered = 0
            downs_trusted = self._downs_trusted(snapshot.server_id, now)
            for reading in snapshot.readings:
                key = (snapshot.server_id, reading.key)
                target_mode = TargetMode.TRACKED if plain else rules.mode(reading)
                if not plain and (target_mode is TargetMode.HIDDEN or exempt.covers(snapshot.server_id, reading)):
                    self._forget_timers(key)
                    events += self._release(key, RestoreCause.EXEMPT)
                    continue
                # downs=None — адреса нет в HAProxy релея (цель DNAT) или нода старая: отключать нечем
                if reading.downs is None or reading.samples < MIN_SAMPLES:
                    continue
                considered += 1
                # Здоровому адресу без записи и таймера считать нечего — таких почти все
                if (key in self.pairs or key in self._above or key in self._stalled
                        or _is_bad(reading, target_mode, policy, downs_trusted)):
                    touched.append(_Observed(key, reading, target_mode))
            paused = self._relay_paused(snapshot, touched, considered, policy, now, events)
            for item in touched:
                seen.add(item.key)
                events += self._step(item, snapshot.name, policy, mode, paused, now)
        self._close_silent(seen, now)
        self._forget_old(now)
        return events + self._chronic(policy, mode)

    def desired(self) -> dict[int, list[str]]:
        """Релей → адреса, которые он должен держать отключёнными; худшие первыми —
        нода раздаёт слоты доли в этом порядке. Сервер без ответов бесполезен
        целиком, при любых потерях до него."""
        wanted: dict[int, list[Pair]] = {}
        for pair in self.pairs.values():
            if pair.state in WANTED_STATES:
                wanted.setdefault(pair.relay_id, []).append(pair)
        return {
            relay_id: [pair.target for pair in sorted(
                pairs, key=lambda p: (p.reason_kind is not ReasonKind.STALLED, -p.loss_pct, p.target),
            )]
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
        self._forget_timers(key)
        return events

    def clear(self, now: float, cause: RestoreCause) -> list[Event]:
        """Режим сменили: всё отключённое возвращается, счёт отключений остаётся."""
        events: list[Event] = []
        for key in list(self.pairs):
            events += [event for event in self._release(key, cause) if not event.observe]
        self._above.clear()
        self._stalled.clear()
        self._pauses.clear()
        self._downs_ignored_until.clear()
        self._forget_old(now)
        return events

    def paused_relays(self) -> dict[int, RelayPause]:
        return dict(self._pauses)

    def chronic_targets(self, now: float, policy: QuarantinePolicy) -> dict[str, int]:
        """Адрес → наибольшее число отключений одной его пары за сутки."""
        edge = now - STRIKE_WINDOW_SEC
        chronic: dict[str, int] = {}
        # Снимок списком: страницу читают, пока расчёт цикла идёт в другом потоке
        for pair in list(self.pairs.values()):
            recent = sum(1 for moment in pair.strikes if moment > edge)
            if recent >= policy.chronic_strikes:
                chronic[pair.target] = max(chronic.get(pair.target, 0), recent)
        return chronic

    def _relay_paused(self, snapshot: RelaySnapshot, touched: list[_Observed], considered: int,
                      policy: QuarantinePolicy, now: float, events: list[Event]) -> bool:
        """considered — все учитываемые пары релея; плохие бывают только среди touched."""
        downs_trusted = self._downs_trusted(snapshot.server_id, now)
        bad = sum(1 for item in touched if self._counts_as_bad(item, policy, downs_trusted))
        pause = self._pauses.get(snapshot.server_id)
        if pause is None:
            if bad < RELAY_TROUBLE_MIN_PAIRS or bad <= considered * RELAY_TROUBLE_ENTER_SHARE:
                return False
            self._pauses[snapshot.server_id] = RelayPause(snapshot.name, now, now)
            events.append(Event(EventKind.RELAY_PAUSED, snapshot.server_id, snapshot.name))
        else:
            pause.last_seen = now
            if bad > considered * RELAY_TROUBLE_EXIT_SHARE:
                pause.calm_since = None
            else:
                pause.calm_since = pause.calm_since if pause.calm_since is not None else now
                if now - pause.calm_since >= policy.sustained_sec:
                    del self._pauses[snapshot.server_id]
                    self._downs_ignored_until[snapshot.server_id] = now + DOWNS_WINDOW_SEC
                    return False
        for item in touched:
            self._forget_timers(item.key)
        return True

    def _downs_trusted(self, relay_id: int, now: float) -> bool:
        return now >= self._downs_ignored_until.get(relay_id, 0.0)

    def _forget_timers(self, key: PairKey) -> None:
        self._above.pop(key, None)
        self._stalled.pop(key, None)

    def _counts_as_bad(self, item: _Observed, policy: QuarantinePolicy, downs_trusted: bool) -> bool:
        if _is_bad(item.reading, item.mode, policy, downs_trusted):
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
            if paused and pair.state in (PairState.PENDING, PairState.BLOCKED):
                # Ещё не отключённое при беде релея не отключается: решение примется заново после неё
                self._idle(pair)
        if pair is None or pair.state is PairState.IDLE:
            return self._step_idle(item, pair, relay_name, policy, mode, paused, now)
        return self._step_active(item, pair, policy, now)

    def _step_idle(self, item: _Observed, pair: Optional[Pair], relay_name: str, policy: QuarantinePolicy,
                   mode: Mode, paused: bool, now: float) -> list[Event]:
        held = pair is not None and pair.hold_until is not None and now < pair.hold_until
        if paused or held:
            self._forget_timers(item.key)
            return []
        reason = self._reason(item, policy, now)
        if reason is None:
            return []
        self._forget_timers(item.key)
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
        reading = item.reading
        # Доверие падениям спрашивается здесь, а не в начале цикла: выход из беды релея
        # в этом же цикле уже снял его
        downs = _usable_downs(reading, item.mode, self._downs_trusted(item.key[0], now))
        # Оба таймера идут каждый цикл, какой бы повод ни сработал первым
        stalled_long = self._held(self._stalled, item.key, bool(reading.stalled), policy, now)
        loss_long = self._held(self._above, item.key, _loss_counts(reading, item.mode, policy, downs), policy, now)
        if stalled_long:
            return ReasonKind.STALLED
        if loss_long:
            return ReasonKind.LOSS
        if downs >= policy.downs_threshold:
            return ReasonKind.DOWNS
        return None

    @staticmethod
    def _held(since: dict[PairKey, float], key: PairKey, holds: bool, policy: QuarantinePolicy, now: float) -> bool:
        """Повод держится дольше удержания; перерыв начинает счёт заново."""
        if not holds:
            since.pop(key, None)
            return False
        return now - since.setdefault(key, now) >= policy.sustained_sec

    def _step_active(self, item: _Observed, pair: Pair, policy: QuarantinePolicy, now: float) -> list[Event]:
        # Молчащий сервер в работе (нода отказала, режим наблюдения) затишья не начинает
        quiet = item.reading.loss_pct < _recovery_bound(item.mode, policy) and not item.reading.stalled
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
        events = self._release(item.key, RestoreCause.CALM)
        # Нода обнуляет падения адреса при возврате, но замер, снятый до него, реестр
        # считает свежим ещё STALE_AFTER_SEC: по нему пара отключилась бы заново
        pair.hold_until = now + STALE_AFTER_SEC
        return events

    def _apply_answer(self, pair: Pair, answer: dict, now: float) -> list[Event]:
        applied = answer.get("applied") or []
        skipped = answer.get("skipped") or []
        if applied:
            pair.missing = 0
            pair.node = {"partial": str(skipped[0].get("reason"))} if skipped else {}
            pair.state = PairState.DISABLED
            if pair.disabled_at is not None:
                # Тот же эпизод: нода сняла отключение (сосед упал) и поставила снова —
                # мигающий сосед не должен слать сообщение и растить счёт каждую минуту
                return []
            pair.disabled_at = now
            self._strike(pair, now)
            pair.until = now + TERMS_SEC[min(len(pair.strikes), len(TERMS_SEC)) - 1]
            pair.sessions = sum(int(row.get("sessions") or 0) for row in applied)
            return [self._event(EventKind.DISABLED, pair, term_sec=pair.until - now)]
        if not answer.get("found"):
            pair.missing += 1
            if pair.missing >= MISSING_ANSWERS_TO_CLOSE:
                self._idle(pair)
            elif pair.state is not PairState.DISABLED:
                pair.state, pair.node = PairState.BLOCKED, {"error": NOT_FOUND}
            return []
        pair.missing = 0
        reason = str(skipped[0].get("reason")) if skipped else NodeFailure.ERROR.value
        events = []
        if pair.state is PairState.DISABLED and not pair.guard_reported:
            # Нода сняла отключение сама: сервер остался последним живым в правиле
            events.append(self._event(EventKind.RESTORED, pair, cause=RestoreCause.GUARD))
            pair.guard_reported = True
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
        pair.node, pair.missing, pair.guard_reported = {}, 0, False

    def _close_silent(self, seen: set[PairKey], now: float) -> None:
        """Без данных час — запись закрывается молча: что с адресом, неизвестно, а
        держать его отключённым вслепую нельзя. Удержание без данных тоже не идёт."""
        for key, pair in self.pairs.items():
            if key not in seen and pair.state is not PairState.IDLE and now - pair.last_data_at > NO_DATA_CLOSE_SEC:
                self._idle(pair)
        self._above = {key: since for key, since in self._above.items() if key in seen}
        self._stalled = {key: since for key, since in self._stalled.items() if key in seen}
        self._pauses = {
            relay_id: pause for relay_id, pause in self._pauses.items()
            if now - pause.last_seen <= NO_DATA_CLOSE_SEC
        }
        self._downs_ignored_until = {
            relay_id: until for relay_id, until in self._downs_ignored_until.items() if until > now
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
