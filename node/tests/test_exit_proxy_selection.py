"""Exit-прокси: слияние кандидатов, вердикт «здоров», липкий выбор выхода.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"

Закреплённые инварианты: порядок пользователя важнее порядка обнаружения,
новые адреса встают перед WARP, одиночный сетевой сбой (unknown) не
переключает выход, а полное отсутствие здоровых даёт первого по приоритету.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.exit_proxy.models import BuiltinChecks, CheckItem, CheckResult  # noqa: E402
from app.services.exit_proxy.selection import (  # noqa: E402
    REASON_KEEP,
    REASON_NO_CANDIDATES,
    REASON_NO_HEALTHY,
    REASON_PINNED,
    REASON_SWITCHED,
    REASON_UNKNOWN,
    DiscoveredIp,
    choose_exit,
    health,
    merge_candidates,
)

PRIMARY = DiscoveredIp("5.255.127.33", primary=True)
EXTRA = DiscoveredIp("5.255.127.34", managed=True)
BUILTIN = BuiltinChecks()


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


class HealthTest(unittest.TestCase):
    def test_no_result_or_failed_transport_is_unknown(self):
        self.assertIsNone(health(None, ["RU"], BUILTIN))
        self.assertIsNone(health(result(ok=False, error="trace failed"), ["RU"], BUILTIN))

    def test_blocked_country_captcha_gemini_and_custom_fail(self):
        self.assertFalse(health(result(country="RU"), ["RU"], BUILTIN))
        self.assertFalse(health(result(captcha=True), ["RU"], BUILTIN))
        self.assertFalse(health(result(gemini="blocked"), ["RU"], BUILTIN))
        failed = [CheckItem(name="Claude", ok=False, status=403)]
        self.assertFalse(health(result(checks=failed), ["RU"], BUILTIN))

    def test_transient_errors_are_unknown_not_unhealthy(self):
        self.assertIsNone(health(result(country=None), ["RU"], BUILTIN))
        self.assertIsNone(health(result(gemini="error"), ["RU"], BUILTIN))
        timed_out = [CheckItem(name="Claude", ok=False, status=None, detail="no response")]
        self.assertIsNone(health(result(checks=timed_out), ["RU"], BUILTIN))

    def test_disabled_builtin_checks_are_ignored(self):
        lenient = BuiltinChecks(google_country=False, google_captcha=False, gemini=False)
        self.assertTrue(health(result(country="RU", captcha=True, gemini="blocked"), ["RU"], lenient))

    def test_healthy(self):
        self.assertTrue(health(result(checks=[CheckItem(name="Claude", ok=True, status=200)]), ["RU"], BUILTIN))


class ChooseExitTest(unittest.TestCase):
    def setUp(self):
        self.candidates = merge_candidates([PRIMARY, EXTRA], warp_present=True, order=[], disabled=[])
        self.ids = [c.id for c in self.candidates]

    def test_no_enabled_candidates(self):
        disabled = merge_candidates([PRIMARY], warp_present=False, order=[], disabled=["ip:5.255.127.33"])
        self.assertEqual(choose_exit(disabled, {}, None, "auto", None).reason, REASON_NO_CANDIDATES)

    def test_manual_pin_wins_over_health(self):
        verdicts = {self.ids[0]: True, "warp": False}
        decision = choose_exit(self.candidates, verdicts, self.ids[0], "manual", "warp")
        self.assertEqual((decision.candidate, decision.reason), ("warp", REASON_PINNED))

    def test_manual_without_valid_pin_falls_back_to_auto(self):
        decision = choose_exit(self.candidates, {self.ids[0]: True}, None, "manual", "ip:9.9.9.9")
        self.assertEqual(decision.candidate, self.ids[0])

    def test_sticky_current_stays_while_healthy(self):
        verdicts = {self.ids[0]: True, self.ids[1]: True}
        decision = choose_exit(self.candidates, verdicts, self.ids[1], "auto", None)
        self.assertEqual((decision.candidate, decision.reason), (self.ids[1], REASON_KEEP))

    def test_unknown_current_stays_when_nobody_is_confirmed_healthy(self):
        decision = choose_exit(self.candidates, {}, self.ids[1], "auto", None)
        self.assertEqual((decision.candidate, decision.reason), (self.ids[1], REASON_KEEP))

    def test_unhealthy_current_switches_to_first_healthy_by_priority(self):
        verdicts = {self.ids[0]: False, self.ids[1]: True, "warp": True}
        decision = choose_exit(self.candidates, verdicts, self.ids[0], "auto", None)
        self.assertEqual((decision.candidate, decision.reason), (self.ids[1], REASON_SWITCHED))

    def test_healthy_beats_unknown_current(self):
        verdicts = {self.ids[0]: None, "warp": True}
        decision = choose_exit(self.candidates, verdicts, self.ids[0], "auto", None)
        self.assertEqual((decision.candidate, decision.reason), ("warp", REASON_SWITCHED))

    def test_unhealthy_current_prefers_unknown_over_unhealthy(self):
        verdicts = {self.ids[0]: False, self.ids[1]: None, "warp": False}
        decision = choose_exit(self.candidates, verdicts, self.ids[0], "auto", None)
        self.assertEqual((decision.candidate, decision.reason), (self.ids[1], REASON_UNKNOWN))

    def test_nobody_healthy_gives_first_enabled_by_priority(self):
        verdicts = {cid: False for cid in self.ids}
        decision = choose_exit(self.candidates, verdicts, "warp", "auto", None)
        self.assertEqual((decision.candidate, decision.reason), (self.ids[0], REASON_NO_HEALTHY))

    def test_disabled_candidates_never_chosen(self):
        candidates = merge_candidates([PRIMARY, EXTRA], warp_present=False, order=[], disabled=["ip:5.255.127.33"])
        decision = choose_exit(candidates, {"ip:5.255.127.33": True, "ip:5.255.127.34": True}, None, "auto", None)
        self.assertEqual(decision.candidate, "ip:5.255.127.34")


if __name__ == "__main__":
    unittest.main()
