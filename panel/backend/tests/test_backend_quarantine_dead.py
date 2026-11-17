"""Tests for the dead-address state of backend auto-disable.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

«Адрес умер» — единственное, что оператор получает в Telegram в режиме «только
умершие адреса». Ложное «умер» поднимает его ночью зря, пропущенное — оставляет
без сигнала о заблокированном адресе. Опаснее всего перебои самой панели: связь
с релеями пропала, панель перезапустили — состав релеев адреса при этом не менялся.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.backend_quarantine import (  # noqa: E402
    Event,
    EventKind,
    Mode,
    Pair,
    PairState,
    QuarantinePolicy,
    ReasonKind,
)
from app.services.backend_quarantine_dead import (  # noqa: E402
    HOLDER_MEMORY_SEC,
    REVIVE_HOLD_SEC,
    DeadTracker,
    revive_hold,
)

IP = "193.141.60.129"
TARGET = f"{IP}:8443"
SECOND_IP = "193.141.60.130"
SECOND = f"{SECOND_IP}:8443"
OWNERS = {IP: "nl-3", SECOND_IP: "nl-3"}
NOW = 1_700_000_000.0


def pair(relay_id: int, state: PairState = PairState.DISABLED, reason: ReasonKind = ReasonKind.LOSS,
         target: str = TARGET) -> Pair:
    return Pair(relay_id=relay_id, relay_name=f"relay{relay_id:02d}", target=target, state=state,
                reason_kind=reason, loss_pct=80.0, downs=0, decided_at=NOW, last_data_at=NOW)


def pairs(*relay_ids: int, target: str = TARGET) -> list[Pair]:
    return [pair(relay_id, target=target) for relay_id in relay_ids]


def votes(*relay_ids: int, target: str = TARGET) -> dict[str, set[int]]:
    return {target: set(relay_ids)}


def kinds(events: list[Event]) -> list[EventKind]:
    return [event.kind for event in events]


class DeadTestCase(unittest.TestCase):
    def setUp(self):
        self.tracker = DeadTracker()
        self.now = NOW

    def observe(self, failing=(), voters=None, owners=OWNERS, mode: Mode = Mode.ENFORCE, share: int = 70,
                hold: float = REVIVE_HOLD_SEC, advance: float = 0.0, quiet: bool = False) -> list[Event]:
        self.now += advance
        return self.tracker.observe(list(failing), voters or {}, owners, mode, share, hold, self.now, quiet)

    def die(self, *relay_ids: int, target: str = TARGET) -> list[Event]:
        return self.observe(pairs(*relay_ids, target=target), votes(*relay_ids, target=target))


class DyingTests(DeadTestCase):
    def test_address_disabled_on_enough_relays_dies_once(self):
        everyone = votes(*range(1, 11))

        [event] = self.observe(pairs(*range(1, 8)), everyone)

        self.assertEqual((event.kind, event.target, event.failing, event.total), (EventKind.DEAD, TARGET, 7, 10))
        self.assertEqual((event.owner, event.observe), ("nl-3", False))
        self.assertEqual(self.observe(pairs(*range(1, 8)), everyone, advance=60), [])

    def test_below_threshold_stays_silent(self):
        self.assertEqual(self.observe(pairs(*range(1, 7)), votes(*range(1, 11))), [])
        self.assertEqual(self.tracker.dead, {})

    def test_single_relay_address_dies(self):
        [event] = self.die(1)
        self.assertEqual((event.failing, event.total), (1, 1))

    def test_enforce_mode_counts_pending_and_blocked(self):
        decided = [pair(1, PairState.PENDING), pair(2, PairState.BLOCKED), pair(3, PairState.IDLE),
                   pair(4, PairState.WOULD_DISABLE)]

        [event] = self.observe(decided, votes(1, 2, 3, 4), share=50)

        self.assertEqual((event.failing, event.total), (2, 4))

    def test_observe_mode_counts_would_disable(self):
        watched = [pair(relay_id, PairState.WOULD_DISABLE) for relay_id in (1, 2, 3)]

        [event] = self.observe(watched, votes(1, 2, 3), mode=Mode.OBSERVE)

        self.assertEqual((event.kind, event.observe, event.failing), (EventKind.DEAD, True, 3))

    def test_observe_mode_ignores_real_disables(self):
        self.assertEqual(self.observe(pairs(1, 2, 3), votes(1, 2, 3), mode=Mode.OBSERVE), [])

    def test_reasons_are_distinct_and_ordered(self):
        failing = [pair(1, reason=ReasonKind.NO_REPLY), pair(2), pair(3)]

        [event] = self.observe(failing, votes(1, 2, 3))

        self.assertEqual(event.reasons, (ReasonKind.LOSS, ReasonKind.NO_REPLY))

    def test_history_relay_is_first_failing_by_name(self):
        [event] = self.observe(pairs(9, 3), votes(3, 9))
        self.assertEqual((event.relay_id, event.relay_name), (3, "relay03"))


class DenominatorTests(DeadTestCase):
    """Перебой связи панели с релеями состав релеев адреса не меняет."""

    def test_relay_that_stopped_reporting_stays_in_denominator(self):
        self.assertEqual(self.observe(pairs(1, 2), votes(*range(1, 11))), [])

        # Замеры восьми релеев устарели: голосуют только двое, у которых адрес отключён
        self.assertEqual(self.observe(pairs(1, 2), votes(1, 2), advance=120), [])

        [event] = self.observe(pairs(1, 2), votes(1, 2), advance=HOLDER_MEMORY_SEC + 1)
        self.assertEqual((event.failing, event.total), (2, 2))

    def test_relay_with_a_pair_counts_even_without_a_vote(self):
        # Релей не на связи, а пара ещё живёт: он и в числителе, и в знаменателе
        [event] = self.observe(pairs(1, 2, 3), votes(1))
        self.assertEqual((event.failing, event.total), (3, 3))

    def test_quiet_cycle_learns_holders_but_announces_nothing(self):
        self.assertEqual(self.observe(pairs(*range(1, 8)), votes(*range(1, 11)), quiet=True), [])
        self.assertEqual(self.tracker.dead, {})

        [event] = self.observe(pairs(*range(1, 8)), votes(1, 2), advance=60)
        self.assertEqual((event.failing, event.total), (7, 10))


class RevivalTests(DeadTestCase):
    def test_revives_only_after_hold(self):
        self.die(1, 2, 3)
        healthy = votes(1, 2, 3)

        self.assertEqual(self.observe(voters=healthy, advance=60), [])
        self.assertEqual(self.observe(voters=healthy, advance=REVIVE_HOLD_SEC - 1), [])
        [event] = self.observe(voters=healthy, advance=1)

        self.assertEqual((event.kind, event.target, event.failing, event.total), (EventKind.ALIVE, TARGET, 0, 3))
        self.assertEqual(event.owner, "nl-3")
        self.assertEqual(self.tracker.dead, {})

    def test_dying_again_within_hold_is_silent(self):
        self.die(1, 2, 3)
        healthy = votes(1, 2, 3)

        self.assertEqual(self.observe(voters=healthy, advance=60), [])
        self.assertEqual(self.observe(pairs(1, 2, 3), healthy, advance=REVIVE_HOLD_SEC // 2), [])
        self.assertEqual(self.observe(voters=healthy, advance=60), [])
        self.assertEqual(self.observe(voters=healthy, advance=REVIVE_HOLD_SEC - 1), [])
        self.assertEqual(kinds(self.observe(voters=healthy, advance=1)), [EventKind.ALIVE])

    def test_partly_restored_address_reports_the_remaining_spread(self):
        self.die(1, 2, 3)

        self.observe(pairs(1), votes(1, 2, 3), advance=60)
        [event] = self.observe(pairs(1), votes(1, 2, 3), advance=REVIVE_HOLD_SEC)

        self.assertEqual((event.kind, event.failing, event.total), (EventKind.ALIVE, 1, 3))

    def test_address_nobody_measures_does_not_revive(self):
        # Ни одного свежего замера: связь с релеями пропала либо адрес убрали из конфигов —
        # сказать «снова работает» не по чему
        self.die(1, 2, 3)

        self.assertEqual(self.observe(advance=REVIVE_HOLD_SEC), [])
        self.assertEqual(self.observe(advance=REVIVE_HOLD_SEC), [])
        self.assertIn(TARGET, self.tracker.dead)

    def test_address_gone_from_fleet_is_forgotten_silently(self):
        self.die(1, 2, 3)

        self.assertEqual(self.observe(advance=HOLDER_MEMORY_SEC + 1), [])
        self.assertEqual(self.tracker.dead, {})

    def test_clear_forgets_dead_silently(self):
        self.die(1)
        self.tracker.clear()
        self.assertEqual(self.tracker.dead, {})

    def test_hold_outlasts_redetection(self):
        self.assertEqual(revive_hold(QuarantinePolicy()), REVIVE_HOLD_SEC)
        self.assertEqual(revive_hold(QuarantinePolicy(sustained_sec=1800)), 3600)


class NodeTests(DeadTestCase):
    def both(self, *relay_ids: int) -> tuple[list[Pair], dict[str, set[int]]]:
        failing = pairs(*relay_ids) + pairs(*relay_ids, target=SECOND)
        return failing, {**votes(1, 2), **votes(1, 2, target=SECOND)}

    def test_node_with_all_addresses_dead_marks_events(self):
        events = self.observe(*self.both(1, 2))
        self.assertEqual(sorted((event.target, event.node_total) for event in events), [(TARGET, 2), (SECOND, 2)])

    def test_last_address_of_node_marks_only_the_new_event(self):
        _, everyone = self.both(1, 2)
        [first] = self.observe(pairs(1, 2), everyone)
        self.assertEqual(first.node_total, 0)

        [second] = self.observe(*self.both(1, 2), advance=60)
        self.assertEqual((second.target, second.node_total), (SECOND, 2))

    def test_single_address_owner_is_not_a_node_event(self):
        [event] = self.die(1)
        self.assertEqual(event.node_total, 0)

    def test_owner_is_remembered_after_registry_forgets_it(self):
        self.observe(voters=votes(1, 2))

        # Нода упала целиком: метрик нет, реестр её уже не знает
        [event] = self.observe(pairs(1, 2), votes(1, 2), owners={}, advance=120)

        self.assertEqual(event.owner, "nl-3")

    def test_node_revival_marks_alive_events(self):
        failing, everyone = self.both(1, 2)
        self.observe(failing, everyone)

        self.observe(voters=everyone, advance=60)
        events = self.observe(voters=everyone, advance=REVIVE_HOLD_SEC)

        self.assertEqual(sorted((event.kind, event.target, event.node_total) for event in events),
                         [(EventKind.ALIVE, TARGET, 2), (EventKind.ALIVE, SECOND, 2)])

    def test_revived_address_of_a_node_that_never_died_is_named(self):
        # У ноды умирал один адрес из двух: «снова работают все адреса» читалось бы как падение всей ноды
        _, everyone = self.both(1, 2)
        self.observe(pairs(1, 2), everyone)

        self.observe(voters=everyone, advance=60)
        [event] = self.observe(voters=everyone, advance=REVIVE_HOLD_SEC)

        self.assertEqual((event.kind, event.target, event.node_total), (EventKind.ALIVE, TARGET, 0))

    def test_node_with_one_address_still_dead_revives_by_address(self):
        failing, everyone = self.both(1, 2)
        self.observe(failing, everyone)

        self.observe(pairs(1, 2, target=SECOND), everyone, advance=60)
        [event] = self.observe(pairs(1, 2, target=SECOND), everyone, advance=REVIVE_HOLD_SEC)

        self.assertEqual((event.target, event.node_total), (TARGET, 0))


class StorageTests(DeadTestCase):
    """Перезапуск панели: то, что уже объявлено, не объявляется заново, а состав релеев адреса не забывается."""

    def restart(self, advance: float = 300.0) -> None:
        rows = self.tracker.stored()
        self.tracker = DeadTracker()
        self.now += advance
        self.tracker.restore(rows, self.now)

    def test_stored_state_survives_restore_and_does_not_reannounce(self):
        self.die(1, 2, 3)

        self.restart()

        self.assertEqual(self.observe(pairs(1, 2, 3), votes(1, 2, 3), owners={}), [])
        entry = self.tracker.dead[TARGET]
        self.assertEqual((entry.owner, entry.relay_id, entry.relay_name), ("nl-3", 1, "relay01"))

    def test_dead_address_outlives_a_restart_with_silent_relays(self):
        # Панель обновили, а релеи адреса ещё минуты не отвечают: запись не стирается и второй раз не объявляется
        watched = [pair(relay_id, PairState.WOULD_DISABLE) for relay_id in (1, 2, 3)]
        self.observe(watched, votes(1, 2, 3), mode=Mode.OBSERVE)

        self.restart()

        self.assertEqual(self.observe(mode=Mode.OBSERVE, quiet=True), [])
        self.assertEqual(self.observe(mode=Mode.OBSERVE, advance=300), [])
        self.assertIn(TARGET, self.tracker.dead)
        self.assertEqual(self.observe(watched, votes(1, 2, 3), mode=Mode.OBSERVE, advance=1800), [])

    def test_suspect_address_keeps_its_relays_across_a_restart(self):
        # Отключён на 2 из 10. После перезапуска восемь здоровых релеев молчат — это не «2 из 2»
        self.assertEqual(self.observe(pairs(1, 2), votes(*range(1, 11))), [])

        self.restart()

        self.assertEqual(self.observe(pairs(1, 2), votes(1, 2)), [])
        self.assertEqual(self.tracker.dead, {})

    def test_quiet_cycles_after_a_restart_do_not_thin_the_stored_state(self):
        self.observe(pairs(1, 2), votes(*range(1, 11)))
        saved = self.tracker.stored()

        self.restart()
        self.observe(pairs(1, 2), votes(1, 2), quiet=True)

        self.assertEqual(self.tracker.stored(), saved)

    def test_only_suspect_and_dead_addresses_are_stored(self):
        healthy = {**votes(1, 2), **votes(1, 2, target=SECOND)}
        self.observe(pairs(1), healthy, share=100)

        self.assertEqual(self.tracker.stored(), {TARGET: {"holders": [1, 2], "dead": None}})

    def test_dead_node_is_remembered_across_a_restart(self):
        everyone = {**votes(1, 2), **votes(1, 2, target=SECOND)}
        self.observe(pairs(1, 2) + pairs(1, 2, target=SECOND), everyone)

        self.restart()
        self.observe(voters=everyone, owners={}, advance=60)
        events = self.observe(voters=everyone, owners={}, advance=REVIVE_HOLD_SEC)

        self.assertEqual(sorted(event.node_total for event in events), [2, 2])

    def test_unreadable_stored_row_is_skipped(self):
        self.die(1)
        rows = {**self.tracker.stored(), "1.1.1.1:443": {"holders": "many"}, "2.2.2.2:443": "garbage",
                "3.3.3.3:443": {"holders": [1], "dead": {"relay_name": "no id"}}}

        restarted = DeadTracker()
        restarted.restore(rows, self.now)

        self.assertEqual(set(restarted.dead), {TARGET})


if __name__ == "__main__":
    unittest.main()
