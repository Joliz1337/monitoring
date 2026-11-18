"""Алерт о потерях до адресов назначения релеев.

Нода-релей сама меряет потери до своих бэкендов HAProxy и целей DNAT
(node/app/services/loss_probe.py), панель собирает их со всего парка
(loss_registry). Здесь решается, когда об этом писать.

Сообщение — только при смене статуса адреса, а не по таймеру:
- «новые потери» — хотя бы один релей видит потери не ниже порога дольше
  удержания; открывается эпизод сразу с уровнем, который есть сейчас;
- «усилились» — эпизод перешёл на уровень выше (50%, 90%), тоже с удержанием;
  улучшения внутри эпизода не пишутся;
- «снизились» — у всех релеев потери ниже половины порога непрерывно всё время
  затишья. Любой всплеск выше половины порога сбрасывает отсчёт: мигающий адрес
  остаётся в эпизоде, а не шлёт пары «есть / нет»;
- нет данных (релеи выключены, нода перезапускается) — не изменение: эпизод
  ждёт, а без данных дольше часа закрывается молча;
- напоминание об открытых эпизодах — только если включено;
- адрес вне учёта с галочкой «только полные потери» живёт по своей политике:
  один уровень от TOTAL_LOSS_PCT («почти не отвечает»), а «снизились» — когда
  все релеи ниже него всё затишье.

Проблема на релее. Адрес, который теряет один релей, пока остальные до него
доходят, — беда релея или его пути, а не адреса: эпизод запоминает этот релей.
Когда таких адресов у релея набирается RELAY_MIN_TARGETS, вместо блока на
каждый адрес приходит одно сообщение про релей. Адреса, присоединившиеся в
первые RELAY_JOIN_WINDOW_SEC, молчат, позже — строка «+N»; «снова доходит» —
когда закрылся последний его адрес. Начинают терять адрес и другие релеи —
адрес выходит из группы и приходит обычными «новыми потерями».

Уровень сообщений (LossScope). Эпизоды ведутся одинаково при любом уровне, он
решает только, что уйдёт в Telegram: всё; только общее — адрес теряют не меньше
заданной доли его релеев либо релей один теряет не меньше той же доли своих
адресов; только адрес, который почти не отвечает этой доле релеев. Остальные
события выходят с пометкой quiet — их получает история алертов. О конце пишется,
только если писали о начале; узкий сбой, ставший общим, объявляется в этот момент.

Упавший сервер. Про сервер, о недоступности которого панель сообщила, потери до
его адресов — та же новость второй раз. Такой адрес придержан (`held`): эпизод
не открывается, уровень не растёт, напоминаний нет; «снизились» по уже
объявленному эпизоду приходит как обычно. Придержка живёт ещё OUTAGE_SETTLE_SEC
после возвращения сервера (OutageWatch) — окна замеров релеев помнят простой;
замеры самого вернувшегося сервера это время не учитываются.

Эпизоды и проблемы релеев хранятся в базе, поэтому перезапуск панели уже
известные потери повторно не присылает.
"""

import html
from collections import Counter
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Optional

from app.services.loss_registry import TOTAL_LOSS_PCT, LossReading, RelaySnapshot, TargetMode, TargetRules

# Нода проверяет адрес раз в 2 с: 30 попыток — минута данных. Доля потерь по
# меньшему числу — шум первых попыток после старта агента или смены конфига
MIN_SAMPLES = 30
RECOVERY_RATIO = 0.5
SEVERITY_BOUNDS = (50.0, 90.0)
# Уровень «почти не отвечает» — им сразу открывается эпизод «только полных потерь»
TOTAL_LEVEL = len(SEVERITY_BOUNDS) + 1
NO_DATA_CLOSE_SEC = 3600
# Лимит Telegram — 4096 символов; запас под заголовок части и разметку
MESSAGE_LIMIT = 3800
# Один-два адреса, которые теряет только один релей, бывают и у здорового
# релея (путь до конкретной сети); три и больше — уже проблема самого релея
RELAY_MIN_TARGETS = 3
# Пока авария разгорается, новые адреса релея присоединяются молча. Дольше —
# нельзя: пара давно висящих адресов глушила бы все новые беды релея
RELAY_JOIN_WINDOW_SEC = 900
RELAY_OWNERS_SHOWN = 12
# С какой доли проблема считается общей: адрес теряет половина его релеев,
# релей один теряет половину своих адресов
WIDE_SHARE_PCT = 50
WIDE_SHARE_BOUNDS = (10, 100)


