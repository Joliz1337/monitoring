"""Tests for the decision to take a backend address out of balancing on a relay.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Автоотключение меняет трафик клиентов без участия человека. Ложное отключение
снимает здоровый сервер, пропущенное оставляет клиентов на больном, а мигание
«отключил — вернул» засыпает и релеи командами, и Telegram сообщениями.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.backend_quarantine import (  # noqa: E402
    Event,
    EventKind,
    Exemptions,
    Mode,
    NodeFailure,
    Pair,
    PairState,
    QuarantinePolicy,
    QuarantineTracker,
    ReasonKind,
    RestoreCause,
)
from app.services.loss_registry import (  # noqa: E402
    ExcludedTarget,
    LossReading,
    LossRegistry,
    RelaySnapshot,
    TargetRules,
    parse_readings,
)

POLICY = QuarantinePolicy()
RELAY = 1
IP = "10.0.0.5"
TARGET = f"{IP}:443"
NO_RULES = TargetRules()
NO_EXEMPT = Exemptions()
CYCLE_SEC = 60.0


def reading(loss: float = 0.0, downs: int | None = 0, ip: str = IP, port: int = 443, samples: int = 60) -> LossReading:
    return LossReading(ip, port, loss, 40.0, samples, downs)


def snap(*readings: LossReading, relay_id: int = RELAY) -> RelaySnapshot:
    return RelaySnapshot(relay_id, f"relay{relay_id}", tuple(readings), frozenset(), 0.0)


def node_answer(target: str = TARGET, sessions: tuple[int, ...] = (5,), skipped: tuple[str, ...] = (),
                found: bool = True) -> list[dict]:
    ip, _, port = target.rpartition(":")
    return [{
        "ip": ip, "port": int(port), "found": found,
        "applied": [{"backend": "backend_tcp_lb", "server": f"srv{i}", "sessions": n} for i, n in enumerate(sessions)],
        "skipped": [{"backend": "backend_tcp_lb", "reason": reason} for reason in skipped],
    }]


def kinds(events: list[Event]) -> list[EventKind]:
    return [event.kind for event in events]


class TrackerTestCase(unittest.TestCase):
    mode = Mode.ENFORCE

    def setUp(self):
        self.tracker = QuarantineTracker()
        self.now = 1_700_000_000.0

    def step(self, *snapshots: RelaySnapshot, advance: float = 0.0, rules: TargetRules = NO_RULES,
             exempt: Exemptions = NO_EXEMPT, policy: QuarantinePolicy = POLICY) -> list[Event]:
        """Те же замеры приходят всё время `advance`, раз в минуту, как при живом
        сборе: пропуск данных дольше 90 с модуль считает разрывом."""
        events: list[Event] = []
        remaining = advance
        while remaining > CYCLE_SEC:
            self.now += CYCLE_SEC
            remaining -= CYCLE_SEC
            events += self.tracker.evaluate(list(snapshots), rules, exempt, policy, self.mode, self.now)
        self.now += remaining
        return events + self.tracker.evaluate(list(snapshots), rules, exempt, policy, self.mode, self.now)

    def answer(self, results, relay_id: int = RELAY, advance: float = 0.0) -> list[Event]:
        self.now += advance
        return self.tracker.apply_node_results(relay_id, results, POLICY, self.now)

    def state(self, relay_id: int = RELAY, target: str = TARGET) -> PairState | None:
        pair = self.tracker.pairs.get((relay_id, target))
        return pair.state if pair else None

    def disable(self, relay_id: int = RELAY, ip: str = IP) -> list[Event]:
        """Падения дают решение сразу; ответ ноды его подтверждает."""
        pair = self.tracker.pairs.get((relay_id, f"{ip}:443"))
        if pair is not None and pair.hold_until is not None and self.now < pair.hold_until:
            # Только что возвращённую пару модуль какое-то время не рассматривает
            self.step(snap(reading(0.0, ip=ip), relay_id=relay_id), advance=pair.hold_until - self.now)
        self.step(snap(reading(downs=3, ip=ip), relay_id=relay_id))
        return self.answer(node_answer(f"{ip}:443"), relay_id)


class LossTriggerTests(TrackerTestCase):
    def test_loss_must_hold_for_the_whole_sustain_time(self):
        self.step(snap(reading(12.0)))
        self.step(snap(reading(12.0)), advance=170)
        self.assertIsNone(self.state())
        self.step(snap(reading(12.0)), advance=10)
        self.assertEqual(self.state(), PairState.PENDING)

    def test_decision_alone_is_silent(self):
        self.step(snap(reading(12.0)))
        self.assertEqual(self.step(snap(reading(12.0)), advance=180), [])

    def test_loss_below_threshold_never_decides(self):
        for _ in range(5):
            self.step(snap(reading(9.9)), advance=120)
        self.assertIsNone(self.state())

    def test_dip_restarts_the_sustain_time(self):
        self.step(snap(reading(12.0)))
        self.step(snap(reading(4.0)), advance=100)
        self.step(snap(reading(12.0)), advance=10)
        self.step(snap(reading(12.0)), advance=170)
        self.assertIsNone(self.state())
        self.step(snap(reading(12.0)), advance=10)
        self.assertEqual(self.state(), PairState.PENDING)

    def test_reason_remembers_the_numbers(self):
        self.step(snap(reading(25.0, downs=1)))
        self.step(snap(reading(25.0, downs=1)), advance=180)
        pair = self.tracker.pairs[(RELAY, TARGET)]
        self.assertEqual((pair.reason_kind, pair.loss_pct, pair.downs), (ReasonKind.LOSS, 25.0, 1))


class DownsTriggerTests(TrackerTestCase):
    def test_enough_downs_decide_at_once(self):
        self.step(snap(reading(0.0, downs=3)))
        self.assertEqual(self.state(), PairState.PENDING)
        self.assertEqual(self.tracker.pairs[(RELAY, TARGET)].reason_kind, ReasonKind.DOWNS)

    def test_two_downs_are_not_enough(self):
        self.step(snap(reading(0.0, downs=2)))
        self.assertIsNone(self.state())

    def test_address_outside_haproxy_is_never_considered(self):
        # downs=None — цель DNAT или нода старой версии: вывести её из балансировки нечем
        self.step(snap(reading(100.0, downs=None)))
        self.step(snap(reading(100.0, downs=None)), advance=600)
        self.assertIsNone(self.state())

    def test_short_window_is_never_considered(self):
        self.step(snap(reading(100.0, downs=9, samples=29)))
        self.step(snap(reading(100.0, downs=9, samples=29)), advance=600)
        self.assertIsNone(self.state())


class ConfirmationTests(TrackerTestCase):
    def test_node_answer_turns_decision_into_disabled(self):
        self.step(snap(reading(downs=3)))
        [event] = self.answer(node_answer(sessions=(7, 3)))
        self.assertEqual(self.state(), PairState.DISABLED)
        self.assertEqual((event.kind, event.relay_id, event.relay_name, event.target),
                         (EventKind.DISABLED, RELAY, "relay1", TARGET))
        self.assertEqual((event.term_sec, event.sessions, event.strikes, event.observe), (600, 10, 1, False))
        self.assertEqual((event.reason_kind, event.downs), (ReasonKind.DOWNS, 3))

    def test_repeated_answers_are_silent(self):
        self.disable()
        self.assertEqual(self.answer(node_answer(sessions=(0,)), advance=60), [])

    def test_terms_grow_with_every_disable_within_a_day(self):
        terms = []
        for _ in range(5):
            event = self.disable()[0]
            terms.append(event.term_sec)
            self.step(snap(reading(0.0)), advance=event.term_sec)
            self.step(snap(reading(0.0)), advance=POLICY.calm_sec)
            self.assertEqual(self.state(), PairState.IDLE)
        self.assertEqual(terms, [600, 1800, 7200, 21600, 21600])

    def test_disables_older_than_a_day_do_not_count(self):
        self.disable()
        self.step(snap(reading(0.0)), advance=600)
        self.step(snap(reading(0.0)), advance=600)
        self.step(snap(reading(0.0)), advance=86400)
        [event] = self.disable()
        self.assertEqual((event.term_sec, event.strikes), (600, 1))


class BlockedTests(TrackerTestCase):
    def test_refusal_blocks_without_event_or_strike(self):
        self.step(snap(reading(downs=3)))
        self.assertEqual(self.answer(node_answer(sessions=(), skipped=("last_server",))), [])
        pair = self.tracker.pairs[(RELAY, TARGET)]
        self.assertEqual((pair.state, pair.strikes, pair.node), (PairState.BLOCKED, [], {"error": "last_server"}))

    def test_blocked_pair_stays_wanted_and_applies_later(self):
        self.step(snap(reading(downs=3)))
        self.answer(node_answer(sessions=(), skipped=("share_limit",)))
        self.assertEqual(self.tracker.desired(), {RELAY: [TARGET]})
        [event] = self.answer(node_answer(), advance=60)
        self.assertEqual((event.kind, self.state()), (EventKind.DISABLED, PairState.DISABLED))

    def test_unreachable_node_blocks_with_its_code(self):
        self.step(snap(reading(downs=3)))
        self.assertEqual(self.answer(NodeFailure.UNSUPPORTED), [])
        self.assertEqual(self.tracker.pairs[(RELAY, TARGET)].node, {"error": "unsupported"})
        self.assertEqual(self.state(), PairState.BLOCKED)

    def test_unreachable_node_keeps_a_disabled_pair_disabled(self):
        self.disable()
        self.answer(NodeFailure.OFFLINE, advance=60)
        self.assertEqual(self.state(), PairState.DISABLED)

    def test_blocked_pair_is_dropped_when_the_cause_is_gone(self):
        self.step(snap(reading(downs=3)))
        self.answer(node_answer(sessions=(), skipped=("last_server",)))
        self.step(snap(reading(0.0)), advance=60)
        self.assertEqual(self.step(snap(reading(0.0)), advance=POLICY.calm_sec), [])
        self.assertEqual(self.tracker.desired(), {})

    def test_node_releasing_the_last_server_is_reported(self):
        self.disable()
        [event] = self.answer(node_answer(sessions=(), skipped=("last_server",)), advance=60)
        self.assertEqual((event.kind, event.cause), (EventKind.RESTORED, RestoreCause.GUARD))
        self.assertEqual(self.state(), PairState.BLOCKED)

    def test_reapplying_after_the_guard_is_the_same_disable(self):
        self.disable()
        self.answer(node_answer(sessions=(), skipped=("last_server",)), advance=60)
        self.assertEqual(self.answer(node_answer(), advance=60), [])
        pair = self.tracker.pairs[(RELAY, TARGET)]
        self.assertEqual((pair.state, len(pair.strikes)), (PairState.DISABLED, 1))
        # Срок считается от первого отключения, а не начинается заново
        self.assertEqual(pair.until, pair.disabled_at + 600)

    def test_flapping_neighbour_is_reported_once(self):
        # Сосед мигает: нода то снимает отключение, то ставит снова — каждую минуту
        self.disable()
        events = []
        for _ in range(5):
            events += self.answer(node_answer(sessions=(), skipped=("last_server",)), advance=60)
            events += self.answer(node_answer(), advance=60)
        self.assertEqual([(e.kind, e.cause) for e in events], [(EventKind.RESTORED, RestoreCause.GUARD)])

    def test_next_episode_reports_the_guard_again(self):
        self.disable()
        self.answer(node_answer(sessions=(), skipped=("last_server",)), advance=60)
        self.answer(node_answer(), advance=60)
        self.step(snap(reading(0.0)), advance=1)
        self.step(snap(reading(0.0)), advance=600)
        self.disable()
        events = self.answer(node_answer(sessions=(), skipped=("last_server",)), advance=60)
        self.assertEqual(kinds(events), [EventKind.RESTORED])

    def test_partial_refusal_is_remembered_on_a_disabled_pair(self):
        self.step(snap(reading(downs=3)))
        self.answer(node_answer(sessions=(4,), skipped=("share_limit",)))
        pair = self.tracker.pairs[(RELAY, TARGET)]
        self.assertEqual((pair.state, pair.node), (PairState.DISABLED, {"partial": "share_limit"}))


class VanishedAddressTests(TrackerTestCase):
    def test_one_not_found_answer_is_not_the_end(self):
        self.disable()
        self.answer(node_answer(sessions=(), found=False), advance=60)
        self.assertEqual(self.state(), PairState.DISABLED)

    def test_two_not_found_answers_close_the_record_silently(self):
        self.disable()
        self.answer(node_answer(sessions=(), found=False), advance=60)
        self.assertEqual(self.answer(node_answer(sessions=(), found=False), advance=60), [])
        self.assertEqual(self.tracker.desired(), {})

    def test_found_answer_between_resets_the_count(self):
        self.disable()
        self.answer(node_answer(sessions=(), found=False), advance=60)
        self.answer(node_answer(sessions=(0,)), advance=60)
        self.answer(node_answer(sessions=(), found=False), advance=60)
        self.assertEqual(self.state(), PairState.DISABLED)

    def test_pending_pair_of_a_missing_address_shows_why(self):
        self.step(snap(reading(downs=3)))
        self.answer(node_answer(sessions=(), found=False))
        pair = self.tracker.pairs[(RELAY, TARGET)]
        self.assertEqual((pair.state, pair.node), (PairState.BLOCKED, {"error": "not_found"}))


class ReturnTests(TrackerTestCase):
    def test_returns_after_term_and_calm(self):
        self.disable()
        self.assertEqual(self.step(snap(reading(4.0)), advance=1), [])
        [event] = self.step(snap(reading(4.0)), advance=600)
        self.assertEqual((event.kind, event.cause, event.observe), (EventKind.RESTORED, RestoreCause.CALM, False))
        self.assertEqual(self.state(), PairState.IDLE)
        self.assertEqual(self.tracker.desired(), {})

    def test_term_holds_even_through_a_long_calm(self):
        self.disable()
        self.disable_again_after_return()
        # Второе отключение — на 30 минут: десять минут затишья его не снимают
        self.step(snap(reading(0.0)), advance=1)
        self.assertEqual(self.step(snap(reading(0.0)), advance=900), [])
        self.assertEqual(self.state(), PairState.DISABLED)

    def disable_again_after_return(self) -> None:
        self.step(snap(reading(0.0)), advance=600)
        self.step(snap(reading(0.0)), advance=600)
        self.disable()

    def test_spike_restarts_the_calm(self):
        self.disable()
        self.step(snap(reading(4.0)), advance=600)
        self.step(snap(reading(6.0)), advance=500)
        self.assertEqual(self.step(snap(reading(4.0)), advance=10), [])
        self.assertEqual(self.step(snap(reading(4.0)), advance=590), [])
        self.assertEqual(kinds(self.step(snap(reading(4.0)), advance=10)), [EventKind.RESTORED])

    def test_calm_is_half_the_threshold(self):
        self.disable()
        self.step(snap(reading(5.0)), advance=600)
        self.assertEqual(self.step(snap(reading(5.0)), advance=600), [])
        self.assertEqual(self.state(), PairState.DISABLED)

    def test_stale_reading_right_after_return_does_not_disable_again(self):
        # Нода обнуляет падения при возврате, но замер, снятый до него, панель ещё считает свежим 90 с
        self.disable()
        self.step(snap(reading(0.0, downs=3)), advance=1)
        self.assertEqual(kinds(self.step(snap(reading(0.0, downs=3)), advance=600)), [EventKind.RESTORED])
        self.step(snap(reading(0.0, downs=3)), advance=89)
        self.assertEqual(self.tracker.desired(), {})
        # Падения держатся и после этого — сервер и правда снова падает
        self.step(snap(reading(0.0, downs=3)), advance=2)
        self.assertEqual(self.tracker.desired(), {RELAY: [TARGET]})

    def test_downs_do_not_hold_a_disabled_server(self):
        # В обслуживании HAProxy сервер не проверяет: счётчик падений там молчит
        self.disable()
        self.step(snap(reading(0.0, downs=3)), advance=600)
        self.assertEqual(kinds(self.step(snap(reading(0.0, downs=3)), advance=600)), [EventKind.RESTORED])


class ExemptionTests(TrackerTestCase):
    def loss_for(self, **kwargs) -> PairState | None:
        self.step(snap(reading(50.0, downs=9)), **kwargs)
        self.step(snap(reading(50.0, downs=9)), advance=600, **kwargs)
        return self.state()

    def test_exempt_endpoint(self):
        self.assertIsNone(self.loss_for(exempt=Exemptions(endpoints=frozenset({(IP, 443)}))))

    def test_exempt_ip_covers_every_port(self):
        self.assertIsNone(self.loss_for(exempt=Exemptions(ips=frozenset({IP}))))

    def test_exempt_endpoint_leaves_other_ports(self):
        self.assertEqual(self.loss_for(exempt=Exemptions(endpoints=frozenset({(IP, 8443)}))), PairState.PENDING)

    def test_exempt_relay(self):
        self.assertIsNone(self.loss_for(exempt=Exemptions(server_ids=frozenset({RELAY}))))

    def test_hidden_address(self):
        registry = LossRegistry()
        registry.set_excluded_targets([ExcludedTarget(IP, None)])
        self.assertIsNone(self.loss_for(rules=registry.target_rules()))

    def test_exempting_a_disabled_address_returns_it_at_once(self):
        self.disable()
        [event] = self.step(snap(reading(50.0)), advance=10, exempt=Exemptions(ips=frozenset({IP})))
        self.assertEqual((event.kind, event.cause), (EventKind.RESTORED, RestoreCause.EXEMPT))
        self.assertEqual(self.tracker.desired(), {})


class TotalOnlyTests(TrackerTestCase):
    def setUp(self):
        super().setUp()
        registry = LossRegistry()
        registry.set_excluded_targets([ExcludedTarget(IP, None, total_only=True)])
        self.rules = registry.target_rules()

    def test_partial_loss_and_downs_are_the_norm_there(self):
        self.step(snap(reading(60.0, downs=9)), rules=self.rules)
        self.step(snap(reading(60.0, downs=9)), advance=600, rules=self.rules)
        self.assertIsNone(self.state())

    def test_total_loss_disables_after_the_sustain_time(self):
        self.step(snap(reading(96.0)), rules=self.rules)
        self.step(snap(reading(96.0)), advance=180, rules=self.rules)
        self.assertEqual(self.state(), PairState.PENDING)

    def test_returns_once_loss_is_below_total(self):
        self.step(snap(reading(96.0)), rules=self.rules)
        self.step(snap(reading(96.0)), advance=180, rules=self.rules)
        self.answer(node_answer())
        self.step(snap(reading(90.0)), advance=600, rules=self.rules)
        self.assertEqual(kinds(self.step(snap(reading(90.0)), advance=600, rules=self.rules)), [EventKind.RESTORED])


class RelayTroubleTests(TrackerTestCase):
    def fleet(self, bad: int, total: int = 6, loss: float = 40.0) -> RelaySnapshot:
        return snap(*(reading(loss if i < bad else 0.0, ip=f"10.0.0.{i + 1}") for i in range(total)))

    def test_relay_losing_most_addresses_disables_nothing(self):
        [paused] = self.step(self.fleet(4))
        self.assertEqual((paused.kind, paused.relay_id, paused.relay_name), (EventKind.RELAY_PAUSED, RELAY, "relay1"))
        self.assertEqual(self.step(self.fleet(4), advance=600), [])
        self.assertEqual(self.tracker.desired(), {})
        self.assertEqual(list(self.tracker.paused_relays()), [RELAY])

    def test_pause_begins_before_the_first_pair_could_be_disabled(self):
        # Падения дают решение сразу: без проверки «беды» раньше удержания их отключило бы первым же циклом
        self.step(snap(*(reading(downs=5, ip=f"10.0.0.{i + 1}") for i in range(4))))
        self.assertEqual(self.tracker.desired(), {})

    def test_half_is_not_most(self):
        self.step(self.fleet(3))
        self.step(self.fleet(3), advance=180)
        self.assertEqual(len(self.tracker.desired()[RELAY]), 3)

    def test_two_bad_of_three_is_not_relay_trouble(self):
        self.step(self.fleet(2, total=3))
        self.step(self.fleet(2, total=3), advance=180)
        self.assertEqual(len(self.tracker.desired()[RELAY]), 2)

    def test_leaving_the_pause_does_not_disable_in_a_volley(self):
        self.step(self.fleet(4))
        self.step(self.fleet(4), advance=600)
        # Плохим остался один адрес: пауза держится ещё удержание, затем таймеры идут с нуля
        self.step(self.fleet(1), advance=10)
        self.step(self.fleet(1), advance=179)
        self.assertEqual(list(self.tracker.paused_relays()), [RELAY])
        self.step(self.fleet(1), advance=1)
        self.assertEqual(self.tracker.paused_relays(), {})
        self.assertEqual(self.tracker.desired(), {})
        self.step(self.fleet(1), advance=179)
        self.assertEqual(self.tracker.desired(), {})
        self.step(self.fleet(1), advance=1)
        self.assertEqual(self.tracker.desired(), {RELAY: ["10.0.0.1:443"]})

    def test_downs_gathered_during_the_pause_do_not_disable_after_it(self):
        # Пока у релея не было сети, HAProxy ронял все серверы: падения лежат в окне ещё полчаса
        trouble = snap(*(reading(40.0, downs=6, ip=f"10.0.0.{i + 1}") for i in range(6)))
        self.step(trouble)
        self.step(trouble, advance=300)
        after = snap(*(reading(0.0, downs=6 if i == 0 else 0, ip=f"10.0.0.{i + 1}") for i in range(6)))
        self.step(after, advance=10)
        self.step(after, advance=180)
        self.assertEqual(self.tracker.paused_relays(), {})
        self.assertEqual(self.tracker.desired(), {})
        self.step(after, advance=1790)
        self.assertEqual(self.tracker.desired(), {})
        # Окно падений ноды прошло: такие падения уже новые
        self.step(after, advance=20)
        self.assertEqual(self.tracker.desired(), {RELAY: ["10.0.0.1:443"]})

    def test_loss_still_disables_while_old_downs_are_ignored(self):
        trouble = snap(*(reading(40.0, downs=6, ip=f"10.0.0.{i + 1}") for i in range(6)))
        self.step(trouble)
        after = snap(*(reading(40.0 if i == 0 else 0.0, downs=0, ip=f"10.0.0.{i + 1}") for i in range(6)))
        self.step(after, advance=10)
        self.step(after, advance=180)
        self.step(after, advance=180)
        self.assertEqual(self.tracker.desired(), {RELAY: ["10.0.0.1:443"]})

    def test_pending_and_blocked_pairs_do_not_apply_during_the_pause(self):
        calm = [reading(0.0, ip=f"10.0.0.{i}") for i in range(2, 7)]
        self.step(snap(reading(downs=3, ip="10.0.0.1"), *calm))
        self.answer(node_answer("10.0.0.1:443", sessions=(), skipped=("share_limit",)))
        self.assertEqual(self.tracker.desired(), {RELAY: ["10.0.0.1:443"]})
        self.step(self.fleet(4), advance=10)
        self.assertEqual(self.tracker.desired(), {})

    def test_quarter_bad_is_still_calm_enough_to_leave(self):
        self.step(self.fleet(5, total=8))
        self.step(self.fleet(2, total=8), advance=10)
        self.step(self.fleet(2, total=8), advance=180)
        self.assertEqual(self.tracker.paused_relays(), {})

    def test_relapse_keeps_the_pause(self):
        self.step(self.fleet(4))
        self.step(self.fleet(1), advance=10)
        self.step(self.fleet(4), advance=100)
        self.step(self.fleet(1), advance=100)
        self.assertEqual(list(self.tracker.paused_relays()), [RELAY])

    def test_already_disabled_pairs_follow_the_usual_return(self):
        self.disable(ip="10.0.0.1")
        self.step(self.fleet(4), advance=10)
        self.assertEqual(self.tracker.desired(), {RELAY: ["10.0.0.1:443"]})

    def test_disabled_pairs_still_lossy_count_as_bad(self):
        # Два адреса отключены и ещё теряют 7 % (ниже порога, выше половины), третий плохой сейчас
        for ip in ("10.0.0.1", "10.0.0.2"):
            self.disable(ip=ip)
        lossy = [reading(7.0, ip="10.0.0.1"), reading(7.0, ip="10.0.0.2"), reading(40.0, ip="10.0.0.3"),
                 reading(0.0, ip="10.0.0.4"), reading(0.0, ip="10.0.0.5")]
        self.assertEqual(kinds(self.step(snap(*lossy), advance=10)), [EventKind.RELAY_PAUSED])

    def test_other_relays_are_not_paused(self):
        self.step(self.fleet(4), snap(reading(downs=3), relay_id=2))
        self.assertEqual(self.tracker.desired(), {2: [TARGET]})


class NoDataTests(TrackerTestCase):
    def test_silent_relay_changes_nothing(self):
        self.disable()
        self.assertEqual(self.step(advance=600), [])
        self.assertEqual(self.state(), PairState.DISABLED)

    def test_gap_in_data_restarts_the_calm(self):
        self.disable()
        self.step(snap(reading(0.0)), advance=600)
        self.step(advance=100)
        self.assertEqual(self.step(snap(reading(0.0)), advance=500), [])
        self.assertEqual(kinds(self.step(snap(reading(0.0)), advance=600)), [EventKind.RESTORED])

    def test_an_hour_without_data_closes_the_record_silently(self):
        self.disable()
        self.assertEqual(self.step(advance=3600), [])
        self.assertEqual(self.state(), PairState.DISABLED)
        self.assertEqual(self.step(advance=1), [])
        self.assertEqual(self.tracker.desired(), {})

    def test_address_missing_from_readings_is_no_data_not_a_removal(self):
        self.disable()
        self.assertEqual(self.step(snap(reading(0.0, ip="10.0.0.9")), advance=60), [])
        self.assertEqual(self.state(), PairState.DISABLED)

    def test_sustain_time_does_not_survive_a_gap(self):
        self.step(snap(reading(12.0)))
        self.step(advance=100)
        self.step(snap(reading(12.0)), advance=80)
        self.assertIsNone(self.state())


class ManualReleaseTests(TrackerTestCase):
    def test_release_reports_and_pauses_the_pair_for_an_hour(self):
        self.disable()
        self.now += 10
        [event] = self.tracker.release(RELAY, TARGET, self.now)
        self.assertEqual((event.kind, event.cause), (EventKind.RESTORED, RestoreCause.MANUAL))
        self.assertEqual(self.tracker.desired(), {})
        self.step(snap(reading(50.0, downs=9)), advance=3599)
        self.assertEqual(self.tracker.desired(), {})
        self.step(snap(reading(50.0, downs=9)), advance=1)
        self.assertEqual(self.tracker.desired(), {RELAY: [TARGET]})

    def test_release_of_an_unknown_pair_is_nothing(self):
        self.assertEqual(self.tracker.release(RELAY, TARGET, self.now), [])

    def test_release_of_a_pending_pair_is_silent_but_holds(self):
        self.step(snap(reading(downs=3)))
        self.assertEqual(self.tracker.release(RELAY, TARGET, self.now), [])
        self.step(snap(reading(downs=3)), advance=10)
        self.assertEqual(self.tracker.desired(), {})


class ChronicTests(TrackerTestCase):
    def cycle(self) -> list[Event]:
        events = self.disable()
        term = events[0].term_sec
        self.step(snap(reading(0.0)), advance=1)
        self.step(snap(reading(0.0)), advance=max(term, POLICY.calm_sec))
        return events

    def test_fourth_disable_of_a_pair_marks_the_address(self):
        for _ in range(3):
            self.assertEqual(kinds(self.cycle()), [EventKind.DISABLED])
        events = self.disable()
        self.assertEqual(kinds(events), [EventKind.DISABLED, EventKind.CHRONIC])
        chronic = events[1]
        self.assertEqual((chronic.target, chronic.strikes, chronic.relays), (TARGET, 4, ("relay1",)))
        self.assertEqual(self.tracker.chronic_targets(self.now, POLICY), {TARGET: 4})

    def test_address_is_reported_once(self):
        for _ in range(4):
            self.cycle()
        self.assertEqual(kinds(self.disable()), [EventKind.DISABLED])

    def test_backend_down_everywhere_is_not_a_chronic_address(self):
        events = []
        for relay_id in range(1, 6):
            events += self.disable(relay_id=relay_id)
        self.assertEqual(kinds(events), [EventKind.DISABLED] * 5)
        self.assertEqual(self.tracker.chronic_targets(self.now, POLICY), {})


class ObserveModeTests(TrackerTestCase):
    mode = Mode.OBSERVE

    def test_decision_is_reported_and_nothing_is_wanted(self):
        [event] = self.step(snap(reading(downs=3)))
        self.assertEqual((event.kind, event.observe, event.term_sec), (EventKind.DISABLED, True, 0))
        self.assertEqual(self.state(), PairState.WOULD_DISABLE)
        self.assertEqual(self.tracker.desired(), {})

    def test_stays_listed_while_the_cause_holds(self):
        self.step(snap(reading(downs=3)))
        self.assertEqual(self.step(snap(reading(downs=3)), advance=3600), [])
        self.assertEqual(self.state(), PairState.WOULD_DISABLE)

    def test_leaves_the_list_after_calm(self):
        self.step(snap(reading(downs=3)))
        self.step(snap(reading(0.0, downs=2)), advance=10)
        [event] = self.step(snap(reading(0.0, downs=2)), advance=600)
        self.assertEqual((event.kind, event.observe, event.cause), (EventKind.RESTORED, True, RestoreCause.CALM))

    def test_entries_count_towards_chronic(self):
        for _ in range(3):
            self.step(snap(reading(downs=3)), advance=100)
            self.step(snap(reading(0.0, downs=0)), advance=10)
            self.step(snap(reading(0.0, downs=0)), advance=600)
        self.assertEqual(kinds(self.step(snap(reading(downs=3)), advance=100)), [EventKind.DISABLED, EventKind.CHRONIC])


class ClearTests(TrackerTestCase):
    def test_only_disabled_pairs_are_reported(self):
        self.disable(ip="10.0.0.1")
        self.step(snap(reading(downs=3, ip="10.0.0.2")), advance=1)
        events = self.tracker.clear(self.now, RestoreCause.MODE)
        self.assertEqual([(e.kind, e.cause, e.target) for e in events],
                         [(EventKind.RESTORED, RestoreCause.MODE, "10.0.0.1:443")])
        self.assertEqual(self.tracker.desired(), {})

    def test_clear_ends_a_relay_pause(self):
        self.step(snap(*(reading(40.0, ip=f"10.0.0.{i}") for i in range(1, 5))))
        self.tracker.clear(self.now, RestoreCause.MODE)
        self.assertEqual(self.tracker.paused_relays(), {})


class DesiredOrderTests(TrackerTestCase):
    def test_worst_loss_goes_first(self):
        relay_readings = [reading(15.0, ip="10.0.0.1"), reading(40.0, ip="10.0.0.2"), reading(0.0, ip="10.0.0.3"),
                          reading(0.0, ip="10.0.0.4"), reading(0.0, ip="10.0.0.5")]
        self.step(snap(*relay_readings))
        self.step(snap(*relay_readings), advance=180)
        self.assertEqual(self.tracker.desired(), {RELAY: ["10.0.0.2:443", "10.0.0.1:443"]})


class StorageTests(TrackerTestCase):
    def test_pair_survives_a_round_trip(self):
        self.disable()
        pair = self.tracker.pairs[(RELAY, TARGET)]
        self.assertEqual(Pair.from_dict(pair.to_dict()), pair)

    def test_stored_pair_is_plain_json(self):
        self.disable()
        stored = self.tracker.pairs[(RELAY, TARGET)].to_dict()
        self.assertEqual((stored["state"], stored["reason_kind"]), ("disabled", "downs"))

    def test_restored_tracker_keeps_terms(self):
        self.disable()
        restored = QuarantineTracker()
        restored.pairs = {key: Pair.from_dict(pair.to_dict()) for key, pair in self.tracker.pairs.items()}
        self.tracker = restored
        self.step(snap(reading(0.0)), advance=1)
        self.assertEqual(kinds(self.step(snap(reading(0.0)), advance=600)), [EventKind.RESTORED])


class RegistryDownsTests(unittest.TestCase):
    def test_downs_are_parsed_from_node_metrics(self):
        metrics = {"loss_probe": [
            {"ip": IP, "port": 443, "loss_pct": 1.0, "rtt_ms": 40.0, "samples": 60, "downs": 4},
            {"ip": "10.0.0.6", "port": 443, "loss_pct": 1.0, "rtt_ms": 40.0, "samples": 60, "downs": None},
            {"ip": "10.0.0.7", "port": 443, "loss_pct": 1.0, "rtt_ms": 40.0, "samples": 60},
        ]}
        self.assertEqual([r.downs for r in parse_readings(metrics)], [4, None, None])


if __name__ == "__main__":
    unittest.main()
