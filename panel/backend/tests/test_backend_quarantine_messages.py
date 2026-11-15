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
)

TARGET = "193.141.60.129:8443"
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

    def test_observe_events_are_marked_in_history(self):
        [record] = history_records([disabled(observe=True, term_sec=0)], OWNERS, "ru")
        self.assertIn("наблюдение", record.message)
        self.assertTrue(record.details["observe"])


if __name__ == "__main__":
    unittest.main()