class LossScope(str, Enum):
    """О чём писать в Telegram. В историю алертов идёт всё при любом уровне."""
    ALL = "all"
    # Только общее: одиночный сбой на паре релеев — обычная работа парка
    WIDE = "wide"
    # Только адрес, который почти не отвечает большинству своих релеев
    DEAD = "dead"


def parse_scope(value: object) -> LossScope:
    """Запись настроек, сделанная до появления уровня, значит «все события»."""
    try:
        return LossScope(value)
    except ValueError:
        return LossScope.ALL


# Окно замеров ноды — 60 попыток раз в 2 с — помнит простой ещё две минуты после
# него: и релеи до вернувшегося сервера, и он сам, если пережил обрыв сети.
# Запас — на сервисы, которые поднимаются позже агента ноды
OUTAGE_SETTLE_SEC = 300
# Нода замолчала, а проверка доступности ещё не вынесла вердикт — о потерях до
# неё ждём его. Начало короче минимального удержания (60 с): «новые потери» не
# успеют обогнать алерт о недоступности. Конец — чтобы проверка доступности,
# настроенная на редкий опрос, не задерживала алерт о потерях
VERDICT_WAIT_FROM_SEC = 45
VERDICT_WAIT_UNTIL_SEC = 300


@dataclass(frozen=True)
class RelayReading:
    relay_id: int
    relay_name: str
    reading: LossReading


@dataclass(frozen=True)
class LossPolicy:
    threshold: float
    sustained_sec: float
    calm_sec: float
    reminder_sec: float = 0
    first_level: int = 1
    recovery_ratio: float = RECOVERY_RATIO
    scope: LossScope = LossScope.ALL
    wide_share_pct: int = WIDE_SHARE_PCT

    def level_bounds(self) -> list[float]:
        return [self.threshold] + [bound for bound in SEVERITY_BOUNDS if bound > self.threshold]

    def top_level(self) -> int:
        return self.first_level + len(self.level_bounds()) - 1

    def recovery_bound(self) -> float:
        return self.threshold * self.recovery_ratio

    def total_only(self) -> "LossPolicy":
        # Частичные потери на таком адресе — норма, поэтому «снизились» уже ниже
        # самого порога; от мигания у порога защищает непрерывное затишье
        return replace(self, threshold=TOTAL_LOSS_PCT, first_level=TOTAL_LEVEL, recovery_ratio=1.0)


@dataclass
class Episode:
    level: int
    opened_at: float
    last_data_at: float
    notified_at: float
    calm_since: Optional[float] = None
    blamed_relay_id: Optional[int] = None
    # Писали ли о начале в Telegram: о том, о чём молчали, не пишется и дальше
    announced: bool = True


@dataclass
class RelayIncident:
    relay_name: str
    opened_at: float
    notified_at: float
    # Сколько адресов релея получатель уже знает: рост сверх этого — «+N»
    reported: int
    announced: bool = True


class EventKind(str, Enum):
    NEW = "new"
    WORSE = "worse"
    STILL = "still"
    RECOVERED = "recovered"


class RelayEventKind(str, Enum):
    OPEN = "relay_open"
    GREW = "relay_grew"
    STILL = "relay_still"
    RECOVERED = "relay_recovered"


@dataclass(frozen=True)
class LossEvent:
    kind: EventKind
    target: str
    level: int
    relays: tuple[RelayReading, ...]
    blamed_relay_id: Optional[int] = None
    # Только в историю алертов: выбранному уровню сообщений событие не положено
    quiet: bool = False


@dataclass(frozen=True)
class RelayEvent:
    kind: RelayEventKind
    relay_id: int
    relay_name: str
    count: int
    readings: tuple[LossReading, ...] = ()
    other_relays: int = 0
    added: int = 0
    quiet: bool = False


