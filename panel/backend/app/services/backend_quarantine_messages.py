"""Тексты автоотключения адресов: сводка в Telegram и строки истории алертов.

Все события одного цикла — одно сообщение с разделами в постоянном порядке:
отключены, возвращены, постоянные проблемы, приостановка на релее. В режиме
наблюдения разделы свои («отключил бы» / «вернул бы»), чтобы их нельзя было
принять за настоящие отключения. Модуль чистый: строит строки и ничего не шлёт.
"""

import html
from dataclasses import dataclass
from typing import Callable, Optional

from app.services.backend_quarantine import Event, EventKind, ReasonKind, RestoreCause

# Лимит Telegram — 4096 символов; запас под заголовок части и разметку
MESSAGE_LIMIT = 3800
DOWNS_WINDOW_MINUTES = 30
SECONDS_IN_MINUTE = 60
SECONDS_IN_HOUR = 3600

ALERT_TYPES = {
    EventKind.DISABLED: ("backend_disabled", "warning"),
    EventKind.RESTORED: ("backend_restored", "info"),
    EventKind.CHRONIC: ("backend_chronic", "warning"),
    EventKind.RELAY_PAUSED: ("backend_quarantine_paused", "warning"),
}


@dataclass(frozen=True)
class HistoryRecord:
    server_id: int
    server_name: str
    alert_type: str
    severity: str
    message: str
    details: dict


def _times_ru(count: int) -> str:
    tens, ones = count % 100, count % 10
    return "раза" if ones in (2, 3, 4) and tens not in (12, 13, 14) else "раз"


TEXTS = {
    "ru": {
        "title": "Автоотключение адресов",
        "disabled": "Отключены",
        "would_disable": "Отключил бы (режим наблюдения)",
        "restored": "Возвращены",
        "would_restore": "Вернул бы (режим наблюдения)",
        "chronic": "Постоянные проблемы",
        "paused": "Автоотключение приостановлено",
        "relay": "релей",
        "loss": lambda pct: f"потери {pct} %",
        "downs": lambda count: f"упал {count} {_times_ru(count)} за {DOWNS_WINDOW_MINUTES} мин",
        "term_min": lambda minutes: f"на {minutes} мин",
        "term_hours": lambda hours: f"на {hours} ч",
        "sessions": lambda count: f"соединений переведено: {count}",
        "strikes": lambda count: f"отключений за сутки: {count}",
        "causes": {
            RestoreCause.CALM: "потери ушли",
            RestoreCause.MANUAL: "возвращён вручную",
            RestoreCause.EXEMPT: "адрес в списке «не отключать»",
            RestoreCause.GUARD: "сервер остался в правиле последним живым",
            RestoreCause.MODE: "автоотключение выключено",
        },
        "chronic_line": lambda strikes, relays: (
            f"отключений за сутки: {strikes}, релеи: {relays}. Адрес стоит заменить"
        ),
        "paused_line": "теряет больше половины своих адресов — похоже, проблема самого релея",
        "observe_mark": "наблюдение",
    },
    "en": {
        "title": "Backend auto-disable",
        "disabled": "Disabled",
        "would_disable": "Would disable (observe mode)",
        "restored": "Restored",
        "would_restore": "Would restore (observe mode)",
        "chronic": "Constant problems",
        "paused": "Auto-disable paused",
        "relay": "relay",
        "loss": lambda pct: f"loss {pct} %",
        "downs": lambda count: f"went down {count} times in {DOWNS_WINDOW_MINUTES} min",
        "term_min": lambda minutes: f"for {minutes} min",
        "term_hours": lambda hours: f"for {hours} h",
        "sessions": lambda count: f"connections moved: {count}",
        "strikes": lambda count: f"disables in 24 h: {count}",
        "causes": {
            RestoreCause.CALM: "loss is gone",
            RestoreCause.MANUAL: "restored by hand",
            RestoreCause.EXEMPT: "address is on the never-disable list",
            RestoreCause.GUARD: "it became the last live server of the rule",
            RestoreCause.MODE: "auto-disable is switched off",
        },
        "chronic_line": lambda strikes, relays: (
            f"disables in 24 h: {strikes}, relays: {relays}. Consider replacing the address"
        ),
        "paused_line": "is losing more than half of its addresses — looks like the relay's own problem",
        "observe_mark": "observe",
    },
}


def _texts(lang: str) -> dict:
    return TEXTS.get(lang, TEXTS["en"])


def _number(value: float) -> str:
    return f"{value:g}"


def _reason(event: Event, texts: dict) -> str:
    if event.reason_kind is ReasonKind.DOWNS:
        return texts["downs"](event.downs)
    return texts["loss"](_number(event.loss_pct))


