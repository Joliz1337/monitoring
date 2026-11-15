"""Tests for the stored settings of backend auto-disable.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Настройки лежат в базе JSON-строкой и переживают версии панели: запись от
старой версии, битая или с лишними полями не должна ни уронить панель на
старте, ни включить автоотключение с порогами, которых никто не выставлял.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.backend_quarantine import Exemptions, Mode, QuarantinePolicy  # noqa: E402
from app.services.backend_quarantine_settings import QuarantineSettings, parse_settings  # noqa: E402

DEFAULTS = {
    "mode": "off",
    "loss_threshold": 50.0,
    "sustained_seconds": 180,
    "downs_threshold": 3,
    "calm_seconds": 600,
    "chronic_strikes": 4,
    "max_share_pct": 50,
    "drop_sessions": True,
    "notify": True,
    "exempt_server_ids": [],
    "exempt_targets": [],
}


def stored(**fields) -> str:
    return json.dumps({**DEFAULTS, **fields})


class DefaultsTests(unittest.TestCase):
    def test_fresh_panel_does_not_disable_anything(self):
        settings = parse_settings(None)
        self.assertIs(settings.mode, Mode.OFF)
        self.assertEqual(settings.to_dict(), DEFAULTS)

    def test_policy_carries_the_documented_defaults(self):
        self.assertEqual(parse_settings(None).policy, QuarantinePolicy(
            loss_threshold=50.0, sustained_sec=180, downs_threshold=3, calm_sec=600,
            chronic_strikes=4, max_share_pct=50, drop_sessions=True,
        ))


class RoundTripTests(unittest.TestCase):
    def test_saved_settings_come_back_the_same(self):
        saved = stored(
            mode="enforce", loss_threshold=62.5, sustained_seconds=300, downs_threshold=5, calm_seconds=900,
            chronic_strikes=6, max_share_pct=30, drop_sessions=False, notify=False,
            exempt_server_ids=[7, 3], exempt_targets=["1.2.3.4", "1.2.3.4:8443", "[2001:db8::1]:443"],
        )
        settings = parse_settings(saved)
        self.assertEqual(settings.to_dict(), {**json.loads(saved), "exempt_server_ids": [3, 7]})
        self.assertEqual(parse_settings(json.dumps(settings.to_dict())), settings)

    def test_policy_follows_the_saved_values(self):
        policy = parse_settings(stored(loss_threshold=62.5, sustained_seconds=300, calm_seconds=900,
                                       max_share_pct=30, drop_sessions=False)).policy
        self.assertEqual((policy.loss_threshold, policy.sustained_sec, policy.calm_sec,
                          policy.max_share_pct, policy.drop_sessions), (62.5, 300, 900, 30, False))


class BoundsTests(unittest.TestCase):
    def test_values_below_the_floor_are_raised(self):
        settings = parse_settings(stored(loss_threshold=0.5, sustained_seconds=5, downs_threshold=1,
                                         calm_seconds=10, chronic_strikes=1, max_share_pct=0))
        self.assertEqual(
            {key: settings.to_dict()[key] for key in ("loss_threshold", "sustained_seconds", "downs_threshold",
                                                       "calm_seconds", "chronic_strikes", "max_share_pct")},
            {"loss_threshold": 50.0, "sustained_seconds": 60, "downs_threshold": 2,
             "calm_seconds": 120, "chronic_strikes": 2, "max_share_pct": 10},
        )

    def test_threshold_saved_by_the_first_version_is_raised(self):
        # Первая версия отключала с 10 % потерь и сняла бы рабочий сервер с потерями на пути
        self.assertEqual(parse_settings(stored(mode="enforce", loss_threshold=10.0)).policy.loss_threshold, 50.0)

    def test_values_above_the_ceiling_are_lowered(self):
        settings = parse_settings(stored(loss_threshold=99, sustained_seconds=99999, downs_threshold=100,
                                         calm_seconds=99999, chronic_strikes=100, max_share_pct=100))
        self.assertEqual(
            {key: settings.to_dict()[key] for key in ("loss_threshold", "sustained_seconds", "downs_threshold",
                                                       "calm_seconds", "chronic_strikes", "max_share_pct")},
            {"loss_threshold": 90.0, "sustained_seconds": 1800, "downs_threshold": 20,
             "calm_seconds": 3600, "chronic_strikes": 20, "max_share_pct": 90},
        )


class DamagedRecordTests(unittest.TestCase):
    def test_broken_json_is_the_default(self):
        for raw in ("{broken", "[]", "null", '"enforce"', ""):
            with self.subTest(raw=raw):
                self.assertEqual(parse_settings(raw), QuarantineSettings())

    def test_unknown_mode_is_off(self):
        self.assertIs(parse_settings(stored(mode="wipe")).mode, Mode.OFF)

    def test_wrong_types_fall_back_field_by_field(self):
        settings = parse_settings(stored(mode="observe", loss_threshold="many", sustained_seconds=None,
                                         exempt_server_ids="all", exempt_targets={"a": 1}))
        self.assertIs(settings.mode, Mode.OBSERVE)
        self.assertEqual((settings.policy.loss_threshold, settings.policy.sustained_sec), (50.0, 180))
        self.assertEqual((settings.exempt_server_ids, settings.exempt_targets), (frozenset(), ()))

    def test_missing_fields_are_defaults(self):
        self.assertEqual(parse_settings('{"mode": "enforce"}').to_dict(), {**DEFAULTS, "mode": "enforce"})

    def test_unknown_fields_are_ignored(self):
        self.assertEqual(parse_settings(stored(future_option=1)), QuarantineSettings())


class ExemptTargetTests(unittest.TestCase):
    def test_addresses_are_parsed_and_garbage_dropped(self):
        settings = parse_settings(stored(exempt_targets=[
            "1.2.3.4", " 1.2.3.4:8443 ", "[2001:db8::1]:443", "example.com", "1.2.3.4:99999", 5, "0.0.0.0",
        ]))
        self.assertEqual(settings.exempt_targets, (("1.2.3.4", None), ("1.2.3.4", 8443), ("2001:db8::1", 443)))

    def test_repeated_address_is_kept_once(self):
        settings = parse_settings(stored(exempt_targets=["1.2.3.4:443", "1.2.3.4:443"]))
        self.assertEqual(settings.to_dict()["exempt_targets"], ["1.2.3.4:443"])

    def test_exemptions_split_whole_addresses_from_endpoints(self):
        settings = parse_settings(stored(exempt_server_ids=[3], exempt_targets=["1.2.3.4", "5.6.7.8:8443"]))
        self.assertEqual(settings.exemptions(), Exemptions(
            server_ids=frozenset({3}), ips=frozenset({"1.2.3.4"}), endpoints=frozenset({("5.6.7.8", 8443)}),
        ))


if __name__ == "__main__":
    unittest.main()