AlertEvent = LossEvent | RelayEvent


def collect_observations(snapshots: list[RelaySnapshot], excluded: set[int],
                         rules: TargetRules = TargetRules()) -> dict[str, list[RelayReading]]:
    """Адрес → что видит каждый релей; окно короче MIN_SAMPLES и скрытые
    адреса не считаются."""
    observations: dict[str, list[RelayReading]] = {}
    for snapshot in snapshots:
        if snapshot.server_id in excluded:
            continue
        for reading in snapshot.readings:
            if reading.samples < MIN_SAMPLES or rules.mode(reading) is TargetMode.HIDDEN:
                continue
            observations.setdefault(reading.key, []).append(
                RelayReading(snapshot.server_id, snapshot.name, reading)
            )
    return observations


def total_only_targets(observations: dict[str, list[RelayReading]], rules: TargetRules) -> frozenset[str]:
    return frozenset(
        target for target, relays in observations.items()
        if rules.mode(relays[0].reading) is TargetMode.TOTAL_ONLY
    )


def held_targets(observations: dict[str, list[RelayReading]], ips: frozenset[str]) -> frozenset[str]:
    """Адреса назначения на перечисленных IP — все их порты."""
    return frozenset(target for target, relays in observations.items() if relays[0].reading.ip in ips)


@dataclass
class OutageWatch:
    """Серверы, о недоступности которых панель сообщила, и те, что вернулись
    меньше OUTAGE_SETTLE_SEC назад: окна замеров ещё полны их простоя."""
    _returned_at: dict[int, float] = field(default_factory=dict)

    def update(self, was_down: frozenset[int], down: frozenset[int], now: float) -> frozenset[int]:
        """was_down — недоступные до обхода парка, down — после него."""
        for server_id in was_down - down:
            self._returned_at[server_id] = now
        self._returned_at = {
            server_id: returned_at for server_id, returned_at in self._returned_at.items()
            if server_id not in down and now - returned_at < OUTAGE_SETTLE_SEC
        }
        return down | frozenset(self._returned_at)


def blamed_relay(relays: tuple[RelayReading, ...], policy: LossPolicy) -> Optional[int]:
    """Релей, который один теряет адрес, пока остальные до него доходят."""
    lossy = [r.relay_id for r in relays if r.reading.loss_pct >= policy.recovery_bound()]
    if len(lossy) == 1 and len(relays) > 1:
        return lossy[0]
    return None


