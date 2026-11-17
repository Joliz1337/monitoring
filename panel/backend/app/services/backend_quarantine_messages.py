"""Тексты автоотключения адресов: сводка в Telegram и строки истории алертов.

Все события одного цикла — одно сообщение с разделами в постоянном порядке:
отключены, возвращены, постоянные проблемы, приостановка на релее. В режиме
наблюдения разделы свои («отключил бы» / «вернул бы»), чтобы их нельзя было
принять за настоящие отключения. Модуль чистый: строит строки и ничего не шлёт.

В режиме «только умершие адреса» сводка другая: вместо строк по парам «релей —
адрес» — адреса, отключённые почти на всех своих релеях, и их возврат; нода, у
которой так умерли все адреса, — одной строкой. История алертов получает всё.
"""

import html
from dataclasses import dataclass
from typing import Callable, Optional

from app.services.backend_quarantine import Event, EventKind, ReasonKind, RestoreCause
from app.services.backend_quarantine_settings import NotifyScope

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
    EventKind.DEAD: ("backend_dead", "critical"),
    EventKind.ALIVE: ("backend_alive", "info"),
}

# Что из событий уходит в Telegram в каждом режиме уведомлений
TELEGRAM_KINDS = {
    NotifyScope.ALL: (EventKind.DISABLED, EventKind.RESTORED, EventKind.CHRONIC, EventKind.RELAY_PAUSED),
    NotifyScope.DEAD: (EventKind.DEAD, EventKind.ALIVE, EventKind.RELAY_PAUSED),
}
# События об адресе целиком, а не о паре «релей — адрес»
ADDRESS_STATE = (EventKind.DEAD, EventKind.ALIVE)


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


def _addresses_ru(count: int) -> str:
    tens, ones = count % 100, count % 10
    if tens in (11, 12, 13, 14):
        return "адресов"
    if ones == 1:
        return "адрес"
    return "адреса" if ones in (2, 3, 4) else "адресов"


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
        "stalled": "данные не идут: сервер не отвечает клиентам",
        "no_reply": "не отвечает на проверку как клиент",
        "no_reply_gone": "снова отвечает на проверку",
        "until_answers": "пока не начнёт отвечать на проверку",
        "stalled_retry": "срок вышел, сервер снова получает клиентов",
        "stalled_gone": "молчание сервера больше не наблюдается",
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
            RestoreCause.BLIND: "проверка недоступна, срок вышел — сервер снова получает клиентов",
        },
        "chronic_line": lambda strikes, relays: (
            f"отключений за сутки: {strikes}, релеи: {relays}. Адрес стоит заменить"
        ),
        "paused_line": "теряет больше половины своих адресов — похоже, проблема самого релея",
        "observe_mark": "наблюдение",
        "dead": "Адрес не работает",
        "would_die": "Не работал бы (режим наблюдения)",
        "alive": "Адрес снова работает",
        "would_revive": "Снова работал бы (режим наблюдения)",
        "spread": lambda failing, total: f"на {failing} из {total} релеев",
        "dead_line": lambda spread: f"не работает {spread}",
        "alive_line": "снова работает",
        "alive_partly": lambda spread: f"снова работает, отключён {spread}",
        "reason_names": {
            ReasonKind.LOSS: "потери",
            ReasonKind.DOWNS: "падения",
            ReasonKind.STALLED: "не отвечает клиентам",
            ReasonKind.NO_REPLY: "не отвечает на проверку",
        },
        "node": "нода",
        "node_dead": lambda count: f"не работают все {count} {_addresses_ru(count)}",
        "node_alive": lambda count: f"снова работают все {count} {_addresses_ru(count)}",
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
        "stalled": "no data comes back: the server does not answer clients",
        "no_reply": "does not answer the client-like check",
        "no_reply_gone": "answers the check again",
        "until_answers": "until it answers the check",
        "stalled_retry": "term is over, the server gets clients again",
        "stalled_gone": "the server's silence is no longer observed",
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
            RestoreCause.BLIND: "the check is unavailable, term is over — the server gets clients again",
        },
        "chronic_line": lambda strikes, relays: (
            f"disables in 24 h: {strikes}, relays: {relays}. Consider replacing the address"
        ),
        "paused_line": "is losing more than half of its addresses — looks like the relay's own problem",
        "observe_mark": "observe",
        "dead": "Address is down",
        "would_die": "Would be down (observe mode)",
        "alive": "Address works again",
        "would_revive": "Would work again (observe mode)",
        "spread": lambda failing, total: f"on {failing} of {total} relays",
        "dead_line": lambda spread: f"is down {spread}",
        "alive_line": "works again",
        "alive_partly": lambda spread: f"works again, still disabled {spread}",
        "reason_names": {
            ReasonKind.LOSS: "loss",
            ReasonKind.DOWNS: "going down",
            ReasonKind.STALLED: "does not answer clients",
            ReasonKind.NO_REPLY: "does not answer the check",
        },
        "node": "node",
        "node_dead": lambda count: f"all {count} addresses are down",
        "node_alive": lambda count: f"all {count} addresses work again",
    },
}


def _texts(lang: str) -> dict:
    return TEXTS.get(lang, TEXTS["en"])


def _number(value: float) -> str:
    return f"{value:g}"


def _reason(event: Event, texts: dict) -> str:
    if event.reason_kind is ReasonKind.DOWNS:
        return texts["downs"](event.downs)
    if event.reason_kind is ReasonKind.STALLED:
        return texts["stalled"]
    if event.reason_kind is ReasonKind.NO_REPLY:
        return texts["no_reply"]
    return texts["loss"](_number(event.loss_pct))


