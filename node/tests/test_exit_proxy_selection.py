"""Exit-прокси: слияние кандидатов, счёт проверок, выбор выхода по счёту.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"

Закреплённые инварианты: порядок пользователя важнее порядка обнаружения,
новые адреса встают перед WARP, выигрывает выход с наибольшим числом
пройденных проверок, при равном счёте текущий остаётся, а одиночный сетевой
сбой (unknown) сам по себе выход не переключает.
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


def result(**overrides) -> CheckResult:
    base = dict(ok=True, ip="5.255.127.33", country="NL", captcha=False, gemini="ok", checks=[], checked_at="now")
    base.update(overrides)
    return CheckResult(**base)


class MergeCandidatesTest(unittest.TestCase):
    def test_fresh_discovery_puts_primary_first_and_warp_last(self):
        merged = merge_candidates([EXTRA, PRIMARY], warp_present=True, order=[], disabled=[])
        self.assertEqual([c.id for c in merged], ["ip:5.255.127.33", "ip:5.255.127.34", "warp"])
        self.assertEqual([c.priority for c in merged], [0, 1, 2])
        self.assertTrue(all(c.enabled for c in merged))

    def test_user_order_wins_and_new_ip_goes_before_warp(self):
        order = ["warp", "ip:5.255.127.33"]
        merged = merge_candidates([PRIMARY, EXTRA], warp_present=True, order=order, disabled=[])
        self.assertEqual([c.id for c in merged], ["ip:5.255.127.34", "warp", "ip:5.255.127.33"])

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
    def test_no_result_or_failed_transport_is_unknown(self):
        self.assertEqual(tally(None, ["RU"], BUILTIN), NO_RESULT)
        self.assertEqual(tally(result(ok=False, error="trace failed"), ["RU"], BUILTIN), NO_RESULT)
        self.assertIsNone(NO_RESULT.verdict)

    def test_blocked_country_captcha_gemini_and_custom_fail(self):
        self.assertIs(tally(result(country="RU"), ["RU"], BUILTIN).verdict, False)
        self.assertIs(tally(result(captcha=True), ["RU"], BUILTIN).verdict, False)
        self.assertIs(tally(result(gemini="blocked"), ["RU"], BUILTIN).verdict, False)
        failed = [CheckItem(name="Claude", ok=False, status=403)]
        self.assertIs(tally(result(checks=failed), ["RU"], BUILTIN).verdict, False)

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

    def test_rank_prefers_more_passed_then_fewer_failed(self):
        self.assertLess(ALL_PASSED.rank, ONE_FAILED.rank)
        self.assertLess(ONE_UNKNOWN.rank, ONE_FAILED.rank)
        self.assertLess(ONE_FAILED.rank, NO_RESULT.rank)


class ChooseExitTest(unittest.TestCase):
    def setUp(self):
        self.candidates = merge_candidates([PRIMARY, EXTRA], warp_present=True, order=[], disabled=[])
        self.ids = [c.id for c in self.candidates]

    def test_no_enabled_candidates(self):
        disabled = merge_candidates([PRIMARY], warp_present=False, order=[], disabled=["ip:5.255.127.33"])
        self.assertEqual(choose_exit(disabled, {}, None, "auto", None).reason, REASON_NO_CANDIDATES)

    def test_manual_pin_wins_over_score(self):
        tallies = {self.ids[0]: ALL_PASSED, "warp": ONE_FAILED}
        decision = choose_exit(self.candidates, tallies, self.ids[0], "manual", "warp")
        self.assertEqual((decision.candidate, decision.reason), ("warp", REASON_PINNED))

    def test_manual_without_valid_pin_falls_back_to_auto(self):
        decision = choose_exit(self.candidates, {self.ids[0]: ALL_PASSED}, None, "manual", "ip:9.9.9.9")
        self.assertEqual(decision.candidate, self.ids[0])

    def test_sticky_current_stays_at_equal_score(self):
        tallies = {self.ids[0]: ALL_PASSED, self.ids[1]: ALL_PASSED}
        decision = choose_exit(self.candidates, tallies, self.ids[1], "auto", None)
        self.assertEqual((decision.candidate, decision.reason), (self.ids[1], REASON_KEEP))

    def test_unknown_current_stays_when_nobody_scored_better(self):
        decision = choose_exit(self.candidates, {}, self.ids[1], "auto", None)
        self.assertEqual((decision.candidate, decision.reason), (self.ids[1], REASON_KEEP))

    def test_more_passed_checks_beat_the_primary(self):
        tallies = {self.ids[0]: ONE_FAILED, self.ids[1]: ALL_PASSED, "warp": ONE_FAILED}
        decision = choose_exit(self.candidates, tallies, None, "auto", None)
        self.assertEqual((decision.candidate, decision.reason), (self.ids[1], REASON_SWITCHED))

    def test_current_with_a_failure_switches_to_first_full_score_by_priority(self):
        tallies = {self.ids[0]: ONE_FAILED, self.ids[1]: ALL_PASSED, "warp": ALL_PASSED}
        decision = choose_exit(self.candidates, tallies, self.ids[0], "auto", None)
        self.assertEqual((decision.candidate, decision.reason), (self.ids[1], REASON_SWITCHED))

    def test_full_score_beats_unknown_current(self):
        tallies = {self.ids[0]: ONE_UNKNOWN, "warp": ALL_PASSED}
        decision = choose_exit(self.candidates, tallies, self.ids[0], "auto", None)
        self.assertEqual((decision.candidate, decision.reason), ("warp", REASON_SWITCHED))

    def test_at_equal_passed_a_timeout_beats_a_confirmed_block(self):
        tallies = {self.ids[0]: ONE_FAILED, self.ids[1]: ONE_UNKNOWN, "warp": ONE_FAILED}
        decision = choose_exit(self.candidates, tallies, self.ids[0], "auto", None)
        self.assertEqual((decision.candidate, decision.reason), (self.ids[1], REASON_UNKNOWN))

    def test_confirmed_passes_beat_a_never_checked_candidate(self):
        tallies = {self.ids[0]: ONE_FAILED, "warp": ONE_FAILED}
        decision = choose_exit(self.candidates, tallies, self.ids[0], "auto", None)
        self.assertEqual((decision.candidate, decision.reason), (self.ids[0], REASON_NO_HEALTHY))

    def test_nobody_full_keeps_current_at_equal_score_and_reports_no_healthy(self):
        tallies = {cid: ONE_FAILED for cid in self.ids}
        decision = choose_exit(self.candidates, tallies, "warp", "auto", None)
        self.assertEqual((decision.candidate, decision.reason), ("warp", REASON_NO_HEALTHY))

    def test_nobody_full_takes_the_best_score_not_the_primary(self):
        tallies = {self.ids[0]: CheckTally(passed=1, failed=2), self.ids[1]: ONE_FAILED, "warp": ONE_FAILED}
        decision = choose_exit(self.candidates, tallies, self.ids[0], "auto", None)
        self.assertEqual((decision.candidate, decision.reason), (self.ids[1], REASON_NO_HEALTHY))

    def test_disabled_candidates_never_chosen(self):
        candidates = merge_candidates([PRIMARY, EXTRA], warp_present=False, order=[], disabled=["ip:5.255.127.33"])
        decision = choose_exit(candidates, {"ip:5.255.127.33": ALL_PASSED, "ip:5.255.127.34": ALL_PASSED}, None, "auto", None)
        self.assertEqual(decision.candidate, "ip:5.255.127.34")


if __name__ == "__main__":
    unittest.main()