@dataclass
class LossTracker:
    episodes: dict[str, Episode] = field(default_factory=dict)
    incidents: dict[int, RelayIncident] = field(default_factory=dict)
    # адрес → (релей, уровень) → с какого момента потери держатся на уровне
    _above: dict[str, dict[tuple[int, int], float]] = field(default_factory=dict)

    def reset(self) -> None:
        self.episodes.clear()
        self.incidents.clear()
        self._above.clear()

    def evaluate(self, observations: dict[str, list[RelayReading]], policy: LossPolicy,
                 now: float, total_only: frozenset[str] = frozenset(),
                 held: frozenset[str] = frozenset()) -> list[AlertEvent]:
        """total_only — адреса с галочкой «только полные потери» (total_only_targets),
        held — адреса серверов, о недоступности которых уже сказано (held_targets)."""
        total_policy = policy.total_only()

        def policy_for(target: str) -> LossPolicy:
            return total_policy if target in total_only else policy

        self._update_timers(observations, policy_for, now)
        events = []
        for target in sorted(set(observations) | set(self.episodes)):
            relays = tuple(sorted(observations.get(target, []), key=lambda r: -r.reading.loss_pct))
            event = self._step(target, relays, policy_for(target), now, target in held)
            if event is not None:
                events.append(event)
        return self._group_by_relay(events, observations, policy, now)

    def _update_timers(self, observations: dict[str, list[RelayReading]],
                       policy_for: Callable[[str], LossPolicy], now: float) -> None:
        active = {}
        for target, relays in observations.items():
            policy = policy_for(target)
            previous = self._above.get(target, {})
            timers = {}
            for relay in relays:
                for level, bound in enumerate(policy.level_bounds(), start=policy.first_level):
                    if relay.reading.loss_pct >= bound:
                        key = (relay.relay_id, level)
                        timers[key] = previous.get(key, now)
            if timers:
                active[target] = timers
        self._above = active

    def _level(self, target: str, now: float, min_age: float, skip_relay: Optional[int] = None) -> int:
        return max(
            (level for (relay_id, level), since in self._above.get(target, {}).items()
             if relay_id != skip_relay and now - since >= min_age),
            default=0,
        )

    def _reach(self, target: str, watchers: int, policy: LossPolicy, now: float) -> int:
        """Наивысший уровень, который держит не меньше заданной доли релеев адреса.
        Считается релей, продержавший уровень всё удержание — как при открытии
        эпизода: минута без ответа всем релеям узкий сбой общим не делает."""
        timers = self._above.get(target, {})
        for level in range(policy.top_level(), policy.first_level - 1, -1):
            lossy = sum(
                1 for (_, held_level), since in timers.items()
                if held_level == level and now - since >= policy.sustained_sec
            )
            if lossy and lossy * 100 >= policy.wide_share_pct * watchers:
                return level
        return 0

    def _worth_telling(self, target: str, watchers: int, policy: LossPolicy, now: float) -> bool:
        if policy.scope is LossScope.ALL:
            return True
        needed = policy.top_level() if policy.scope is LossScope.DEAD else policy.first_level
        return self._reach(target, watchers, policy, now) >= needed

    def _step(self, target: str, relays: tuple[RelayReading, ...], policy: LossPolicy,
              now: float, held: bool = False) -> Optional[LossEvent]:
        # Придержанный адрес не открывает эпизод, не повышает уровень и не шлёт
        # напоминаний. Таймеры идут: потери, пережившие придержку, объявятся сразу
        level = 0 if held else self._level(target, now, policy.sustained_sec)
        episode = self.episodes.get(target)
        if episode is None:
            if level == 0:
                return None
            # Удержание пройдено на пороге, а назвать надо то, что сейчас: окно ноды
            # доползает до 100% позже, чем до порога, и при обрыве за «потерями»
            # через минуту пришло бы «почти не отвечает»
            level = self._level(target, now, 0)
            blamed = blamed_relay(relays, policy)
            told = self._worth_telling(target, len(relays), policy, now)
            self.episodes[target] = Episode(level=level, opened_at=now, last_data_at=now,
                                            notified_at=now, blamed_relay_id=blamed, announced=told)
            return LossEvent(EventKind.NEW, target, level, relays, blamed, quiet=not told)

        blamed = episode.blamed_relay_id
        if blamed is not None:
            if all(r.relay_id != blamed for r in relays):
                # Без виноватого релея остальные выглядели бы затишьем — ждём его
                relays = ()
            elif not held and self._level(target, now, policy.sustained_sec, skip_relay=blamed):
                told = self._worth_telling(target, len(relays), policy, now)
                episode.blamed_relay_id = None
                episode.level = self._level(target, now, 0)
                episode.last_data_at = now
                episode.notified_at = now
                episode.calm_since = None
                episode.announced = episode.announced or told
                return LossEvent(EventKind.NEW, target, episode.level, relays, quiet=not told)

        if not relays:
            episode.calm_since = None
            if now - episode.last_data_at >= NO_DATA_CLOSE_SEC:
                del self.episodes[target]
            return None
        episode.last_data_at = now

        calm = all(r.reading.loss_pct < policy.recovery_bound() for r in relays)
        if not held and not episode.announced and not calm and self._worth_telling(target, len(relays), policy, now):
            # Узкий сбой стал общим либо уровень сообщений подняли: о начале ещё не писали.
            # У придержанного адреса «стал общим» — это упавший сервер, о котором уже сказано
            episode.announced = True
            episode.level = max(episode.level, self._level(target, now, 0))
            episode.calm_since = None
            episode.notified_at = now
            return LossEvent(EventKind.NEW, target, episode.level, relays, blamed)

        if level > episode.level:
            episode.level = level
            episode.calm_since = None
            episode.notified_at = now
            return LossEvent(EventKind.WORSE, target, level, relays, blamed, quiet=not episode.announced)

        if calm:
            if episode.calm_since is None:
                episode.calm_since = now
            if now - episode.calm_since >= policy.calm_sec:
                del self.episodes[target]
                return LossEvent(EventKind.RECOVERED, target, 0, relays, blamed, quiet=not episode.announced)
            return None
        episode.calm_since = None

        if not held and episode.announced and policy.reminder_sec and now - episode.notified_at >= policy.reminder_sec:
            episode.notified_at = now
            return LossEvent(EventKind.STILL, target, episode.level, relays, blamed)
        return None

    def _group_by_relay(self, events: list[LossEvent], observations: dict[str, list[RelayReading]],
                        policy: LossPolicy, now: float) -> list[AlertEvent]:
        """События адресов, которые теряет один релей, — одним событием про релей."""
        blamed_targets: dict[int, list[str]] = {}
        for target, episode in self.episodes.items():
            if episode.blamed_relay_id is not None:
                blamed_targets.setdefault(episode.blamed_relay_id, []).append(target)
        events_by_relay: dict[int, list[LossEvent]] = {}
        for event in events:
            if event.blamed_relay_id is not None:
                events_by_relay.setdefault(event.blamed_relay_id, []).append(event)
        names: dict[int, str] = {}
        # Релей → сколько адресов он проверяет: от этого считается доля потерянных
        watched: Counter[int] = Counter()
        for relays in observations.values():
            for relay in relays:
                names[relay.relay_id] = relay.relay_name
                watched[relay.relay_id] += 1

        result: list[AlertEvent] = [e for e in events if e.blamed_relay_id is None]
        # Релей из одних событий — закрылся его последний адрес: «снизились» по нему тоже событие
        for relay_id in sorted(set(blamed_targets) | set(self.incidents) | set(events_by_relay)):
            targets = blamed_targets.get(relay_id, [])
            own_events = events_by_relay.get(relay_id, [])
            incident = self.incidents.get(relay_id)
            # События адресов проблемы наружу не выходят — их заменяет событие релея. Поэтому
            # о проблеме пишется и тогда, когда писать положено хотя бы об одном её адресе
            told = self._relay_worth_telling(len(targets), watched[relay_id], policy) or any(
                self.episodes[target].announced for target in targets
            )

            if incident is None:
                if len(targets) < RELAY_MIN_TARGETS:
                    result.extend(own_events)
                    continue
                incident = RelayIncident(names.get(relay_id, f"#{relay_id}"), now, now, len(targets), told)
                self.incidents[relay_id] = incident
                result.append(self._relay_event(RelayEventKind.OPEN, relay_id, incident, targets, observations))
                continue

            if not targets:
                del self.incidents[relay_id]
                # Адреса закрылись без данных (релей пропал) — проблема закрывается так же молча
                if any(e.kind is EventKind.RECOVERED for e in own_events):
                    result.append(RelayEvent(RelayEventKind.RECOVERED, relay_id, incident.relay_name,
                                             incident.reported, quiet=not incident.announced))
                continue

            if not incident.announced and told:
                # Проблема релея доросла до общей либо уровень сообщений подняли: о начале ещё не писали
                incident.announced = True
                incident.reported = len(targets)
                incident.notified_at = now
                result.append(self._relay_event(RelayEventKind.OPEN, relay_id, incident, targets, observations))
                continue

            if len(targets) > incident.reported:
                added = len(targets) - incident.reported
                incident.reported = len(targets)
                if now - incident.opened_at >= RELAY_JOIN_WINDOW_SEC:
                    incident.notified_at = now
                    result.append(self._relay_event(RelayEventKind.GREW, relay_id, incident, targets,
                                                    observations, added))
                continue

            if incident.announced and policy.reminder_sec and now - incident.notified_at >= policy.reminder_sec:
                incident.notified_at = now
                result.append(self._relay_event(RelayEventKind.STILL, relay_id, incident, targets, observations))
        return result

    @staticmethod
    def _relay_worth_telling(lost: int, watched: int, policy: LossPolicy) -> bool:
        """Проблема релея общая, когда он один теряет не меньше заданной доли своих адресов."""
        if policy.scope is LossScope.ALL:
            return True
        if policy.scope is LossScope.DEAD:
            return False
        return watched > 0 and lost * 100 >= policy.wide_share_pct * watched

    @staticmethod
    def _relay_event(kind: RelayEventKind, relay_id: int, incident: RelayIncident, targets: list[str],
                     observations: dict[str, list[RelayReading]], added: int = 0) -> RelayEvent:
        readings = []
        other_relays = set()
        for target in targets:
            for relay in observations.get(target, ()):
                if relay.relay_id == relay_id:
                    readings.append(relay.reading)
                else:
                    other_relays.add(relay.relay_id)
        readings.sort(key=lambda reading: -reading.loss_pct)
        return RelayEvent(kind, relay_id, incident.relay_name, len(targets), tuple(readings),
                          len(other_relays), added, quiet=not incident.announced)


