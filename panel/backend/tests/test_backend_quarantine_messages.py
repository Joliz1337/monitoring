"""Tests for the Telegram digest and alert history of backend auto-disable.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Сообщение — единственное, что оператор видит об автоотключении, не открывая
панель: из него должно быть понятно, какой адрес, на каком релее, почему и
надолго ли. Имена серверов приходят от людей и уходят в HTML Telegram.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.backend_quarantine import Event, EventKind, ReasonKind, RestoreCause  # noqa: E402
from app.services.backend_quarantine_messages import (  # noqa: E402
    MESSAGE_LIMIT,
    format_digest,
    history_records,
    telegram_events,
)
from app.services.backend_quarantine_settings import NotifyScope  # noqa: E402

TARGET = "193.141.60.129:8443"
SECOND = "193.141.60.130:8443"
OWNERS = {"193.141.60.129": "nl-3"}


def disabled(**fields) -> Event:
    base = dict(kind=EventKind.DISABLED, relay_id=1, relay_name="msk-1", target=TARGET,
                reason_kind=ReasonKind.LOSS, loss_pct=25.0, downs=0, term_sec=600, sessions=221, strikes=1)
    return Event(**{**base, **fields})


def restored(cause: RestoreCause, **fields) -> Event:
    return disabled(**{"kind": EventKind.RESTORED, "cause": cause, "term_sec": 0, **fields})


def text(events: list[Event], lang: str = "ru") -> str:
    [message] = format_digest(events, OWNERS, lang)
    return message


def dead(**fields) -> Event:
    base = dict(kind=EventKind.DEAD, relay_id=1, relay_name="msk-1", target=TARGET, failing=5, total=6,
                reasons=(ReasonKind.LOSS,), owner="nl-3")
    return Event(**{**base, **fields})


def alive(**fields) -> Event:
    return dead(**{"kind": EventKind.ALIVE, "failing": 0, "reasons": (), **fields})


def dead_text(events: list[Event], lang: str = "ru") -> str:
    [message] = format_digest(events, OWNERS, lang, NotifyScope.DEAD)
    return message


class DisabledLineTests(unittest.TestCase):
    def test_says_what_where_why_and_for_how_long(self):
        message = text([disabled()])
        for part in ("Отключены", "<code>193.141.60.129:8443</code>", "nl-3", "msk-1", "потери 25 %",
                     "на 10 мин", "соединений переведено: 221"):
            self.assertIn(part, message)

    def test_downs_reason(self):
        message = text([disabled(reason_kind=ReasonKind.DOWNS, loss_pct=0.0, downs=4)])
        self.assertIn("упал 4 раза за 30 мин", message)
        self.assertNotIn("потери", message)

    def test_silent_server_reason(self):
        message = text([disabled(reason_kind=ReasonKind.STALLED, loss_pct=0.0)])
        self.assertIn("сервер не отвечает клиентам", message)
        self.assertNotIn("потери", message)

    def test_reason_of_a_server_silent_to_the_probe(self):
        message = text([disabled(reason_kind=ReasonKind.NO_REPLY, loss_pct=100.0)])
        self.assertIn("не отвечает на проверку как клиент", message)
        self.assertNotIn("потери", message)

    def test_server_silent_to_the_probe_is_out_until_it_answers_not_for_a_term(self):
        message = text([disabled(reason_kind=ReasonKind.NO_REPLY, loss_pct=100.0, term_sec=600)])
        self.assertIn("пока не начнёт отвечать", message)
        self.assertNotIn("на 10 мин", message)

    def test_downs_are_declined(self):
        for downs, phrase in ((3, "упал 3 раза "), (5, "упал 5 раз "), (11, "упал 11 раз "), (21, "упал 21 раз "),
                              (22, "упал 22 раза ")):
            with self.subTest(downs=downs):
                self.assertIn(phrase, text([disabled(reason_kind=ReasonKind.DOWNS, downs=downs)]))

    def test_fractional_loss_keeps_its_fraction(self):
        self.assertIn("потери 12.5 %", text([disabled(loss_pct=12.5)]))

    def test_long_terms_are_in_hours(self):
        self.assertIn("на 30 мин", text([disabled(term_sec=1800)]))
        self.assertIn("на 2 ч", text([disabled(term_sec=7200)]))
        self.assertIn("на 6 ч", text([disabled(term_sec=21600)]))

    def test_repeat_disables_are_counted(self):
        self.assertNotIn("за сутки", text([disabled(strikes=1)]))
        self.assertIn("отключений за сутки: 3", text([disabled(strikes=3)]))

    def test_no_sessions_no_boast(self):
        self.assertNotIn("соединений", text([disabled(sessions=0)]))

    def test_address_without_owner(self):
        message = text([disabled(target="10.9.9.9:443")])
        self.assertIn("<code>10.9.9.9:443</code>", message)
        self.assertNotIn("None", message)

    def test_observe_mode_is_its_own_section(self):
        message = text([disabled(observe=True, term_sec=0, sessions=0)])
        self.assertIn("Отключил бы", message)
        self.assertNotIn("<b>Отключены</b>", message)
        self.assertNotIn("на 0", message)

    def test_observe_mode_promises_no_wait_for_a_server_silent_to_the_probe(self):
        # В наблюдении адрес никуда не уходит — ждать его ответа нечего
        message = text([disabled(observe=True, term_sec=0, sessions=0, reason_kind=ReasonKind.NO_REPLY,
                                 loss_pct=100.0)])
        self.assertIn("не отвечает на проверку как клиент", message)
        self.assertNotIn("пока не начнёт отвечать", message)


class RestoredLineTests(unittest.TestCase):
    def test_every_cause_reads_differently(self):
        phrases = {
            RestoreCause.CALM: "потери ушли",
            RestoreCause.MANUAL: "вручную",
            RestoreCause.EXEMPT: "не отключать",
            RestoreCause.GUARD: "последним",
            RestoreCause.MODE: "выключено",
        }
        for cause, phrase in phrases.items():
            with self.subTest(cause=cause):
                message = text([restored(cause)])
                self.assertIn("Возвращены", message)
                self.assertIn(phrase, message)
        self.assertEqual(len({text([restored(cause)]) for cause in phrases}), len(phrases))

    def test_observe_return_is_marked(self):
        self.assertIn("Вернул бы", text([restored(RestoreCause.CALM, observe=True)]))

    def test_silent_server_does_not_return_because_loss_is_gone(self):
        # Потерь у такого сервера могло и не быть. Отключённый, он трафика не получает:
        # заговорил ли он, покажет только возврат в работу
        back = text([restored(RestoreCause.CALM, reason_kind=ReasonKind.STALLED)])
        self.assertIn("срок вышел", back)
        self.assertNotIn("потери", back)

    def test_watched_silent_server_leaves_the_list_without_a_claim_that_it_answers(self):
        watched = text([restored(RestoreCause.CALM, reason_kind=ReasonKind.STALLED, observe=True)])
        self.assertIn("больше не наблюдается", watched)
        self.assertNotIn("отвечает", watched)
        self.assertNotIn("срок", watched)

    def test_server_checked_as_a_client_returns_because_it_answers(self):
        for observe in (False, True):
            with self.subTest(observe=observe):
                back = text([restored(RestoreCause.CALM, reason_kind=ReasonKind.NO_REPLY, observe=observe)])
                self.assertIn("снова отвечает на проверку", back)
                self.assertNotIn("потери", back)

    def test_return_without_the_probe_says_so(self):
        back = text([restored(RestoreCause.BLIND, reason_kind=ReasonKind.NO_REPLY)])
        self.assertIn("проверка недоступна", back)
        self.assertNotIn("снова отвечает", back)

    def test_other_causes_do_not_depend_on_the_reason(self):
        self.assertIn("вручную", text([restored(RestoreCause.MANUAL, reason_kind=ReasonKind.STALLED)]))


class OtherSectionTests(unittest.TestCase):
    def test_chronic_names_relays_and_advises_replacement(self):
        event = Event(EventKind.CHRONIC, 1, "msk-1", TARGET, strikes=5, relays=("msk-1", "spb-2"))
        message = text([event])
        for part in ("Постоянные проблемы", TARGET, "nl-3", "5", "msk-1, spb-2", "заменить"):
            self.assertIn(part, message)

    def test_relay_pause_names_the_relay(self):
        message = text([Event(EventKind.RELAY_PAUSED, 4, "msk-4")])
        self.assertIn("приостановлено", message)
        self.assertIn("msk-4", message)

    def test_sections_keep_a_fixed_order(self):
        events = [
            Event(EventKind.RELAY_PAUSED, 4, "msk-4"),
            restored(RestoreCause.CALM),
            Event(EventKind.CHRONIC, 1, "msk-1", TARGET, strikes=4, relays=("msk-1",)),
            disabled(),
        ]
        message = text(events)
        order = [message.index(title) for title in ("Отключены", "Возвращены", "Постоянные проблемы", "приостановлено")]
        self.assertEqual(order, sorted(order))

    def test_nothing_to_say(self):
        self.assertEqual(format_digest([], OWNERS, "ru"), [])


class SafetyTests(unittest.TestCase):
    def test_names_are_escaped_for_telegram_html(self):
        message = text([disabled(relay_name="<b>relay</b> & co")])
        self.assertIn("&lt;b&gt;relay&lt;/b&gt; &amp; co", message)
        self.assertNotIn("<b>relay</b>", message)

    def test_owner_is_escaped_too(self):
        [message] = format_digest([disabled()], {"193.141.60.129": "<nl>"}, "ru")
        self.assertIn("&lt;nl&gt;", message)

    def test_long_digest_is_split_between_lines(self):
        events = [disabled(target=f"10.0.{i // 250}.{i % 250 + 1}:443", relay_name=f"relay-{i}") for i in range(120)]
        parts = format_digest(events, {}, "ru")
        self.assertGreater(len(parts), 1)
        for index, part in enumerate(parts, start=1):
            self.assertLessEqual(len(part), MESSAGE_LIMIT)
            self.assertIn(f"({index}/{len(parts)})", part)
            self.assertIn("Отключены", part)
        self.assertEqual(sum(part.count("<code>") for part in parts), 120)

    def test_english_digest(self):
        message = text([disabled(), restored(RestoreCause.CALM)], lang="en")
        for part in ("Disabled", "loss 25 %", "for 10 min", "connections moved: 221", "Restored", "loss is gone"):
            self.assertIn(part, message)
        self.assertNotIn("потери", message)

    def test_english_silent_server(self):
        self.assertIn("the server does not answer clients",
                      text([disabled(reason_kind=ReasonKind.STALLED)], lang="en"))
        self.assertIn("silence is no longer observed",
                      text([restored(RestoreCause.CALM, reason_kind=ReasonKind.STALLED, observe=True)], lang="en"))
        self.assertIn("term is over",
                      text([restored(RestoreCause.CALM, reason_kind=ReasonKind.STALLED)], lang="en"))

    def test_english_client_probe_texts(self):
        self.assertIn("does not answer the client-like check",
                      text([disabled(reason_kind=ReasonKind.NO_REPLY)], lang="en"))
        self.assertIn("answers the check again",
                      text([restored(RestoreCause.CALM, reason_kind=ReasonKind.NO_REPLY)], lang="en"))

    def test_unknown_language_falls_back_to_english(self):
        self.assertIn("Disabled", text([disabled()], lang="de"))


class ScopeTests(unittest.TestCase):
    """Режим уведомлений выбирает, что уходит в Telegram; история алертов получает всё."""

    def test_all_events_scope_ignores_address_state(self):
        self.assertEqual(format_digest([disabled(), dead(), alive()], OWNERS, "ru"),
                         format_digest([disabled()], OWNERS, "ru"))
        self.assertEqual(format_digest([dead()], OWNERS, "ru"), [])

    def test_dead_scope_sends_only_address_state_and_relay_pause(self):
        chronic = Event(EventKind.CHRONIC, 1, "msk-1", TARGET, strikes=4, relays=("msk-1",))
        paused = Event(EventKind.RELAY_PAUSED, 2, "msk-2")
        message = dead_text([disabled(), restored(RestoreCause.CALM), chronic, dead(), paused])
        for part in ("Адрес не работает", "Автоотключение приостановлено", "msk-2"):
            self.assertIn(part, message)
        for part in ("Отключены", "Возвращены", "Постоянные проблемы"):
            self.assertNotIn(part, message)

    def test_dead_scope_with_nothing_of_its_own_is_silent(self):
        events = [disabled(), restored(RestoreCause.CALM)]
        self.assertEqual(format_digest(events, OWNERS, "ru", NotifyScope.DEAD), [])

    def test_telegram_events_by_scope(self):
        events = [disabled(), dead(), alive(), Event(EventKind.RELAY_PAUSED, 2, "msk-2")]
        self.assertEqual([event.kind for event in telegram_events(events, NotifyScope.ALL)],
                         [EventKind.DISABLED, EventKind.RELAY_PAUSED])
        self.assertEqual([event.kind for event in telegram_events(events, NotifyScope.DEAD)],
                         [EventKind.DEAD, EventKind.ALIVE, EventKind.RELAY_PAUSED])


class DeadLineTests(unittest.TestCase):
    def test_dead_line_names_spread_and_reasons(self):
        message = dead_text([dead(reasons=(ReasonKind.LOSS, ReasonKind.NO_REPLY))])
        self.assertIn("• <code>193.141.60.129:8443</code> · nl-3 — не работает на 5 из 6 релеев: "
                      "потери, не отвечает на проверку", message)

    def test_every_reason_has_a_name(self):
        message = dead_text([dead(reasons=tuple(ReasonKind))])
        self.assertIn(": потери, падения, не отвечает клиентам, не отвечает на проверку", message)

    def test_alive_line(self):
        self.assertTrue(dead_text([alive()]).endswith("· nl-3 — снова работает"))
        self.assertIn("— снова работает, отключён на 1 из 6 релеев", dead_text([alive(failing=1)]))

    def test_sections_of_the_dead_scope_keep_a_fixed_order(self):
        message = dead_text([Event(EventKind.RELAY_PAUSED, 2, "msk-2"), alive(), dead()])
        order = [message.index(title) for title in
                 ("Адрес не работает", "Адрес снова работает", "Автоотключение приостановлено")]
        self.assertEqual(order, sorted(order))

    def test_event_owner_wins_over_registry(self):
        [message] = format_digest([dead(owner="old-name")], {}, "ru", NotifyScope.DEAD)
        self.assertIn("· old-name —", message)

    def test_owner_falls_back_to_registry(self):
        self.assertIn("· nl-3 —", dead_text([dead(owner=None)]))

    def test_observe_sections_are_marked(self):
        message = dead_text([dead(observe=True), alive(observe=True)])
        self.assertIn("Не работал бы (режим наблюдения)", message)
        self.assertIn("Снова работал бы (режим наблюдения)", message)
        self.assertNotIn("<b>Адрес не работает</b>", message)

    def test_owner_name_is_escaped(self):
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", dead_text([dead(owner="<b>x</b>")]))

    def test_english_dead_digest_has_no_russian(self):
        events = [dead(reasons=tuple(ReasonKind)), alive(failing=1),
                  dead(target="1.1.1.1:443", node_total=2, owner="nl-9"), dead(observe=True), alive(observe=True)]
        message = dead_text(events, lang="en")
        self.assertNotRegex(message, "[а-яА-ЯёЁ]")
        self.assertIn("is down on 5 of 6 relays", message)


class NodeLineTests(unittest.TestCase):
    """Нода, у которой не работают все адреса, — одна строка, а не строка на адрес."""

    def test_dead_node_is_one_line(self):
        message = dead_text([dead(), dead(target=SECOND, node_total=2)])
        self.assertIn("• нода <b>nl-3</b> — не работают все 2 адреса", message)
        self.assertNotIn("<code>", message)

    def test_address_count_is_declined(self):
        for count, form in ((2, "2 адреса"), (5, "5 адресов"), (11, "11 адресов"), (21, "21 адрес"), (22, "22 адреса")):
            with self.subTest(count=count):
                self.assertTrue(dead_text([dead(node_total=count)]).endswith(f"все {form}"))

    def test_alive_node_is_one_line(self):
        message = dead_text([alive(), alive(target=SECOND, node_total=2)])
        self.assertIn("• нода <b>nl-3</b> — снова работают все 2 адреса", message)
        self.assertNotIn("<code>", message)

    def test_other_owner_keeps_its_own_line(self):
        message = dead_text([dead(node_total=2), dead(target="10.9.9.9:443", owner="de-1")])
        self.assertIn("нода <b>nl-3</b>", message)
        self.assertIn("<code>10.9.9.9:443</code> · de-1", message)

    def test_dead_node_does_not_swallow_its_revived_address(self):
        message = dead_text([dead(node_total=2), alive(target=SECOND)])
        self.assertIn("нода <b>nl-3</b> — не работают", message)
        self.assertIn(f"<code>{SECOND}</code>", message)


class HistoryTests(unittest.TestCase):
    def test_one_record_per_event_behind_its_relay(self):
        events = [disabled(), restored(RestoreCause.MANUAL, relay_id=2, relay_name="spb-2"),
                  Event(EventKind.CHRONIC, 1, "msk-1", TARGET, strikes=4, relays=("msk-1", "spb-2")),
                  Event(EventKind.RELAY_PAUSED, 4, "msk-4")]
        records = history_records(events, OWNERS, "ru")
        self.assertEqual(
            [(r.server_id, r.server_name, r.alert_type, r.severity) for r in records],
            [(1, "msk-1", "backend_disabled", "warning"), (2, "spb-2", "backend_restored", "info"),
             (1, "msk-1", "backend_chronic", "warning"), (4, "msk-4", "backend_quarantine_paused", "warning")],
        )

    def test_history_text_is_plain(self):
        [record] = history_records([disabled(relay_name="<b>x</b>")], OWNERS, "ru")
        self.assertIn(TARGET, record.message)
        self.assertIn("потери 25 %", record.message)
        self.assertNotIn("<code>", record.message)
        self.assertNotIn("&lt;", record.message)

    def test_details_carry_the_numbers(self):
        [record] = history_records([disabled(strikes=2)], OWNERS, "ru")
        self.assertEqual(record.details, {
            "target": TARGET, "owner": "nl-3", "reason": "loss", "loss_pct": 25.0, "downs": 0,
            "term_sec": 600, "sessions": 221, "strikes": 2, "cause": None, "observe": False, "relays": [],
        })

    def test_silent_server_in_history(self):
        [record] = history_records([disabled(reason_kind=ReasonKind.STALLED, loss_pct=0.0)], OWNERS, "ru")
        self.assertEqual(record.details["reason"], "stalled")
        self.assertIn("сервер не отвечает клиентам", record.message)

    def test_history_of_dead_and_alive(self):
        died, revived = history_records(
            [dead(reasons=(ReasonKind.LOSS, ReasonKind.NO_REPLY)), alive(failing=1)], OWNERS, "ru",
        )
        self.assertEqual((died.alert_type, died.severity, died.server_id, died.server_name),
                         ("backend_dead", "critical", 1, "msk-1"))
        self.assertEqual((revived.alert_type, revived.severity), ("backend_alive", "info"))
        self.assertEqual(died.message, "193.141.60.129:8443 · nl-3 — не работает на 5 из 6 релеев: "
                                       "потери, не отвечает на проверку")
        self.assertEqual(died.details, {
            "target": TARGET, "owner": "nl-3", "failing": 5, "total": 6, "reasons": ["loss", "no_reply"],
            "observe": False, "node_total": 0,
        })

    def test_history_keeps_a_row_per_address_of_a_dead_node(self):
        records = history_records([dead(), dead(target=SECOND, node_total=2)], OWNERS, "ru")
        self.assertEqual([record.details["target"] for record in records], [TARGET, SECOND])

    def test_observe_dead_is_marked_in_history(self):
        [record] = history_records([dead(observe=True)], OWNERS, "ru")
        self.assertTrue(record.message.startswith("[наблюдение] "))

    def test_observe_events_are_marked_in_history(self):
        [record] = history_records([disabled(observe=True, term_sec=0)], OWNERS, "ru")
        self.assertIn("наблюдение", record.message)
        self.assertTrue(record.details["observe"])


if __name__ == "__main__":
    unittest.main()