def _cause(event: Event, texts: dict) -> str:
    if event.cause is RestoreCause.CALM and event.reason_kind is ReasonKind.NO_REPLY:
        return texts["no_reply_gone"]
    if event.cause is RestoreCause.CALM and event.reason_kind is ReasonKind.STALLED:
        # Отключённый сервер трафика не получает: возврат — проверка делом. В наблюдении
        # вердикт мог просто пропасть (мало трафика), поэтому «отвечает» не утверждается
        return texts["stalled_gone"] if event.observe else texts["stalled_retry"]
    return texts["causes"][event.cause]


def _term(seconds: float, texts: dict) -> Optional[str]:
    if seconds <= 0:
        return None
    if seconds >= SECONDS_IN_HOUR:
        return texts["term_hours"](_number(round(seconds / SECONDS_IN_HOUR, 1)))
    return texts["term_min"](max(1, round(seconds / SECONDS_IN_MINUTE)))


def _facts(event: Event, texts: dict) -> list[str]:
    """Что сказать о событии помимо адреса и релея — одинаково для Telegram и истории."""
    if event.kind is EventKind.DISABLED:
        # Молчащий на проверку отключён не на срок, а до ответа; в наблюдении срока нет вовсе
        until_answers = event.reason_kind is ReasonKind.NO_REPLY and event.term_sec > 0
        facts = [_reason(event, texts), texts["until_answers"] if until_answers else _term(event.term_sec, texts)]
        if event.sessions > 0:
            facts.append(texts["sessions"](event.sessions))
        if event.strikes > 1:
            facts.append(texts["strikes"](event.strikes))
        return [fact for fact in facts if fact]
    if event.kind is EventKind.RESTORED:
        return [_cause(event, texts)] if event.cause else []
    if event.kind is EventKind.CHRONIC:
        return [texts["chronic_line"](event.strikes, ", ".join(event.relays))]
    if event.kind is EventKind.DEAD:
        reasons = ", ".join(texts["reason_names"][kind] for kind in event.reasons)
        spread = texts["dead_line"](texts["spread"](event.failing, event.total))
        return [f"{spread}: {reasons}" if reasons else spread]
    if event.kind is EventKind.ALIVE:
        # Ниже порога — не значит «везде в работе»: оставшиеся отключения названы
        if event.failing > 0:
            return [texts["alive_partly"](texts["spread"](event.failing, event.total))]
        return [texts["alive_line"]]
    return [texts["paused_line"]]


def _owner(event: Event, owners: dict[str, str]) -> Optional[str]:
    ip, _, _ = event.target.rpartition(":")
    return event.owner or owners.get(ip)


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
    if event.kind is EventKind.CHRONIC or event.kind in ADDRESS_STATE:
        return f"{address} — {facts}"
    return f"{address} — {relay}: {facts}" if facts else f"{address} — {relay}"


def _section(event: Event) -> str:
    if event.kind is EventKind.DISABLED:
        return "would_disable" if event.observe else "disabled"
    if event.kind is EventKind.RESTORED:
        return "would_restore" if event.observe else "restored"
    if event.kind is EventKind.DEAD:
        return "would_die" if event.observe else "dead"
    if event.kind is EventKind.ALIVE:
        return "would_revive" if event.observe else "alive"
    return "chronic" if event.kind is EventKind.CHRONIC else "paused"


SECTION_ORDER = (
    "disabled", "would_disable", "restored", "would_restore", "chronic",
    "dead", "would_die", "alive", "would_revive", "paused",
)


def telegram_events(events: list[Event], scope: NotifyScope) -> list[Event]:
    return [event for event in events if event.kind in TELEGRAM_KINDS[scope]]


def _digest_lines(events: list[Event], owners: dict[str, str], texts: dict) -> list[tuple[str, str]]:
    """Строки сводки с их разделами. Нода, у которой не работают (или снова работают)
    все адреса, — одна строка вместо строки на каждый адрес."""
    def code(text: str) -> str:
        return f"<code>{text}</code>"

    def bold(text: str) -> str:
        return f"<b>{text}</b>"

    whole_nodes: dict[tuple[str, Optional[str]], int] = {}
    for event in events:
        if event.node_total:
            key = (_section(event), _owner(event, owners))
            whole_nodes[key] = max(whole_nodes.get(key, 0), event.node_total)

    lines: list[tuple[str, str]] = []
    told: set[tuple[str, Optional[str]]] = set()
    for event in events:
        section = _section(event)
        key = (section, _owner(event, owners))
        if event.kind not in ADDRESS_STATE or key not in whole_nodes:
            lines.append((section, _line(event, owners, texts, html.escape, code, bold)))
        elif key not in told:
            told.add(key)
            state = texts["node_dead" if event.kind is EventKind.DEAD else "node_alive"](whole_nodes[key])
            lines.append((section, f"{texts['node']} {bold(html.escape(key[1] or ''))} — {html.escape(state)}"))
    return lines


def format_digest(events: list[Event], owners: dict[str, str], lang: str,
                  scope: NotifyScope = NotifyScope.ALL) -> list[str]:
    """Сообщения Telegram (HTML); длинная сводка делится между строками, заголовок
    раздела повторяется в каждой части."""
    events = telegram_events(events, scope)
    if not events:
        return []
    texts = _texts(lang)
    sections: dict[str, list[str]] = {}
    for section, line in _digest_lines(events, owners, texts):
        sections.setdefault(section, []).append(f"• {line}")

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


def _state_details(event: Event, owners: dict[str, str]) -> dict:
    return {
        "target": event.target,
        "owner": _owner(event, owners),
        "failing": event.failing,
        "total": event.total,
        "reasons": [kind.value for kind in event.reasons],
        "observe": event.observe,
        "node_total": event.node_total,
    }


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
            details=_state_details(event, owners) if event.kind in ADDRESS_STATE else {
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