# ---------------------------------------------------------------------------
# Тексты: проблемы релеев, затем адреса, внутри адреса — релеи
# ---------------------------------------------------------------------------

LEVEL_NAMES = {
    "ru": {1: "потери", 2: "сильные потери", 3: "почти не отвечает"},
    "en": {1: "loss", 2: "heavy loss", 3: "barely reachable"},
}
# Порядок ключей — порядок разделов в сообщении
SECTION_TITLES = {
    RelayEventKind.OPEN: ("Проблема на релее", "Relay problem"),
    RelayEventKind.GREW: ("Проблема на релее растёт", "Relay problem grew"),
    EventKind.NEW: ("Новые потери", "New loss"),
    EventKind.WORSE: ("Потери усилились", "Loss got worse"),
    RelayEventKind.STILL: ("Проблема на релее не ушла", "Relay problem persists"),
    EventKind.STILL: ("Всё ещё есть потери", "Loss persists"),
    RelayEventKind.RECOVERED: ("Проблема на релее ушла", "Relay problem resolved"),
    EventKind.RECOVERED: ("Потери снизились", "Loss recovered"),
}
RECOVERY_KINDS = (EventKind.RECOVERED, RelayEventKind.RECOVERED)
REMINDER_KINDS = (EventKind.STILL, RelayEventKind.STILL)