def _term(seconds: float, texts: dict) -> Optional[str]:
    if seconds <= 0:
        return None
    if seconds >= SECONDS_IN_HOUR:
        return texts["term_hours"](_number(round(seconds / SECONDS_IN_HOUR, 1)))
    return texts["term_min"](max(1, round(seconds / SECONDS_IN_MINUTE)))


def _facts(event: Event, texts: dict) -> list[str]:
    """Что сказать о событии помимо адреса и релея — одинаково для Telegram и истории."""
    if event.kind is EventKind.DISABLED:
        facts = [_reason(event, texts), _term(event.term_sec, texts)]
        if event.sessions > 0:
            facts.append(texts["sessions"](event.sessions))
        if event.strikes > 1:
            facts.append(texts["strikes"](event.strikes))
        return [fact for fact in facts if fact]
    if event.kind is EventKind.RESTORED:
        return [texts["causes"][event.cause]] if event.cause else []
    if event.kind is EventKind.CHRONIC:
        return [texts["chronic_line"](event.strikes, ", ".join(event.relays))]
    return [texts["paused_line"]]


def _owner(event: Event, owners: dict[str, str]) -> Optional[str]:
    ip, _, _ = event.target.rpartition(":")
    return owners.get(ip)


def _line(event: Event, owners: dict[str, str], texts: dict, escape: Callable[[str], str],
          code: Callable[[str], str], bold: Callable[[str], str]) -> str:
    facts = " · ".join(escape(fact) for fact in _facts(event, texts))
    relay = f"{texts['relay']} {bold(escape(event.relay_name))}"
    if event.kind is EventKind.RELAY_PAUSED:
        return f"{relay} {facts}"
    address = code(escape(event.target))
    owner = _owner(event, owners)
    if owner:
        address += f" · {escape(owner)}"
    if event.kind is EventKind.CHRONIC:
        return f"{address} — {facts}"
    return f"{address} — {relay}: {facts}" if facts else f"{address} — {relay}"


def _section(event: Event) -> str:
    if event.kind is EventKind.DISABLED:
        return "would_disable" if event.observe else "disabled"
    if event.kind is EventKind.RESTORED:
        return "would_restore" if event.observe else "restored"
    return "chronic" if event.kind is EventKind.CHRONIC else "paused"


SECTION_ORDER = ("disabled", "would_disable", "restored", "would_restore", "chronic", "paused")


def format_digest(events: list[Event], owners: dict[str, str], lang: str) -> list[str]:
    """Сообщения Telegram (HTML); длинная сводка делится между строками, заголовок
    раздела повторяется в каждой части."""
    if not events:
        return []
    texts = _texts(lang)
    sections: dict[str, list[str]] = {}
    for event in events:
        line = _line(event, owners, texts, html.escape, lambda s: f"<code>{s}</code>", lambda s: f"<b>{s}</b>")
        sections.setdefault(_section(event), []).append(f"• {line}")

    parts: list[list[str]] = [[]]
    size = 0
    for name in SECTION_ORDER:
        header = f"<b>{texts[name]}</b>"
        opened = False
        for line in sections.get(name, []):
            needed = len(line) + 1 + (0 if opened else len(header) + 2)
            if size + needed > MESSAGE_LIMIT - 100 and parts[-1]:
                parts.append([])
                size, opened = 0, False
                needed = len(line) + len(header) + 3
            if not opened:
                parts[-1] += ["", header]
                opened = True
            parts[-1].append(line)
            size += needed

    total = len(parts)
    messages = []
    for index, lines in enumerate(parts, start=1):
        counter = f" ({index}/{total})" if total > 1 else ""
        messages.append(f"<b>{texts['title']}{counter}</b>\n" + "\n".join(lines))
    return messages


def history_records(events: list[Event], owners: dict[str, str], lang: str) -> list[HistoryRecord]:
    texts = _texts(lang)
    records = []
    for event in events:
        alert_type, severity = ALERT_TYPES[event.kind]
        message = _line(event, owners, texts, str, str, str)
        if event.observe:
            message = f"[{texts['observe_mark']}] {message}"
        records.append(HistoryRecord(
            server_id=event.relay_id,
            server_name=event.relay_name,
            alert_type=alert_type,
            severity=severity,
            message=message,
            details={
                "target": event.target,
                "owner": _owner(event, owners),
                "reason": event.reason_kind.value if event.reason_kind else None,
                "loss_pct": event.loss_pct,
                "downs": event.downs,
                "term_sec": event.term_sec,
                "sessions": event.sessions,
                "strikes": event.strikes,
                "cause": event.cause.value if event.cause else None,
                "observe": event.observe,
                "relays": list(event.relays),
            },
        ))
    return records
