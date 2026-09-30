"""Алерт о потерях до адресов назначения релеев.

Нода-релей сама меряет потери до своих бэкендов HAProxy и целей DNAT
(node/app/services/loss_probe.py), панель собирает их со всего парка
(loss_registry). Здесь решается, когда об этом писать.

Сообщение — только при смене статуса адреса, а не по таймеру:
- «новые потери» — хотя бы один релей видит потери не ниже порога дольше
  удержания; открывается эпизод;
- «усилились» — эпизод перешёл на уровень выше (50%, 90%), тоже с удержанием;
  улучшения внутри эпизода не пишутся;
- «снизились» — у всех релеев потери ниже половины порога непрерывно всё время
  затишья. Любой всплеск выше половины порога сбрасывает отсчёт: мигающий адрес
  остаётся в эпизоде, а не шлёт пары «есть / нет»;
- нет данных (релеи выключены, нода перезапускается) — не изменение: эпизод
  ждёт, а без данных дольше часа закрывается молча;
- напоминание об открытых эпизодах — только если включено.

Эпизоды хранятся в базе, поэтому перезапуск панели уже известные потери
повторно не присылает.
"""

import html
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.services.loss_registry import LossReading, RelaySnapshot

# Нода проверяет адрес раз в 2 с: 30 попыток — минута данных. Доля потерь по
# меньшему числу — шум первых попыток после старта агента или смены конфига
MIN_SAMPLES = 30
RECOVERY_RATIO = 0.5
SEVERITY_BOUNDS = (50.0, 90.0)
NO_DATA_CLOSE_SEC = 3600
# Лимит Telegram — 4096 символов; запас под заголовок части и разметку
MESSAGE_LIMIT = 3800


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

    def level_bounds(self) -> list[float]:
        return [self.threshold] + [bound for bound in SEVERITY_BOUNDS if bound > self.threshold]


@dataclass
class Episode:
    level: int
    opened_at: float
    last_data_at: float
    notified_at: float
    calm_since: Optional[float] = None


class EventKind(str, Enum):
    NEW = "new"
    WORSE = "worse"
    STILL = "still"
    RECOVERED = "recovered"


@dataclass(frozen=True)
class LossEvent:
    kind: EventKind
    target: str
    level: int
    relays: tuple[RelayReading, ...]


def collect_observations(snapshots: list[RelaySnapshot], excluded: set[int],
                         hidden_ips: frozenset[str] = frozenset()) -> dict[str, list[RelayReading]]:
    """Адрес → что видит каждый релей; окно короче MIN_SAMPLES и адреса
    исключённых серверов (hidden_ips) не считаются."""
    observations: dict[str, list[RelayReading]] = {}
    for snapshot in snapshots:
        if snapshot.server_id in excluded:
            continue
        for reading in snapshot.readings:
            if reading.samples < MIN_SAMPLES or reading.ip in hidden_ips:
                continue
            observations.setdefault(reading.key, []).append(
                RelayReading(snapshot.server_id, snapshot.name, reading)
            )
    return observations


@dataclass
class LossTracker:
    episodes: dict[str, Episode] = field(default_factory=dict)
    # (адрес, релей, уровень) → с какого момента потери держатся на уровне
    _above: dict[tuple[str, int, int], float] = field(default_factory=dict)

    def reset(self) -> None:
        self.episodes.clear()
        self._above.clear()

    def evaluate(self, observations: dict[str, list[RelayReading]], policy: LossPolicy,
                 now: float) -> list[LossEvent]:
        self._update_timers(observations, policy, now)
        events = []
        for target in sorted(set(observations) | set(self.episodes)):
            relays = tuple(sorted(observations.get(target, []), key=lambda r: -r.reading.loss_pct))
            event = self._step(target, relays, policy, now)
            if event is not None:
                events.append(event)
        return events

    def _update_timers(self, observations: dict[str, list[RelayReading]], policy: LossPolicy,
                       now: float) -> None:
        bounds = policy.level_bounds()
        active = {}
        for target, relays in observations.items():
            for relay in relays:
                for level, bound in enumerate(bounds, start=1):
                    if relay.reading.loss_pct >= bound:
                        key = (target, relay.relay_id, level)
                        active[key] = self._above.get(key, now)
        self._above = active

    def _sustained_level(self, target: str, policy: LossPolicy, now: float) -> int:
        return max(
            (level for (key, _, level), since in self._above.items()
             if key == target and now - since >= policy.sustained_sec),
            default=0,
        )

    def _step(self, target: str, relays: tuple[RelayReading, ...], policy: LossPolicy,
              now: float) -> Optional[LossEvent]:
        level = self._sustained_level(target, policy, now)
        episode = self.episodes.get(target)
        if episode is None:
            if level == 0:
                return None
            self.episodes[target] = Episode(level=level, opened_at=now, last_data_at=now, notified_at=now)
            return LossEvent(EventKind.NEW, target, level, relays)

        if not relays:
            episode.calm_since = None
            if now - episode.last_data_at >= NO_DATA_CLOSE_SEC:
                del self.episodes[target]
            return None
        episode.last_data_at = now

        if level > episode.level:
            episode.level = level
            episode.calm_since = None
            episode.notified_at = now
            return LossEvent(EventKind.WORSE, target, level, relays)

        if all(r.reading.loss_pct < policy.threshold * RECOVERY_RATIO for r in relays):
            if episode.calm_since is None:
                episode.calm_since = now
            if now - episode.calm_since >= policy.calm_sec:
                del self.episodes[target]
                return LossEvent(EventKind.RECOVERED, target, 0, relays)
            return None
        episode.calm_since = None

        if policy.reminder_sec and now - episode.notified_at >= policy.reminder_sec:
            episode.notified_at = now
            return LossEvent(EventKind.STILL, target, episode.level, relays)
        return None