def _lang(lang: str) -> str:
    return "ru" if lang == "ru" else "en"


def _rtt_text(reading: LossReading, ru: bool) -> str:
    if reading.rtt_ms is None:
        return "нет ответа" if ru else "no reply"
    return f"{reading.rtt_ms:.0f} {'мс' if ru else 'ms'}"


def _loss_text(reading: LossReading, ru: bool) -> str:
    """Процент потерь; у сервера, молчащего на проверку «как клиент», процента нет —
    подключения до него доходят."""
    if reading.answers is False:
        return "не отвечает как клиент" if ru else "no reply to the client check"
    return f"{reading.loss_pct:g}%"


def _relay_lines(event: LossEvent, policy: LossPolicy, ru: bool) -> list[str]:
    if event.kind is EventKind.RECOVERED:
        worst = max(r.reading.loss_pct for r in event.relays)
        count = len(event.relays)
        return [f"• {count} {'релеев' if ru else 'relays'}, {'максимум' if ru else 'max'} {worst:g}%"]
    quiet_bound = policy.recovery_bound()
    lossy = [r for r in event.relays if r.reading.loss_pct >= quiet_bound]
    lines = [
        f"• {html.escape(r.relay_name)} — {_loss_text(r.reading, ru)} ({_rtt_text(r.reading, ru)})"
        for r in lossy
    ]
    quiet = len(event.relays) - len(lossy)
    if quiet:
        lines.append(f"• {'ещё' if ru else 'and'} {quiet} {'без потерь' if ru else 'without loss'}")
    return lines


