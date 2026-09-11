"""Exit-прокси: слияние кандидатов, счёт проверок, выбор выхода по счёту с подтверждением.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"

Закреплённые инварианты: порядок пользователя важнее порядка обнаружения,
нерасставленный WARP встаёт первым, выигрывает выход с наибольшим числом
пройденных проверок, при равном здоровом счёте — первый по приоритету,
с недоступного или забаненного Google выхода уходят сразу, в остальных
случаях смена ждёт подтверждения следующим прогоном.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.exit_proxy.models import BuiltinChecks, CheckItem, CheckResult  # noqa: E402
from app.services.exit_proxy.selection import (  # noqa: E402
    NO_RESULT,
    REASON_KEEP,
    REASON_NO_CANDIDATES,
    REASON_NO_HEALTHY,
    REASON_PINNED,
    REASON_SWITCHED,
    REASON_UNKNOWN,
    CheckTally,
    Decision,
    DiscoveredIp,
    choose_exit,
    merge_candidates,
    tally,
)

PRIMARY = DiscoveredIp("5.255.127.33", primary=True)
EXTRA = DiscoveredIp("5.255.127.34", managed=True)
BUILTIN = BuiltinChecks()

ALL_PASSED = CheckTally(passed=3)
ONE_FAILED = CheckTally(passed=2, failed=1)
ONE_UNKNOWN = CheckTally(passed=2, unknown=1)
GOOGLE_FAILED = CheckTally(passed=2, failed=1, google_failed=1)


def result(**overrides) -> CheckResult:
    base = dict(ok=True, ip="5.255.127.33", country="NL", captcha=False, gemini="ok", checks=[], checked_at="now")
    base.update(overrides)
    return CheckResult(**base)


class MergeCandidatesTest(unittest.TestCase):
    def test_fresh_discovery_puts_warp_first_then_primary(self):
        merged = merge_candidates([EXTRA, PRIMARY], warp_present=True, order=[], disabled=[])
        self.assertEqual([c.id for c in merged], ["warp", "ip:5.255.127.33", "ip:5.255.127.34"])
        self.assertEqual([c.priority for c in merged], [0, 1, 2])
        self.assertTrue(all(c.enabled for c in merged))

    def test_without_warp_primary_is_first(self):
        merged = merge_candidates([EXTRA, PRIMARY], warp_present=False, order=[], disabled=[])
        self.assertEqual([c.id for c in merged], ["ip:5.255.127.33", "ip:5.255.127.34"])

    def test_user_order_wins_and_new_ip_goes_last(self):
        order = ["ip:5.255.127.33", "warp"]
        merged = merge_candidates([PRIMARY, EXTRA], warp_present=True, order=order, disabled=[])
        self.assertEqual([c.id for c in merged], ["ip:5.255.127.33", "warp", "ip:5.255.127.34"])

    def test_unplaced_warp_goes_before_user_order(self):
        merged = merge_candidates([PRIMARY, EXTRA], warp_present=True, order=["ip:5.255.127.34"], disabled=[])
        self.assertEqual([c.id for c in merged], ["warp", "ip:5.255.127.34", "ip:5.255.127.33"])

    def test_vanished_and_unknown_ids_are_dropped(self):
        merged = merge_candidates([PRIMARY], warp_present=False, order=["ip:1.1.1.1", "warp", "ip:5.255.127.33"], disabled=[])
        self.assertEqual([c.id for c in merged], ["ip:5.255.127.33"])

    def test_disabled_flag_and_kinds(self):
        merged = merge_candidates([PRIMARY], warp_present=True, order=[], disabled=["warp"])
        by_id = {c.id: c for c in merged}
        self.assertFalse(by_id["warp"].enabled)
        self.assertEqual(by_id["warp"].kind, "warp")
        self.assertEqual(by_id["warp"].address, "127.0.0.1:9091")
        self.assertTrue(by_id["ip:5.255.127.33"].primary)


class TallyTest(unittest.TestCase):
    def test_no_result_or_failed_transport_is_unknown_and_urgent(self):
        self.assertEqual(tally(None, ["RU"], BUILTIN), NO_RESULT)
        self.assertEqual(tally(result(ok=False, error="trace failed"), ["RU"], BUILTIN), NO_RESULT)
        self.assertIsNone(NO_RESULT.verdict)
        self.assertTrue(NO_RESULT.urgent)

    def test_google_failures_are_counted_separately_and_urgent(self):
        for counted in (
            tally(result(country="RU"), ["RU"], BUILTIN),
            tally(result(captcha=True), ["RU"], BUILTIN),
            tally(result(gemini="blocked"), ["RU"], BUILTIN),
        ):
            self.assertIs(counted.verdict, False)
            self.assertEqual((counted.failed, counted.google_failed), (1, 1))
            self.assertTrue(counted.urgent)

    def test_custom_failure_is_not_urgent(self):
        failed = [CheckItem(name="Claude", ok=False, status=403)]
        counted = tally(result(checks=failed), ["RU"], BUILTIN)
        self.assertIs(counted.verdict, False)
        self.assertEqual((counted.failed, counted.google_failed), (1, 0))
        self.assertFalse(counted.urgent)

    def test_transient_errors_are_unknown_not_unhealthy(self):
        self.assertIsNone(tally(result(country=None), ["RU"], BUILTIN).verdict)
        self.assertIsNone(tally(result(gemini="error"), ["RU"], BUILTIN).verdict)
        timed_out = [CheckItem(name="Claude", ok=False, status=None, detail="no response")]
        self.assertIsNone(tally(result(checks=timed_out), ["RU"], BUILTIN).verdict)

    def test_disabled_builtin_checks_are_ignored(self):
        lenient = BuiltinChecks(google_country=False, google_captcha=False, gemini=False)
        counted = tally(result(country="RU", captcha=True, gemini="blocked"), ["RU"], lenient)
        self.assertEqual(counted, CheckTally())
        self.assertIs(counted.verdict, True)

    def test_every_enabled_check_is_counted(self):
        checks = [
            CheckItem(name="Claude", ok=True, status=200),
            CheckItem(name="ChatGPT", ok=False, status=403),
            CheckItem(name="Mine", ok=False, status=None, detail="cloudflare challenge, not tested"),
        ]
        counted = tally(result(gemini="error", checks=checks), ["RU"], BUILTIN)
        self.assertEqual(counted, CheckTally(passed=3, failed=1, unknown=2))
        self.assertIs(counted.verdict, False)

    def test_healthy(self):
        counted = tally(result(checks=[CheckItem(name="Claude", ok=True, status=200)]), ["RU"], BUILTIN)
        self.assertEqual(counted, CheckTally(passed=4))
        self.assertIs(counted.verdict, True)

    def test_rank_prefers_more_passed_then_google_then_fewer_failed(self):
        self.assertLess(ALL_PASSED.rank, ONE_FAILED.rank)
        self.assertLess(ONE_UNKNOWN.rank, ONE_FAILED.rank)
        self.assertLess(ONE_FAILED.rank, GOOGLE_FAILED.rank)
        self.assertLess(ONE_FAILED.rank, NO_RESULT.rank)


class ChooseExitTest(unittest.TestCase):
    def setUp(self):
        self.candidates = merge_candidates([PRIMARY, EXTRA], warp_present=True, order=[], disabled=[])
        self.warp, self.primary, self.extra = [c.id for c in self.candidates]

    def choose(self, tallies, current, pending=None, mode="auto", pinned=None) -> Decision:
        return choose_exit(self.candidates, tallies, current, mode, pinned, pending=pending)

    def test_no_enabled_candidates(self):
        disabled = merge_candidates([PRIMARY], warp_present=False, order=[], disabled=["ip:5.255.127.33"])
        self.assertEqual(choose_exit(disabled, {}, None, "auto", None).reason, REASON_NO_CANDIDATES)

    def test_manual_pin_wins_over_score(self):
        decision = self.choose({self.primary: ALL_PASSED, self.warp: ONE_FAILED}, self.primary, mode="manual", pinned=self.warp)
        self.assertEqual((decision.candidate, decision.reason), (self.warp, REASON_PINNED))

    def test_manual_without_valid_pin_falls_back_to_auto(self):
        decision = self.choose({self.primary: ALL_PASSED}, None, mode="manual", pinned="ip:9.9.9.9")
        self.assertEqual(decision.candidate, self.primary)

    def test_first_pick_without_results_is_first_by_priority(self):
        decision = self.choose({}, None)
        self.assertEqual((decision.candidate, decision.reason, decision.pending), (self.warp, REASON_UNKNOWN, None))

    def test_equal_healthy_score_prefers_priority_after_confirmation(self):
        tallies = {self.warp: ALL_PASSED, self.primary: ALL_PASSED, self.extra: ALL_PASSED}
        first = self.choose(tallies, self.primary)
        self.assertEqual((first.candidate, first.reason, first.pending), (self.primary, REASON_KEEP, self.warp))
        confirmed = self.choose(tallies, self.primary, pending=self.warp)
        self.assertEqual((confirmed.candidate, confirmed.reason, confirmed.pending), (self.warp, REASON_SWITCHED, None))

    def test_equal_score_among_unhealthy_keeps_current(self):
        tallies = {self.warp: ONE_FAILED, self.primary: ONE_FAILED, self.extra: ONE_FAILED}
        decision = self.choose(tallies, self.extra)
        self.assertEqual((decision.candidate, decision.reason, decision.pending), (self.extra, REASON_NO_HEALTHY, None))

    def test_unknown_current_stays_when_nobody_scored_better(self):
        decision = self.choose({}, self.extra)
        self.assertEqual((decision.candidate, decision.reason, decision.pending), (self.extra, REASON_KEEP, None))

    def test_google_failure_on_current_switches_at_once(self):
        decision = self.choose({self.primary: GOOGLE_FAILED, self.extra: ALL_PASSED}, self.primary)
        self.assertEqual((decision.candidate, decision.reason, decision.pending), (self.extra, REASON_SWITCHED, None))

    def test_unreachable_current_switches_at_once(self):
        decision = self.choose({self.extra: ALL_PASSED}, self.primary)
        self.assertEqual((decision.candidate, decision.reason, decision.pending), (self.extra, REASON_SWITCHED, None))

    def test_custom_failure_on_current_waits_for_confirmation(self):
        tallies = {self.primary: ONE_FAILED, self.extra: ALL_PASSED}
        first = self.choose(tallies, self.primary)
        self.assertEqual((first.candidate, first.reason, first.pending), (self.primary, REASON_KEEP, self.extra))
        confirmed = self.choose(tallies, self.primary, pending=self.extra)
        self.assertEqual((confirmed.candidate, confirmed.reason, confirmed.pending), (self.extra, REASON_SWITCHED, None))

    def test_pending_for_another_candidate_does_not_confirm(self):
        decision = self.choose({self.primary: ONE_FAILED, self.extra: ALL_PASSED}, self.primary, pending=self.warp)
        self.assertEqual((decision.candidate, decision.pending), (self.primary, self.extra))

    def test_more_passed_checks_beat_priority(self):
        decision = self.choose({self.warp: ONE_FAILED, self.primary: ONE_FAILED, self.extra: ALL_PASSED}, None)
        self.assertEqual((decision.candidate, decision.reason), (self.extra, REASON_SWITCHED))

    def test_at_equal_passed_google_failure_ranks_below_custom_failure(self):
        decision = self.choose({self.primary: GOOGLE_FAILED, self.extra: ONE_FAILED}, None)
        self.assertEqual((decision.candidate, decision.reason), (self.extra, REASON_NO_HEALTHY))

    def test_at_equal_passed_a_timeout_beats_a_confirmed_block_after_confirmation(self):
        tallies = {self.warp: ONE_FAILED, self.primary: ONE_FAILED, self.extra: ONE_UNKNOWN}
        first = self.choose(tallies, self.primary)
        self.assertEqual((first.candidate, first.reason, first.pending), (self.primary, REASON_KEEP, self.extra))
        confirmed = self.choose(tallies, self.primary, pending=self.extra)
        self.assertEqual((confirmed.candidate, confirmed.reason), (self.extra, REASON_UNKNOWN))

    def test_nobody_full_takes_the_best_score_not_the_primary(self):
        tallies = {self.warp: ONE_FAILED, self.primary: CheckTally(passed=1, failed=2), self.extra: ONE_FAILED}
        first = self.choose(tallies, self.primary)
        self.assertEqual((first.candidate, first.reason, first.pending), (self.primary, REASON_NO_HEALTHY, self.warp))
        confirmed = self.choose(tallies, self.primary, pending=self.warp)
        self.assertEqual((confirmed.candidate, confirmed.reason), (self.warp, REASON_NO_HEALTHY))

    def test_disabled_candidates_never_chosen(self):
        candidates = merge_candidates([PRIMARY, EXTRA], warp_present=False, order=[], disabled=["ip:5.255.127.33"])
        decision = choose_exit(candidates, {"ip:5.255.127.33": ALL_PASSED, "ip:5.255.127.34": ALL_PASSED}, None, "auto", None)
        self.assertEqual(decision.candidate, "ip:5.255.127.34")


if __name__ == "__main__":
    unittest.main()