# ---------------------------------------------------------------------------
# Тексты: по адресу, внутри — релеи
# ---------------------------------------------------------------------------

LEVEL_NAMES = {
    "ru": {1: "потери", 2: "сильные потери", 3: "почти не отвечает"},
    "en": {1: "loss", 2: "heavy loss", 3: "barely reachable"},
}
SECTION_TITLES = {
    EventKind.NEW: ("Новые потери", "New loss"),
    EventKind.WORSE: ("Потери усилились", "Loss got worse"),
    EventKind.STILL: ("Всё ещё есть потери", "Loss persists"),
    EventKind.RECOVERED: ("Потери снизились", "Loss recovered"),
}
SECTION_ORDER = (EventKind.NEW, EventKind.WORSE, EventKind.STILL, EventKind.RECOVERED)


def _lang(lang: str) -> str:
    return "ru" if lang == "ru" else "en"


def _rtt_text(reading: LossReading, ru: bool) -> str:
    if reading.rtt_ms is None:
        return "нет ответа" if ru else "no reply"
    return f"{reading.rtt_ms:.0f} {'мс' if ru else 'ms'}"


def _relay_lines(event: LossEvent, policy: LossPolicy, ru: bool) -> list[str]:
    if event.kind is EventKind.RECOVERED:
        worst = max(r.reading.loss_pct for r in event.relays)
        count = len(event.relays)
        return [f"• {count} {'релеев' if ru else 'relays'}, {'максимум' if ru else 'max'} {worst:g}%"]
    quiet_bound = policy.threshold * RECOVERY_RATIO
    lossy = [r for r in event.relays if r.reading.loss_pct >= quiet_bound]
    lines = [
        f"• {html.escape(r.relay_name)} — {r.reading.loss_pct:g}% ({_rtt_text(r.reading, ru)})"
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


def format_digest(events: list[LossEvent], owners: dict[str, str], policy: LossPolicy,
                  lang: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Все изменения проверки — одним сообщением, при нехватке места — частями."""
    if not events:
        return []
    lang = _lang(lang)
    ru = lang == "ru"
    worst_first = sorted(events, key=lambda e: -max((r.reading.loss_pct for r in e.relays), default=0))
    sections = [
        (SECTION_TITLES[kind][0 if ru else 1], [_address_header(e, owners, lang), *_relay_lines(e, policy, ru)])
        for kind in SECTION_ORDER
        for e in worst_first if e.kind is kind
    ]
    minutes = max(1, round(policy.sustained_sec / 60))
    title = "Потери до адресов" if ru else "Packet loss to addresses"
    subtitle = (f"порог {policy.threshold:g}%, удержание {minutes} мин" if ru
                else f"threshold {policy.threshold:g}%, sustained {minutes} min")
    emoji = "\U0001f7e2" if all(e.kind is EventKind.RECOVERED for e in events) else "\U0001f7e1"
    chunks = _pack(sections, limit)
    total = len(chunks)
    return [
        f"{emoji} <b>{title}</b>{f' ({index}/{total})' if total > 1 else ''}\n{subtitle}\n{chunk}"
        for index, chunk in enumerate(chunks, start=1)
    ]


def history_text(event: LossEvent, owners: dict[str, str], lang: str) -> str:
    """Одна строка для истории алертов — без разметки Telegram."""
    lang = _lang(lang)
    ru = lang == "ru"
    ip = event.target.rpartition(":")[0]
    where = f"{event.target} ({owners[ip]})" if ip in owners else event.target
    title = SECTION_TITLES[event.kind][0 if ru else 1]
    if event.kind is EventKind.RECOVERED:
        return f"{title}: {where}"
    parts = ", ".join(f"{r.relay_name} {r.reading.loss_pct:g}%" for r in event.relays[:5])
    return f"{title}: {where} — {LEVEL_NAMES[lang][event.level]} ({parts})"