def _address_header(event: LossEvent, owners: dict[str, str], lang: str) -> str:
    ip = event.target.rpartition(":")[0]
    header = f"<b>{html.escape(event.target)}</b>"
    if ip in owners:
        header += f" · {html.escape(owners[ip])}"
    if event.kind in (EventKind.NEW, EventKind.WORSE):
        header += f" — {LEVEL_NAMES[lang][event.level]}"
    return header


def _addresses(count: int, ru: bool) -> str:
    if not ru:
        return f"{count} address" if count == 1 else f"{count} addresses"
    # После «до» — родительный падеж: до 1 адреса, до 21 адреса, до 5 адресов
    return f"{count} {'адреса' if count % 10 == 1 and count % 100 != 11 else 'адресов'}"


def _loss_range(readings: tuple[LossReading, ...]) -> str:
    low = min(r.loss_pct for r in readings)
    high = max(r.loss_pct for r in readings)
    return f"{high:g}%" if low == high else f"{low:g}–{high:g}%"


def _relay_summary(event: RelayEvent, ru: bool) -> str:
    """Что с релеем — без имени: общая часть сообщения и строки истории."""
    if event.kind is RelayEventKind.RECOVERED:
        if ru:
            return f"снова доходит до {_addresses(event.count, ru)}"
        return f"reaches {_addresses(event.count, ru)} again"
    if ru:
        lead = "потери уже до" if event.kind is RelayEventKind.GREW else "потери до"
    else:
        lead = "loss to"
    text = f"{lead} {_addresses(event.count, ru)}"
    if event.added:
        text += f" (+{event.added})"
    if event.readings:
        text += f", {_loss_range(event.readings)}"
    return text


def _owner_groups(readings: tuple[LossReading, ...], owners: dict[str, str], ru: bool) -> list[str]:
    counts = Counter(owners.get(r.ip, r.key) for r in readings)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    groups = [html.escape(name) + (f" ×{count}" if count > 1 else "") for name, count in ranked]
    if len(groups) > RELAY_OWNERS_SHOWN:
        hidden = len(groups) - RELAY_OWNERS_SHOWN
        groups = groups[:RELAY_OWNERS_SHOWN] + [f"{'ещё' if ru else 'and'} {hidden}"]
    return groups


def _relay_block(event: RelayEvent, owners: dict[str, str], ru: bool) -> list[str]:
    lines = [f"<b>{html.escape(event.relay_name)}</b> — {_relay_summary(event, ru)}"]
    if event.other_relays:
        reach = "другие релеи до них доходят" if ru else "other relays reach them"
        lines.append(f"• {reach}: {event.other_relays}")
    if event.readings:
        lines.append("• " + " · ".join(_owner_groups(event.readings, owners, ru)))
    return lines


def _block(event: AlertEvent, owners: dict[str, str], policy: LossPolicy, lang: str) -> list[str]:
    ru = lang == "ru"
    if isinstance(event, RelayEvent):
        return _relay_block(event, owners, ru)
    return [_address_header(event, owners, lang), *_relay_lines(event, policy, ru)]


def _rank(event: AlertEvent) -> float:
    if isinstance(event, RelayEvent):
        return -event.count
    return -max((r.reading.loss_pct for r in event.relays), default=0)


def _pack(sections: list[tuple[str, list[str]]], limit: int) -> list[str]:
    """Блоки адресов — в части не длиннее limit; блок делится, только если сам
    длиннее лимита, и тогда его заголовок повторяется в следующей части."""
    chunks: list[str] = []
    current = ""
    current_section = None

    def flush():
        nonlocal current, current_section
        if current:
            chunks.append(current.rstrip("\n"))
        current, current_section = "", None

    for section_title, block in sections:
        header, *relay_lines = block
        prefix = "" if current_section == section_title else f"\n<b>{section_title}</b>\n"
        text = prefix + "\n".join(block) + "\n"
        if current and len(current) + len(text) > limit:
            flush()
            prefix = f"\n<b>{section_title}</b>\n"
            text = prefix + "\n".join(block) + "\n"
        if len(text) <= limit:
            current += text
            current_section = section_title
            continue
        # Один адрес длиннее сообщения: сотни релеев под ним
        piece = prefix + header + "\n"
        for line in relay_lines:
            if len(piece) + len(line) + 1 > limit:
                current += piece
                flush()
                piece = f"\n<b>{section_title}</b>\n{header}\n"
            piece += line + "\n"
        current += piece
        current_section = section_title
    flush()
    return chunks


def format_digest(events: list[AlertEvent], owners: dict[str, str], policy: LossPolicy,
                  lang: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Все изменения проверки — одним сообщением, при нехватке места — частями.
    События, оставленные уровнем сообщений только истории, сюда не идут."""
    events = [event for event in events if not event.quiet]
    if not events:
        return []
    lang = _lang(lang)
    ru = lang == "ru"
    ranked = sorted(events, key=_rank)
    sections = [
        (titles[0 if ru else 1], _block(e, owners, policy, lang))
        for kind, titles in SECTION_TITLES.items()
        for e in ranked if e.kind is kind
    ]
    minutes = max(1, round(policy.sustained_sec / 60))
    title = "Потери до адресов" if ru else "Packet loss to addresses"
    subtitle = (f"порог {policy.threshold:g}%, удержание {minutes} мин" if ru
                else f"threshold {policy.threshold:g}%, sustained {minutes} min")
    emoji = "\U0001f7e2" if all(e.kind in RECOVERY_KINDS for e in events) else "\U0001f7e1"
    chunks = _pack(sections, limit)
    total = len(chunks)
    return [
        f"{emoji} <b>{title}</b>{f' ({index}/{total})' if total > 1 else ''}\n{subtitle}\n{chunk}"
        for index, chunk in enumerate(chunks, start=1)
    ]


def history_text(event: AlertEvent, owners: dict[str, str], lang: str) -> str:
    """Одна строка для истории алертов — без разметки Telegram."""
    lang = _lang(lang)
    ru = lang == "ru"
    title = SECTION_TITLES[event.kind][0 if ru else 1]
    if isinstance(event, RelayEvent):
        return f"{title}: {event.relay_name} — {_relay_summary(event, ru)}"
    ip = event.target.rpartition(":")[0]
    where = f"{event.target} ({owners[ip]})" if ip in owners else event.target
    if event.kind is EventKind.RECOVERED:
        return f"{title}: {where}"
    parts = ", ".join(f"{r.relay_name} {_loss_text(r.reading, ru)}" for r in event.relays[:5])
    return f"{title}: {where} — {LEVEL_NAMES[lang][event.level]} ({parts})"


@dataclass(frozen=True)
class HistoryRecord:
    server_id: int
    server_name: str
    recovered: bool
    message: str
    details: dict
    # В Telegram событие не уходило
    quiet: bool = False


def history_records(events: list[AlertEvent], owners: dict[str, str], lang: str) -> list[HistoryRecord]:
    """Строки истории алертов. Адрес пишется за релеем с худшими потерями,
    проблема релея — за ним самим: так их видно в фильтре по серверу.
    Напоминания — не события, в историю не идут."""
    records = []
    for event in events:
        if event.kind in REMINDER_KINDS:
            continue
        recovered = event.kind in RECOVERY_KINDS
        if isinstance(event, RelayEvent):
            details = {
                "kind": event.kind.value,
                "count": event.count,
                "added": event.added,
                "targets": [
                    {"target": r.key, "owner": owners.get(r.ip), "loss_pct": r.loss_pct, "rtt_ms": r.rtt_ms}
                    for r in event.readings
                ],
            }
            records.append(HistoryRecord(event.relay_id, event.relay_name, recovered,
                                         history_text(event, owners, lang), details, event.quiet))
            continue
        if not event.relays:
            continue
        details = {
            "kind": event.kind.value,
            "target": event.target,
            "level": event.level,
            "owner": owners.get(event.target.rpartition(":")[0]),
            "relays": [
                {"server_id": r.relay_id, "name": r.relay_name,
                 "loss_pct": r.reading.loss_pct, "rtt_ms": r.reading.rtt_ms}
                for r in event.relays
            ],
        }
        worst = event.relays[0]
        records.append(HistoryRecord(worst.relay_id, worst.relay_name, recovered,
                                     history_text(event, owners, lang), details, event.quiet))
    return records
