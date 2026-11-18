"""Tests for the packet-loss alert: registry, episodes and the Telegram digest.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Инварианты: сообщение — только при смене статуса адреса (новые / усилились /
снизились), мигающий адрес не шлёт пар «есть / нет», отсутствие данных не
изменение, сообщение сгруппировано по адресу и делится только между адресами.
"""

import os
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError  # noqa: E402

from app.models import AlertSettings  # noqa: E402
from app.routers import alerts  # noqa: E402
from app.services import server_alerter  # noqa: E402
from app.services.latest_metrics import LatestMetrics  # noqa: E402
from app.services.loss_alerts import (  # noqa: E402
    MIN_SAMPLES,
    OUTAGE_SETTLE_SEC,
    RELAY_JOIN_WINDOW_SEC,
    TOTAL_LEVEL,
    VERDICT_WAIT_FROM_SEC,
    VERDICT_WAIT_UNTIL_SEC,
    WIDE_SHARE_PCT,
    Episode,
    NO_DATA_CLOSE_SEC,
    EventKind,
    LossEvent,
    LossPolicy,
    LossScope,
    LossTracker,
    OutageWatch,
    RelayEvent,
    RelayEventKind,
    RelayReading,
    collect_observations,
    format_digest,
    held_targets,
    history_records,
    history_text,
    total_only_targets,
)
from app.services.loss_overview import (  # noqa: E402
    CheckStatus,
    TargetParseError,
    build_overview,
    check_from_servers,
    format_endpoint,
    parse_endpoint,
    parse_target,
)
from app.services.loss_registry import (  # noqa: E402
    ExcludedTarget,
    LossReading,
    TargetMode,
    TargetRules,
    LossRegistry,
    RelaySnapshot,
    parse_addresses,
    parse_readings,
)

POLICY = LossPolicy(threshold=20.0, sustained_sec=300, calm_sec=900)
TARGET = "62.50.146.225:8443"


def relay(loss: float, relay_id: int = 1, name: str = "VK relay 1", rtt: float | None = 45.0,
          ip: str = "62.50.146.225") -> RelayReading:
    return RelayReading(relay_id, name, LossReading(ip, 8443, loss, rtt, 60))


def observe(*relays: RelayReading) -> dict[str, list[RelayReading]]:
    observations: dict[str, list[RelayReading]] = {}
    for r in relays:
        observations.setdefault(r.reading.key, []).append(r)
    return observations


def kinds(events) -> list[tuple[EventKind, str]]:
    return [(e.kind, e.target) for e in events]


def interfaces(*ips: str) -> dict:
    return {"network": {"interfaces": [{"addresses": [{"type": "ipv4", "address": ip} for ip in ips]}]}}


class RegistryTests(unittest.TestCase):
    def test_parses_probe_block_and_ipv4_addresses(self):
        metrics = {
            "loss_probe": [{"ip": "10.0.0.5", "port": 443, "loss_pct": 40.0, "rtt_ms": None, "samples": 12}, "junk"],
            "network": {"interfaces": [{"addresses": [
                {"type": "ipv4", "address": "62.50.146.225"},
                {"type": "ipv6", "address": "fe80::1"},
            ]}]},
        }
        self.assertEqual(parse_readings(metrics), (LossReading("10.0.0.5", 443, 40.0, None, 12),))
        self.assertEqual(parse_addresses(metrics), frozenset({"62.50.146.225"}))

    def test_stale_nodes_drop_out(self):
        now = [1000.0]
        registry = LossRegistry(clock=lambda: now[0])
        registry.update(1, "exit", {"network": {"interfaces": [{"addresses": [{"type": "ipv4", "address": "1.2.3.4"}]}]}})
        self.assertEqual(registry.owners(), {"1.2.3.4": "exit"})
        now[0] += 91
        self.assertEqual((registry.fresh(), registry.owners()), ([], {}))

    def test_silence_is_counted_from_the_last_answer(self):
        now = [1000.0]
        registry = LossRegistry(clock=lambda: now[0])
        self.assertIsNone(registry.silent_for(1))
        registry.update(1, "exit", {})
        now[0] += 45
        self.assertEqual(registry.silent_for(1), 45)

    def test_live_addresses_skip_listed_and_silent_nodes(self):
        now = [1000.0]
        registry = LossRegistry(clock=lambda: now[0])
        registry.update(1, "gone", interfaces("1.2.3.4"))
        now[0] += 91
        registry.update(2, "standby", interfaces("5.6.7.8", "10.0.0.1"))
        registry.update(3, "returned", interfaces("9.9.9.9"))
        self.assertEqual(registry.live_addresses(except_ids={3}), frozenset({"5.6.7.8", "10.0.0.1"}))

    def test_excluded_servers_drop_out_as_relays_and_as_destinations(self):
        registry = LossRegistry(clock=lambda: 0.0)
        registry.update(1, "relay", {"loss_probe": [
            {"ip": "62.50.146.225", "port": 443, "loss_pct": 40.0, "rtt_ms": 1.0, "samples": 60},
            {"ip": "45.145.56.96", "port": 8449, "loss_pct": 8.0, "rtt_ms": 1.0, "samples": 60},
        ]})
        registry.update(2, "bot", {
            "loss_probe": [{"ip": "62.50.146.225", "port": 443, "loss_pct": 90.0, "rtt_ms": None, "samples": 60}],
            "network": {"interfaces": [{"addresses": [{"type": "ipv4", "address": "45.145.56.96"}]}]},
        })
        registry.set_excluded({2})
        self.assertEqual([s.server_id for s in registry.fresh()], [1])
        self.assertEqual(registry.target_rules(), TargetRules(ips={"45.145.56.96": TargetMode.HIDDEN}))
        observations = collect_observations(registry.fresh(), set(), registry.target_rules())
        self.assertEqual({k: [r.relay_id for r in v] for k, v in observations.items()}, {"62.50.146.225:443": [1]})
        rows = build_overview(registry.fresh(), registry.owners(), {}, registry.target_rules())
        self.assertEqual([r["target"] for r in rows], ["62.50.146.225:443"])

    def test_address_silent_to_the_client_probe_tops_the_page_as_not_working(self):
        # Подключения доходят (потери 3 %), а клиенту сервер не отвечает
        registry = LossRegistry(clock=lambda: 0.0)
        registry.update(1, "relay", {"loss_probe": [
            {"ip": "62.50.146.225", "port": 443, "loss_pct": 3.0, "rtt_ms": 46.0, "samples": 60, "answers": False},
            {"ip": "45.145.56.96", "port": 443, "loss_pct": 20.0, "rtt_ms": 46.0, "samples": 60, "answers": True},
        ]})
        rows = build_overview(registry.fresh(), registry.owners(), {}, registry.target_rules())
        self.assertEqual([(r["target"], r["worst_loss"], r["relays"][0]["answers"]) for r in rows],
                         [("62.50.146.225:443", 100.0, False), ("45.145.56.96:443", 20.0, True)])

    def test_excluded_targets_hide_whole_ip_or_one_backend(self):
        registry = LossRegistry(clock=lambda: 0.0)
        registry.update(1, "relay", {"loss_probe": [
            {"ip": "62.50.146.225", "port": 443, "loss_pct": 40.0, "rtt_ms": 1.0, "samples": 60},
            {"ip": "62.50.146.225", "port": 8443, "loss_pct": 40.0, "rtt_ms": 1.0, "samples": 60},
            {"ip": "45.145.56.96", "port": 443, "loss_pct": 40.0, "rtt_ms": 1.0, "samples": 60},
            {"ip": "45.145.56.96", "port": 8449, "loss_pct": 40.0, "rtt_ms": 1.0, "samples": 60},
        ]})
        registry.set_excluded_targets([ExcludedTarget("62.50.146.225", 8443), ExcludedTarget("45.145.56.96", None)])
        rows = build_overview(registry.fresh(), registry.owners(), {}, registry.target_rules())
        self.assertEqual([r["target"] for r in rows], ["62.50.146.225:443"])
        observations = collect_observations(registry.fresh(), set(), registry.target_rules())
        self.assertEqual(list(observations), ["62.50.146.225:443"])

    def test_backend_rule_beats_whole_ip_rule(self):
        registry = LossRegistry(clock=lambda: 0.0)
        registry.update(2, "exit", {"network": {"interfaces": [{"addresses": [{"type": "ipv4", "address": "45.145.56.96"}]}]}})
        registry.set_excluded({2})
        registry.set_excluded_targets([ExcludedTarget("45.145.56.96", 443, total_only=True)])
        rules = registry.target_rules()
        self.assertIs(rules.mode(LossReading("45.145.56.96", 443, 0, None, 60)), TargetMode.TOTAL_ONLY)
        self.assertIs(rules.mode(LossReading("45.145.56.96", 8449, 0, None, 60)), TargetMode.HIDDEN)
        self.assertIs(rules.mode(LossReading("62.50.146.225", 443, 0, None, 60)), TargetMode.TRACKED)

    def test_total_only_target_shows_only_on_total_loss_or_open_episode(self):
        snapshots = [RelaySnapshot(1, "relay", (
            LossReading("62.50.146.225", 443, 60.0, 1.0, 60),
            LossReading("62.50.146.225", 8443, 96.7, None, 60),
            LossReading("45.145.56.96", 443, 40.0, 1.0, 60),
        ), frozenset(), 0)]
        rules = TargetRules(ips={"62.50.146.225": TargetMode.TOTAL_ONLY, "45.145.56.96": TargetMode.TOTAL_ONLY})
        rows = build_overview(snapshots, {}, {}, rules)
        self.assertEqual([(r["target"], r["total_only"]) for r in rows], [("62.50.146.225:8443", True)])
        episode = Episode(level=3, opened_at=0, last_data_at=0, notified_at=0)
        rows = build_overview(snapshots, {}, {"45.145.56.96:443": episode}, rules)
        self.assertEqual([r["target"] for r in rows], ["62.50.146.225:8443", "45.145.56.96:443"])
        self.assertFalse(build_overview(snapshots, {}, {}, TargetRules())[0]["total_only"])

    def test_exclusion_list_round_trip(self):
        from app.services.loss_exclusions import format_ids, format_targets, parse_ids, parse_targets, target_entry
        self.assertEqual(parse_ids(format_ids([7, 3, 3])), {3, 7})
        self.assertEqual(parse_ids(" 5, x ,, 9"), {5, 9})
        self.assertEqual(parse_ids(None), set())
        targets = (
            ExcludedTarget("62.50.146.225", 8443), ExcludedTarget("45.145.56.96", None, total_only=True),
            ExcludedTarget("2001:db8::1", 443), ExcludedTarget("2001:db8::2", None),
        )
        self.assertEqual(
            [target_entry(t)["target"] for t in targets],
            ["62.50.146.225:8443", "45.145.56.96", "[2001:db8::1]:443", "2001:db8::2"],
        )
        self.assertEqual(parse_targets(format_targets(targets)), targets)
        raw = ('[{"target": "1.2.3.4"}, {"target": "junk"}, "x", {"target": "1.2.3.4:99999"},'
               ' {"target": " 1.2.3.4 ", "total_only": true}]')
        self.assertEqual(parse_targets(raw), (ExcludedTarget("1.2.3.4", None, total_only=True),))
        for broken in (None, "", "not json", '{"target": "1.2.3.4"}'):
            with self.subTest(raw=broken):
                self.assertEqual(parse_targets(broken), ())

    def test_observations_skip_short_windows_and_excluded_relays(self):
        snapshots = [
            RelaySnapshot(1, "a", (LossReading("10.0.0.5", 443, 40.0, 1.0, MIN_SAMPLES),
                                   LossReading("10.0.0.6", 443, 90.0, 1.0, MIN_SAMPLES - 1)), frozenset(), 0),
            RelaySnapshot(2, "b", (LossReading("10.0.0.5", 443, 50.0, 1.0, 60),), frozenset(), 0),
        ]
        observations = collect_observations(snapshots, excluded={2})
        self.assertEqual(list(observations), ["10.0.0.5:443"])
        self.assertEqual([r.relay_id for r in observations["10.0.0.5:443"]], [1])


class EpisodeTests(unittest.TestCase):
    def test_new_only_after_sustained(self):
        tracker = LossTracker()
        self.assertEqual(tracker.evaluate(observe(relay(40)), POLICY, 0), [])
        self.assertEqual(tracker.evaluate(observe(relay(40)), POLICY, 299), [])
        self.assertEqual(kinds(tracker.evaluate(observe(relay(40)), POLICY, 300)), [(EventKind.NEW, TARGET)])

    def test_ongoing_loss_is_silent(self):
        tracker = LossTracker()
        tracker.evaluate(observe(relay(40)), POLICY, 0)
        tracker.evaluate(observe(relay(40)), POLICY, 300)
        for now in range(360, 20000, 60):
            self.assertEqual(tracker.evaluate(observe(relay(40)), POLICY, now), [])

    def test_worse_on_level_up_only(self):
        tracker = LossTracker()
        tracker.evaluate(observe(relay(40)), POLICY, 0)
        tracker.evaluate(observe(relay(40)), POLICY, 300)
        tracker.evaluate(observe(relay(60)), POLICY, 360)
        worse = tracker.evaluate(observe(relay(60)), POLICY, 660)
        self.assertEqual([(e.kind, e.level) for e in worse], [(EventKind.WORSE, 2)])
        # Спад обратно к 30% внутри эпизода — тишина
        self.assertEqual(tracker.evaluate(observe(relay(30)), POLICY, 1000), [])

    def test_flapping_address_stays_in_episode(self):
        tracker = LossTracker()
        tracker.evaluate(observe(relay(40)), POLICY, 0)
        tracker.evaluate(observe(relay(40)), POLICY, 300)
        # 15 минут: минута тихо, минута всплеск выше половины порога
        for step, now in enumerate(range(360, 3000, 60)):
            loss = 2 if step % 2 == 0 else 15
            self.assertEqual(tracker.evaluate(observe(relay(loss)), POLICY, now), [], msg=f"t={now}")

    def test_recovered_after_calm_period(self):
        tracker = LossTracker()
        tracker.evaluate(observe(relay(40)), POLICY, 0)
        tracker.evaluate(observe(relay(40)), POLICY, 300)
        self.assertEqual(tracker.evaluate(observe(relay(3)), POLICY, 400), [])
        self.assertEqual(tracker.evaluate(observe(relay(3)), POLICY, 1299), [])
        self.assertEqual(kinds(tracker.evaluate(observe(relay(3)), POLICY, 1300)), [(EventKind.RECOVERED, TARGET)])
        self.assertEqual(tracker.episodes, {})

    def test_calm_needs_every_relay(self):
        tracker = LossTracker()
        tracker.evaluate(observe(relay(40)), POLICY, 0)
        tracker.evaluate(observe(relay(40)), POLICY, 300)
        mixed = observe(relay(1), relay(15, relay_id=2, name="Timeweb"))
        self.assertEqual(tracker.evaluate(mixed, POLICY, 400), [])
        self.assertEqual(tracker.evaluate(mixed, POLICY, 2000), [])

    def test_missing_data_is_not_a_change(self):
        tracker = LossTracker()
        tracker.evaluate(observe(relay(40)), POLICY, 0)
        tracker.evaluate(observe(relay(40)), POLICY, 300)
        # Релей перезапускается: адреса нет в данных — ни «снизились», ни новых
        self.assertEqual(tracker.evaluate({}, POLICY, 400), [])
        self.assertEqual(tracker.evaluate(observe(relay(40)), POLICY, 2000), [])
        self.assertIn(TARGET, tracker.episodes)

    def test_episode_without_data_closes_silently(self):
        tracker = LossTracker()
        tracker.evaluate(observe(relay(40)), POLICY, 0)
        tracker.evaluate(observe(relay(40)), POLICY, 300)
        self.assertEqual(tracker.evaluate({}, POLICY, 300 + NO_DATA_CLOSE_SEC), [])
        self.assertEqual(tracker.episodes, {})

    def test_restored_episode_is_not_reported_again(self):
        # Перезапуск панели: таймеры пустые, эпизод поднят из базы
        first = LossTracker()
        first.evaluate(observe(relay(40)), POLICY, 0)
        first.evaluate(observe(relay(40)), POLICY, 300)
        restored = LossTracker(episodes=dict(first.episodes))
        for now in range(360, 2000, 60):
            self.assertEqual(restored.evaluate(observe(relay(40)), POLICY, now), [])

    def test_reminder_only_when_enabled(self):
        policy = LossPolicy(threshold=20.0, sustained_sec=300, calm_sec=900, reminder_sec=3600)
        tracker = LossTracker()
        tracker.evaluate(observe(relay(40)), policy, 0)
        tracker.evaluate(observe(relay(40)), policy, 300)
        self.assertEqual(tracker.evaluate(observe(relay(40)), policy, 3899), [])
        self.assertEqual(kinds(tracker.evaluate(observe(relay(40)), policy, 3900)), [(EventKind.STILL, TARGET)])

    def test_loss_below_severity_levels_uses_threshold_only(self):
        self.assertEqual(LossPolicy(60, 300, 900).level_bounds(), [60, 90])
        self.assertEqual(LossPolicy(95, 300, 900).level_bounds(), [95])

    def test_opens_with_current_level_without_worse_right_after(self):
        # Обрыв: окно ноды доходит до порога сразу, а до 100% — через пару минут
        tracker = LossTracker()
        tracker.evaluate(observe(relay(30)), POLICY, 0)
        tracker.evaluate(observe(relay(100)), POLICY, 120)
        opened = tracker.evaluate(observe(relay(100)), POLICY, 300)
        self.assertEqual([(e.kind, e.level) for e in opened], [(EventKind.NEW, 3)])
        for now in range(360, 1200, 60):
            self.assertEqual(tracker.evaluate(observe(relay(100)), POLICY, now), [], msg=f"t={now}")


class HeldTargetTests(unittest.TestCase):
    """Адрес сервера, о недоступности которого панель уже сообщила: про потери
    до него ничего нового не пишется, замеры при этом идут своим чередом."""

    HELD = frozenset({TARGET})

    def test_no_new_loss_while_held(self):
        tracker = LossTracker()
        for now in range(0, 3600, 60):
            self.assertEqual(tracker.evaluate(observe(relay(100)), POLICY, now, held=self.HELD), [], msg=f"t={now}")
        self.assertEqual(tracker.episodes, {})

    def test_loss_that_outlives_the_hold_is_reported_at_once(self):
        # Сервер вернулся, а порт так и не отвечает: удержание давно пройдено
        tracker = LossTracker()
        for now in (0, 300, 600):
            tracker.evaluate(observe(relay(100)), POLICY, now, held=self.HELD)
        opened = tracker.evaluate(observe(relay(100)), POLICY, 660)
        self.assertEqual([(e.kind, e.level) for e in opened], [(EventKind.NEW, 3)])

    def test_open_episode_gets_neither_worse_nor_reminder_while_held(self):
        policy = LossPolicy(threshold=20.0, sustained_sec=300, calm_sec=900, reminder_sec=3600)
        tracker = LossTracker()
        tracker.evaluate(observe(relay(40)), policy, 0)
        tracker.evaluate(observe(relay(40)), policy, 300)
        for now in range(360, 8000, 60):
            self.assertEqual(tracker.evaluate(observe(relay(100)), policy, now, held=self.HELD), [], msg=f"t={now}")
        self.assertEqual(tracker.episodes[TARGET].level, 1)

    def test_open_episode_still_recovers_while_held(self):
        # Лежит только агент ноды: релеи до адреса доходят, и объявленные потери закрываются
        tracker = LossTracker()
        tracker.evaluate(observe(relay(40)), POLICY, 0)
        tracker.evaluate(observe(relay(40)), POLICY, 300)
        self.assertEqual(tracker.evaluate(observe(relay(0)), POLICY, 400, held=self.HELD), [])
        recovered = tracker.evaluate(observe(relay(0)), POLICY, 1300, held=self.HELD)
        self.assertEqual(kinds(recovered), [(EventKind.RECOVERED, TARGET)])

    def test_other_targets_are_not_held(self):
        tracker = LossTracker()
        both = observe(relay(100), relay(100, ip="45.145.56.96"))
        tracker.evaluate(both, POLICY, 0, held=self.HELD)
        self.assertEqual(
            kinds(tracker.evaluate(both, POLICY, 300, held=self.HELD)), [(EventKind.NEW, "45.145.56.96:8443")],
        )

    def test_held_targets_are_picked_by_ip(self):
        observations = observe(relay(100), relay(100, ip="45.145.56.96"))
        self.assertEqual(held_targets(observations, frozenset({"62.50.146.225"})), frozenset({TARGET}))
        self.assertEqual(held_targets(observations, frozenset()), frozenset())


class OutageWatchTests(unittest.TestCase):
    """Окно замеров ноды помнит простой ещё пару минут после него: вернувшийся
    сервер не в счёт, пока простой не уйдёт из окон."""

    def test_returned_server_stays_unsettled_for_the_settle_period(self):
        watch = OutageWatch()
        none, down = frozenset(), frozenset({7})
        self.assertEqual(watch.update(none, down, 0), down)
        self.assertEqual(watch.update(down, down, 600), down)
        self.assertEqual(watch.update(down, none, 660), down)
        self.assertEqual(watch.update(none, none, 660 + OUTAGE_SETTLE_SEC - 1), down)
        self.assertEqual(watch.update(none, none, 660 + OUTAGE_SETTLE_SEC), none)

    def test_settle_period_counts_from_the_last_return(self):
        watch = OutageWatch()
        none, down = frozenset(), frozenset({7})
        watch.update(down, none, 0)
        watch.update(none, down, 100)
        self.assertEqual(watch.update(down, none, 200), down)
        self.assertEqual(watch.update(none, none, OUTAGE_SETTLE_SEC + 100), down)
        self.assertEqual(watch.update(none, none, OUTAGE_SETTLE_SEC + 200), none)

    def test_only_servers_that_were_down_settle(self):
        watch = OutageWatch()
        self.assertEqual(watch.update(frozenset({1}), frozenset({2}), 0), frozenset({1, 2}))


VK = (1, "вк 90 1")
FIRST_WAVE = {  # не-Hetzner адреса — потерялись первыми
    "176.97.210.169": "тубо", "176.97.210.175": "тубо", "176.97.210.204": "тубо", "176.97.210.52": "тубо",
    "62.50.144.32": "Ред свитч 3", "62.50.144.34": "Ред свитч 3",
    "62.50.146.173": "Нидерланды ред свич", "62.50.146.174": "Нидерланды ред свич",
    "62.50.146.231": "Нидерланды ред свитч 2", "62.50.146.85": "Нидерланды ред свитч 2",
    "62.50.146.86": "Нидерланды ред свитч 2",
}
HETZNER = {
    **{f"77.42.57.{n}": "Финляндия hetzner" for n in range(73, 79)},
    **{f"77.42.57.{n}": "Финка хетзнер 2" for n in range(105, 111)},
    **{f"77.42.57.{n}": "Финка хетзнер 3" for n in range(97, 103)},
}
OWNERS = {**FIRST_WAVE, **HETZNER}


def fleet(losses: dict[str, float], clean_relays: int = 7, missing: frozenset[int] = frozenset()):
    """Релей VK теряет адреса из losses, остальные релеи до них доходят."""
    rows = []
    for ip, loss in losses.items():
        rows.append(relay(loss, relay_id=VK[0], name=VK[1], ip=ip, rtt=None if loss >= 100 else 19.0))
        rows.extend(relay(0, relay_id=r, name=f"relay-{r}", ip=ip) for r in range(2, 2 + clean_relays))
    return observe(*(r for r in rows if r.relay_id not in missing))


def relay_kinds(events) -> list[tuple]:
    return [(e.kind, e.count, e.added) if isinstance(e, RelayEvent) else (e.kind, e.target) for e in events]


class RelayProblemTests(unittest.TestCase):
    """Один релей теряет много адресов, остальные до них доходят: одно сообщение
    про релей вместо блока на каждый адрес."""

    def test_incident_is_two_messages(self):
        # Инцидент 2026-10-02: 11 адресов сразу, Hetzner FI догоняет волнами ещё 5 минут
        hetzner = list(HETZNER)
        onset = {ip: 0 for ip in FIRST_WAVE}
        for start, ips in ((60, hetzner[:3]), (120, hetzner[3:7]), (180, hetzner[7:14]),
                           (240, hetzner[14:16]), (300, hetzner[16:])):
            onset.update({ip: start for ip in ips})
        tracker = LossTracker()
        messages = {}
        for now in range(0, 2400, 60):
            if now < 900:
                losses = {ip: (100.0 if now - start >= 120 else 30.0) for ip, start in onset.items() if now >= start}
            else:
                losses = {ip: 0.0 for ip in onset}
            events = tracker.evaluate(fleet({ip: losses.get(ip, 0.0) for ip in onset}), POLICY, now)
            if events:
                messages[now] = relay_kinds(events)
        self.assertEqual(messages, {
            300: [(RelayEventKind.OPEN, 11, 0)],
            1800: [(RelayEventKind.RECOVERED, 29, 0)],
        })
        self.assertEqual((tracker.episodes, tracker.incidents), ({}, {}))

    def test_one_or_two_addresses_stay_address_blocks(self):
        tracker = LossTracker()
        losses = {"62.50.144.32": 100.0, "62.50.144.34": 100.0, "62.50.146.86": 0.0}
        tracker.evaluate(fleet(losses), POLICY, 0)
        events = tracker.evaluate(fleet(losses), POLICY, 300)
        self.assertEqual(
            [(type(e), e.kind, e.blamed_relay_id) for e in events],
            [(LossEvent, EventKind.NEW, 1), (LossEvent, EventKind.NEW, 1)],
        )
        self.assertEqual(tracker.incidents, {})

    def open_incident(self, tracker: LossTracker, losses: dict[str, float]) -> None:
        tracker.evaluate(fleet(losses), POLICY, 0)
        events = tracker.evaluate(fleet(losses), POLICY, 300)
        self.assertEqual(relay_kinds(events), [(RelayEventKind.OPEN, len(losses), 0)])

    def test_address_lost_by_other_relays_leaves_the_group(self):
        tracker = LossTracker()
        losses = dict.fromkeys(list(FIRST_WAVE)[:4], 100.0)
        self.open_incident(tracker, losses)
        target = "176.97.210.169"
        both_lose = fleet(losses)
        both_lose[f"{target}:8443"] = [
            relay(100, relay_id=2, name="relay-2", ip=target) if r.relay_id == 2 else r
            for r in both_lose[f"{target}:8443"]
        ]
        tracker.evaluate(both_lose, POLICY, 360)
        events = tracker.evaluate(both_lose, POLICY, 660)
        self.assertEqual([(type(e), e.kind, e.target, e.blamed_relay_id) for e in events],
                         [(LossEvent, EventKind.NEW, f"{target}:8443", None)])
        self.assertIn(VK[0], tracker.incidents)

    def test_held_address_does_not_leave_the_group(self):
        # Сервер адреса упал: его перестали видеть все релеи, но это уже не новость
        tracker = LossTracker()
        losses = dict.fromkeys(list(FIRST_WAVE)[:4], 100.0)
        self.open_incident(tracker, losses)
        target = "176.97.210.169"
        all_lose = fleet(losses)
        all_lose[f"{target}:8443"] = [
            relay(100, relay_id=r.relay_id, name=r.relay_name, ip=target) for r in all_lose[f"{target}:8443"]
        ]
        held = frozenset({f"{target}:8443"})
        for now in range(360, 1200, 60):
            self.assertEqual(tracker.evaluate(all_lose, POLICY, now, held=held), [], msg=f"t={now}")
        self.assertEqual(tracker.episodes[f"{target}:8443"].blamed_relay_id, VK[0])

    def test_growth_after_join_window_is_one_line(self):
        tracker = LossTracker()
        losses = dict.fromkeys(list(FIRST_WAVE)[:3], 100.0)
        self.open_incident(tracker, losses)
        late = 300 + RELAY_JOIN_WINDOW_SEC
        losses["77.42.57.73"] = 100.0
        tracker.evaluate(fleet(losses), POLICY, late)
        events = tracker.evaluate(fleet(losses), POLICY, late + 300)
        self.assertEqual(relay_kinds(events), [(RelayEventKind.GREW, 4, 1)])

    def test_missing_blamed_relay_is_no_data_not_recovery(self):
        tracker = LossTracker()
        losses = dict.fromkeys(list(FIRST_WAVE)[:3], 100.0)
        self.open_incident(tracker, losses)
        # Релей VK пропал из данных, остальные до адресов доходят — это не «снизились»
        for now in range(360, 3000, 60):
            self.assertEqual(tracker.evaluate(fleet(losses, missing=frozenset({VK[0]})), POLICY, now), [])
        self.assertEqual(len(tracker.episodes), 3)
        # Час без данных — эпизоды и проблема релея закрываются молча
        self.assertEqual(tracker.evaluate(fleet(losses, missing=frozenset({VK[0]})), POLICY, 300 + NO_DATA_CLOSE_SEC), [])
        self.assertEqual((tracker.episodes, tracker.incidents), ({}, {}))

    def test_restored_incident_is_not_reported_again(self):
        first = LossTracker()
        losses = dict.fromkeys(FIRST_WAVE, 100.0)
        self.open_incident(first, losses)
        restored = LossTracker(episodes=dict(first.episodes), incidents=dict(first.incidents))
        for now in range(360, 2000, 60):
            self.assertEqual(restored.evaluate(fleet(losses), POLICY, now), [])

    def test_last_address_of_a_relay_without_incident_reports_recovery(self):
        # Один-два адреса релея идут обычными блоками — и «снизились» по ним должно дойти
        tracker = LossTracker()
        tracker.evaluate(fleet({"62.50.144.32": 100.0}), POLICY, 0)
        tracker.evaluate(fleet({"62.50.144.32": 100.0}), POLICY, 300)
        tracker.evaluate(fleet({"62.50.144.32": 0.0}), POLICY, 360)
        events = tracker.evaluate(fleet({"62.50.144.32": 0.0}), POLICY, 1260)
        self.assertEqual(kinds(events), [(EventKind.RECOVERED, "62.50.144.32:8443")])

    def test_reminder_is_about_relay_not_each_address(self):
        policy = LossPolicy(threshold=20.0, sustained_sec=300, calm_sec=900, reminder_sec=3600)
        tracker = LossTracker()
        losses = dict.fromkeys(FIRST_WAVE, 100.0)
        tracker.evaluate(fleet(losses), policy, 0)
        tracker.evaluate(fleet(losses), policy, 300)
        self.assertEqual(tracker.evaluate(fleet(losses), policy, 3899), [])
        self.assertEqual(relay_kinds(tracker.evaluate(fleet(losses), policy, 3900)), [(RelayEventKind.STILL, 11, 0)])

    def test_address_silent_to_the_client_probe_is_named_so_not_by_a_percent(self):
        silent = RelayReading(1, "VK relay 1", LossReading("62.50.146.225", 8443, 100.0, 45.0, 60, answers=False))
        tracker = LossTracker()
        tracker.evaluate(observe(silent), POLICY, 0)
        opened = tracker.evaluate(observe(silent), POLICY, 300)
        [text] = format_digest(opened, {}, POLICY, "ru")
        self.assertIn("VK relay 1 — не отвечает как клиент (45 мс)", text)
        self.assertNotIn("100%", text)
        [record] = history_records(opened, {}, "ru")
        self.assertIn("VK relay 1 не отвечает как клиент", record.message)

    def test_digest_and_history(self):
        tracker = LossTracker()
        losses = dict.fromkeys(FIRST_WAVE, 100.0)
        tracker.evaluate(fleet(losses), POLICY, 0)
        opened = tracker.evaluate(fleet(losses), POLICY, 300)
        [text] = format_digest(opened, OWNERS, POLICY, "ru")
        self.assertIn("<b>Проблема на релее</b>\n<b>вк 90 1</b> — потери до 11 адресов, 100%", text)
        self.assertIn("• другие релеи до них доходят: 7", text)
        self.assertIn("• тубо ×4 · Нидерланды ред свитч 2 ×3 · Нидерланды ред свич ×2 · Ред свитч 3 ×2", text)
        self.assertNotIn("176.97.210.169", text)
        [record] = history_records(opened, OWNERS, "ru")
        self.assertEqual((record.server_id, record.recovered, record.message),
                         (1, False, "Проблема на релее: вк 90 1 — потери до 11 адресов, 100%"))
        self.assertEqual(len(record.details["targets"]), 11)

        recovered = RelayEvent(RelayEventKind.RECOVERED, 1, "вк 90 1", 21)
        [text] = format_digest([recovered], OWNERS, POLICY, "ru")
        self.assertTrue(text.startswith("\U0001f7e2"))
        self.assertIn("<b>вк 90 1</b> — снова доходит до 21 адреса", text)
        self.assertEqual(history_text(recovered, OWNERS, "en"),
                         "Relay problem resolved: вк 90 1 — reaches 21 addresses again")


WIDE = replace(POLICY, scope=LossScope.WIDE)
DEAD = replace(POLICY, scope=LossScope.DEAD)


def crowd(lossy: int, total: int, loss: float = 40.0) -> dict[str, list[RelayReading]]:
    """Адрес проверяют total релеев, первые lossy из них его теряют."""
    return observe(*(
        relay(loss if relay_id <= lossy else 0.0, relay_id=relay_id, name=f"relay-{relay_id}")
        for relay_id in range(1, total + 1)
    ))


def relay_losing(lost: int, watched: int) -> dict[str, list[RelayReading]]:
    """Релей VK проверяет watched адресов и один теряет первые lost из них."""
    return fleet({f"10.0.0.{n}": (100.0 if n <= lost else 0.0) for n in range(1, watched + 1)})


def told(events) -> list[tuple]:
    return [(e.kind, e.quiet) for e in events]


def relay_told(events) -> list[tuple]:
    return [(e.kind, e.count, e.quiet) for e in events]


class ScopeTests(unittest.TestCase):
    """Уровень сообщений: одиночный сбой — только в историю алертов, в Telegram —
    то, что задело не меньше заданной доли релеев. О конце пишется, если писали о начале."""

    def open_episode(self, tracker: LossTracker, observations: dict, policy: LossPolicy) -> list:
        tracker.evaluate(observations, policy, 0)
        return tracker.evaluate(observations, policy, 300)

    def test_every_event_is_told_by_default(self):
        self.assertEqual((POLICY.scope, POLICY.wide_share_pct), (LossScope.ALL, WIDE_SHARE_PCT))
        self.assertEqual(told(self.open_episode(LossTracker(), crowd(1, 18), POLICY)), [(EventKind.NEW, False)])

    def test_address_lost_by_a_few_relays_goes_to_history_only(self):
        # Сообщения 2026-11-18: «селектел 1 — 45%, ещё 17 без потерь» и «…ещё 16 без потерь»
        for lossy in (1, 2):
            events = self.open_episode(LossTracker(), crowd(lossy, 18), WIDE)
            self.assertEqual(told(events), [(EventKind.NEW, True)], msg=f"{lossy} of 18")
            self.assertEqual(format_digest(events, {}, WIDE, "ru"), [])
            [record] = history_records(events, {}, "ru")
            self.assertTrue(record.quiet)

    def test_address_lost_by_half_of_its_relays_is_told(self):
        events = self.open_episode(LossTracker(), crowd(9, 18), WIDE)
        self.assertEqual(told(events), [(EventKind.NEW, False)])
        self.assertEqual(len(format_digest(events, {}, WIDE, "ru")), 1)
        self.assertFalse(history_records(events, {}, "ru")[0].quiet)

    def test_share_of_relays_is_a_setting(self):
        events = self.open_episode(LossTracker(), crowd(2, 18), replace(WIDE, wide_share_pct=10))
        self.assertEqual(told(events), [(EventKind.NEW, False)])

    def test_told_once_the_loss_spreads(self):
        tracker = LossTracker()
        self.open_episode(tracker, crowd(2, 18), WIDE)
        self.assertEqual(tracker.evaluate(crowd(9, 18), WIDE, 360), [])
        self.assertEqual(tracker.evaluate(crowd(9, 18), WIDE, 659), [])
        self.assertEqual(told(tracker.evaluate(crowd(9, 18), WIDE, 660)), [(EventKind.NEW, False)])
        for now in range(720, 2000, 60):
            self.assertEqual(tracker.evaluate(crowd(9, 18), WIDE, now), [], msg=f"t={now}")
        tracker.evaluate(crowd(0, 18), WIDE, 2000)
        self.assertEqual(told(tracker.evaluate(crowd(0, 18), WIDE, 2900)), [(EventKind.RECOVERED, False)])

    def test_spread_of_an_untold_episode_stays_silent_while_its_server_is_held(self):
        # Пара релеев теряла адрес — об этом не писали. Потом сервер упал, и его теряют
        # все: «узкий сбой стал общим» здесь та же новость, что уже сказана алертом
        # о недоступности. Придержку сняли, потери остались — тогда и пишем
        tracker = LossTracker()
        held = frozenset({TARGET})
        self.open_episode(tracker, crowd(2, 18), WIDE)
        for now in range(360, 1500, 60):
            self.assertEqual(tracker.evaluate(crowd(18, 18, loss=100), WIDE, now, held=held), [], msg=f"t={now}")

        self.assertEqual(told(tracker.evaluate(crowd(18, 18, loss=100), WIDE, 1500)), [(EventKind.NEW, False)])

    def test_untold_episode_worsens_and_ends_in_history_only(self):
        tracker = LossTracker()
        self.open_episode(tracker, crowd(2, 18), WIDE)
        tracker.evaluate(crowd(2, 18, loss=60), WIDE, 360)
        self.assertEqual(told(tracker.evaluate(crowd(2, 18, loss=60), WIDE, 660)), [(EventKind.WORSE, True)])
        tracker.evaluate(crowd(0, 18), WIDE, 700)
        ended = tracker.evaluate(crowd(0, 18), WIDE, 1600)
        self.assertEqual(told(ended), [(EventKind.RECOVERED, True)])
        self.assertEqual(format_digest(ended, {}, WIDE, "ru"), [])

    def test_untold_episode_gets_no_reminder(self):
        policy = replace(WIDE, reminder_sec=3600)
        tracker = LossTracker()
        self.open_episode(tracker, crowd(2, 18), policy)
        for now in range(360, 9000, 60):
            self.assertEqual(tracker.evaluate(crowd(2, 18), policy, now), [], msg=f"t={now}")

    def test_short_burst_on_every_relay_does_not_make_the_episode_wide(self):
        # Сервер минуту не отвечал всем: у адреса с давним одиночным сбоем это не «потери везде»
        tracker = LossTracker()
        self.open_episode(tracker, crowd(2, 18), WIDE)
        for now in (360, 420):
            self.assertEqual(tracker.evaluate(crowd(18, 18, loss=100), WIDE, now), [], msg=f"t={now}")
        self.assertEqual(tracker.evaluate(crowd(2, 18), WIDE, 480), [])
        self.assertFalse(tracker.episodes[TARGET].announced)

    def test_dead_scope_waits_until_the_address_barely_answers(self):
        tracker = LossTracker()
        self.assertEqual(told(self.open_episode(tracker, crowd(18, 18), DEAD)), [(EventKind.NEW, True)])
        self.assertEqual(tracker.evaluate(crowd(18, 18, loss=100), DEAD, 360), [])
        died = tracker.evaluate(crowd(18, 18, loss=100), DEAD, 660)
        self.assertEqual([(e.kind, e.level, e.quiet) for e in died], [(EventKind.NEW, TOTAL_LEVEL, False)])
        for now in range(720, 1500, 60):
            self.assertEqual(tracker.evaluate(crowd(18, 18, loss=100), DEAD, now), [], msg=f"t={now}")

    def test_dead_scope_counts_relays_that_lost_the_address_completely(self):
        self.assertEqual(told(self.open_episode(LossTracker(), crowd(8, 18, loss=100), DEAD)),
                         [(EventKind.NEW, True)])
        self.assertEqual(told(self.open_episode(LossTracker(), crowd(9, 18, loss=100), DEAD)),
                         [(EventKind.NEW, False)])

    def test_dead_scope_follows_the_top_level_of_a_high_threshold(self):
        # Порог 60 %: уровней два, верхний — те же 90 %
        policy = LossPolicy(60.0, 300, 900, scope=LossScope.DEAD)
        self.assertEqual(told(self.open_episode(LossTracker(), crowd(18, 18, loss=70), policy)),
                         [(EventKind.NEW, True)])
        self.assertEqual(told(self.open_episode(LossTracker(), crowd(18, 18, loss=95), policy)),
                         [(EventKind.NEW, False)])

    def test_relay_losing_a_few_of_its_addresses_goes_to_history_only(self):
        events = self.open_episode(LossTracker(), relay_losing(3, 10), WIDE)
        self.assertEqual(relay_told(events), [(RelayEventKind.OPEN, 3, True)])
        self.assertEqual(format_digest(events, {}, WIDE, "ru"), [])
        self.assertTrue(history_records(events, {}, "ru")[0].quiet)

    def test_relay_losing_half_of_its_addresses_is_told(self):
        events = self.open_episode(LossTracker(), relay_losing(5, 10), WIDE)
        self.assertEqual(relay_told(events), [(RelayEventKind.OPEN, 5, False)])

    def test_relay_problem_is_told_once_it_grows(self):
        tracker = LossTracker()
        self.open_episode(tracker, relay_losing(3, 10), WIDE)
        tracker.evaluate(relay_losing(5, 10), WIDE, 360)
        self.assertEqual(relay_told(tracker.evaluate(relay_losing(5, 10), WIDE, 660)),
                         [(RelayEventKind.OPEN, 5, False)])
        self.assertEqual(tracker.evaluate(relay_losing(5, 10), WIDE, 720), [])
        tracker.evaluate(relay_losing(0, 10), WIDE, 780)
        self.assertEqual(relay_told(tracker.evaluate(relay_losing(0, 10), WIDE, 1680)),
                         [(RelayEventKind.RECOVERED, 5, False)])

    def test_untold_relay_problem_ends_in_history_only(self):
        policy = replace(WIDE, reminder_sec=3600)
        tracker = LossTracker()
        self.open_episode(tracker, relay_losing(3, 10), policy)
        for now in range(360, 9000, 60):
            self.assertEqual(tracker.evaluate(relay_losing(3, 10), policy, now), [], msg=f"t={now}")
        tracker.evaluate(relay_losing(0, 10), policy, 9000)
        self.assertEqual(relay_told(tracker.evaluate(relay_losing(0, 10), policy, 9900)),
                         [(RelayEventKind.RECOVERED, 3, True)])

    def test_dead_scope_keeps_relay_problems_in_history(self):
        events = self.open_episode(LossTracker(), relay_losing(10, 10), DEAD)
        self.assertEqual(relay_told(events), [(RelayEventKind.OPEN, 10, True)])

    def test_relay_problem_is_told_when_its_addresses_are_wide_themselves(self):
        # Адрес проверяют всего два релея: потерян одним — уже половиной. Таких у релея три из двадцати:
        # для релея это мелочь, но сообщения этих адресов ушли внутрь его проблемы — молчать о ней нельзя
        lost = fleet({f"10.0.0.{n}": 100.0 for n in range(1, 4)}, clean_relays=1)
        healthy = fleet({f"10.0.1.{n}": 0.0 for n in range(1, 18)})
        for policy in (WIDE, DEAD):
            events = self.open_episode(LossTracker(), {**lost, **healthy}, policy)
            self.assertEqual(relay_told(events), [(RelayEventKind.OPEN, 3, False)], msg=policy.scope)

    def test_quiet_relay_problem_is_told_once_its_addresses_become_wide(self):
        tracker = LossTracker()
        self.open_episode(tracker, relay_losing(3, 10), WIDE)
        # Остальные релеи перестали проверять эти три адреса: остался один, и тот до них доходит
        few_watchers = {
            **fleet({f"10.0.0.{n}": 100.0 for n in range(1, 4)}, clean_relays=1),
            **fleet({f"10.0.0.{n}": 0.0 for n in range(4, 11)}),
        }
        self.assertEqual(relay_told(tracker.evaluate(few_watchers, WIDE, 360)), [(RelayEventKind.OPEN, 3, False)])
        self.assertEqual(tracker.evaluate(few_watchers, WIDE, 420), [])

    def test_raising_the_level_tells_about_open_untold_episodes(self):
        tracker = LossTracker()
        self.open_episode(tracker, crowd(2, 18), WIDE)
        self.assertEqual(told(tracker.evaluate(crowd(2, 18), POLICY, 360)), [(EventKind.NEW, False)])
        self.assertEqual(tracker.evaluate(crowd(2, 18), POLICY, 420), [])

    def test_episode_that_is_already_calm_is_not_told_late(self):
        tracker = LossTracker()
        self.open_episode(tracker, crowd(2, 18), WIDE)
        self.assertEqual(tracker.evaluate(crowd(0, 18), POLICY, 360), [])
        self.assertEqual(told(tracker.evaluate(crowd(0, 18), POLICY, 1260)), [(EventKind.RECOVERED, True)])

    def test_restored_untold_episode_stays_untold(self):
        first = LossTracker()
        self.open_episode(first, crowd(2, 18), WIDE)
        restored = LossTracker(episodes=dict(first.episodes))
        for now in range(360, 2000, 60):
            self.assertEqual(restored.evaluate(crowd(2, 18), WIDE, now), [], msg=f"t={now}")
        self.assertFalse(restored.episodes[TARGET].announced)

    def test_digest_leaves_out_what_goes_to_history_only(self):
        everywhere = observe(*(
            relay(40, relay_id=relay_id, name=f"relay-{relay_id}", ip="45.145.56.96") for relay_id in range(1, 4)
        ))
        events = self.open_episode(LossTracker(), {**crowd(2, 18), **everywhere}, WIDE)
        [text] = format_digest(events, {}, WIDE, "ru")
        self.assertIn("45.145.56.96:8443", text)
        self.assertNotIn(TARGET, text)
        self.assertEqual(sorted(record.quiet for record in history_records(events, {}, "ru")), [False, True])


class TotalOnlyEpisodeTests(unittest.TestCase):
    """Адрес с галочкой «только полные потери»: молчит до 95%, открывается сразу
    уровнем «почти не отвечает», снижается, как только все релеи ниже 95%."""

    TOTAL = frozenset({TARGET})

    def test_partial_loss_is_silent(self):
        tracker = LossTracker()
        for now in range(0, 3000, 60):
            self.assertEqual(tracker.evaluate(observe(relay(94)), POLICY, now, self.TOTAL), [])
        self.assertEqual(tracker.episodes, {})

    def test_total_loss_opens_top_level_and_recovers_below_total(self):
        tracker = LossTracker()
        tracker.evaluate(observe(relay(100)), POLICY, 0, self.TOTAL)
        opened = tracker.evaluate(observe(relay(100)), POLICY, 300, self.TOTAL)
        self.assertEqual([(e.kind, e.level) for e in opened], [(EventKind.NEW, TOTAL_LEVEL)])
        # Привычные 60% на этом адресе — уже «снизились», а не вечный эпизод
        self.assertEqual(tracker.evaluate(observe(relay(60)), POLICY, 400, self.TOTAL), [])
        recovered = tracker.evaluate(observe(relay(60)), POLICY, 1300, self.TOTAL)
        self.assertEqual(kinds(recovered), [(EventKind.RECOVERED, TARGET)])

    def test_same_loss_on_other_targets_uses_normal_policy(self):
        tracker = LossTracker()
        other = relay(60, ip="45.145.56.96")
        tracker.evaluate(observe(relay(60), other), POLICY, 0, self.TOTAL)
        events = tracker.evaluate(observe(relay(60), other), POLICY, 300, self.TOTAL)
        self.assertEqual([(e.target, e.level) for e in events], [("45.145.56.96:8443", 2)])

    def test_total_only_targets_follow_rules(self):
        observations = observe(relay(60), relay(60, ip="45.145.56.96"))
        rules = TargetRules(endpoints={("62.50.146.225", 8443): TargetMode.TOTAL_ONLY})
        self.assertEqual(total_only_targets(observations, rules), frozenset({TARGET}))


class DigestTests(unittest.TestCase):
    def events(self, count: int, relays_per_address: int = 2):
        tracker = LossTracker()
        rows = [
            relay(30 + i, relay_id=j, name=f"relay-{j}", ip=f"62.50.146.{i}")
            for i in range(count) for j in range(relays_per_address)
        ]
        tracker.evaluate(observe(*rows), POLICY, 0)
        return tracker.evaluate(observe(*rows), POLICY, 300)

    def test_grouped_by_address_then_relays(self):
        tracker = LossTracker()
        rows = observe(relay(40), relay(35, relay_id=2, name="Timeweb", rtt=None), relay(0, relay_id=3))
        tracker.evaluate(rows, POLICY, 0)
        events = tracker.evaluate(rows, POLICY, 300)
        [text] = format_digest(events, {"62.50.146.225": "NL red switch <2>"}, POLICY, "ru")
        self.assertIn("<b>Новые потери</b>", text)
        self.assertIn("<b>62.50.146.225:8443</b> · NL red switch &lt;2&gt; — потери", text)
        body = text[text.index("<b>62.50.146.225:8443</b>"):]
        self.assertLess(body.index("VK relay 1 — 40%"), body.index("Timeweb — 35% (нет ответа)"))
        self.assertIn("• ещё 1 без потерь", text)

    def test_single_message_when_it_fits(self):
        self.assertEqual(len(format_digest(self.events(5), {}, POLICY, "ru")), 1)

    def test_split_between_addresses_with_numbering(self):
        chunks = format_digest(self.events(40, relays_per_address=3), {}, POLICY, "ru", limit=1000)
        self.assertGreater(len(chunks), 1)
        for index, chunk in enumerate(chunks, start=1):
            self.assertIn(f"({index}/{len(chunks)})", chunk)
            self.assertLessEqual(len(chunk), 1000 + 120)
            # Каждый адрес целиком в одной части: все три релея рядом с ним
            for line in chunk.splitlines():
                if line.startswith("<b>62.50.146."):
                    address = line.split("</b>")[0]
                    self.assertEqual(sum(address in c for c in chunks), 1)
        joined = "".join(chunks)
        self.assertEqual(joined.count("relay-2 —"), 40)

    def test_huge_address_block_continues_with_same_header(self):
        chunks = format_digest(self.events(1, relays_per_address=80), {}, POLICY, "en", limit=600)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all("<b>62.50.146.0:8443</b>" in chunk for chunk in chunks))

    def test_recovered_is_green_and_short(self):
        tracker = LossTracker()
        tracker.evaluate(observe(relay(40)), POLICY, 0)
        tracker.evaluate(observe(relay(40)), POLICY, 300)
        tracker.evaluate(observe(relay(3), relay(1, relay_id=2)), POLICY, 400)
        events = tracker.evaluate(observe(relay(3), relay(1, relay_id=2)), POLICY, 1300)
        [text] = format_digest(events, {}, POLICY, "ru")
        self.assertTrue(text.startswith("\U0001f7e2"))
        self.assertIn("• 2 релеев, максимум 3%", text)

    def test_history_line_is_plain_text(self):
        events = self.events(1)
        line = history_text(events[0], {"62.50.146.0": "NL"}, "ru")
        self.assertEqual(line, "Новые потери: 62.50.146.0:8443 (NL) — потери (relay-0 30%, relay-1 30%)")
        self.assertNotIn("<", line)


class OverviewTests(unittest.TestCase):
    def test_grouped_by_address_worst_first_with_owner_and_episode(self):
        snapshots = [
            RelaySnapshot(1, "VK", (LossReading("62.50.146.225", 8443, 40.0, 45.0, 60),
                                    LossReading("62.50.146.231", 8443, 0.0, 44.0, 60)), frozenset(), 0),
            RelaySnapshot(2, "Timeweb", (LossReading("62.50.146.225", 8443, 10.0, None, 60),), frozenset(), 0),
        ]
        episodes = {"62.50.146.225:8443": Episode(level=1, opened_at=100.0, last_data_at=100.0, notified_at=100.0)}
        rows = build_overview(snapshots, {"62.50.146.225": "NL 2"}, episodes)
        self.assertEqual([r["target"] for r in rows], ["62.50.146.225:8443", "62.50.146.231:8443"])
        first = rows[0]
        self.assertEqual((first["owner"], first["worst_loss"], first["episode"]), ("NL 2", 40.0, {"level": 1, "opened_at": 100.0}))
        self.assertEqual([r["name"] for r in first["relays"]], ["VK", "Timeweb"])
        self.assertIsNone(rows[1]["episode"])


class ParseTargetTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_target(" 62.50.146.225 "), ("62.50.146.225", 443))
        self.assertEqual(parse_target("62.50.146.225:8449"), ("62.50.146.225", 8449))
        self.assertEqual(parse_target("[2001:db8::1]:8443"), ("2001:db8::1", 8443))
        self.assertEqual(parse_target("2001:db8::1"), ("2001:db8::1", 443))

    def test_endpoint_keeps_missing_port(self):
        self.assertEqual(parse_endpoint("62.50.146.225"), ("62.50.146.225", None))
        self.assertEqual(parse_endpoint("[2001:db8::1]"), ("2001:db8::1", None))
        for text in ("62.50.146.225", "62.50.146.225:8449", "[2001:db8::1]:8443", "2001:db8::1"):
            with self.subTest(text=text):
                self.assertEqual(format_endpoint(*parse_endpoint(text)), text)

    def test_rejects(self):
        for bad in ("", "example.com", "1.2.3.4:0", "1.2.3.4:70000", "1.2.3.4:x", "0.0.0.0", "224.0.0.1:443"):
            with self.subTest(target=bad), self.assertRaises(TargetParseError):
                parse_target(bad)


class CheckFanOutTests(unittest.IsolatedAsyncioTestCase):
    """Ответы нод раскладываются по статусам, успешные — сверху по потерям."""

    async def test_statuses(self):
        import httpx
        from app.services import loss_overview

        def server(server_id, caps=None):
            return SimpleNamespace(id=server_id, name=f"n{server_id}", url=f"https://n{server_id}",
                                   node_capabilities=caps)

        responses = {
            "https://n1": httpx.Response(200, json={"loss_pct": 10.0, "rtt_ms": 40.0, "samples": 20}),
            "https://n2": httpx.Response(200, json={"loss_pct": 45.0, "rtt_ms": None, "samples": 20}),
            "https://n3": httpx.Response(404),
        }

        class FakeClient:
            async def post(self, url, **_):
                base = url.split("/api/")[0]
                if base == "https://n4":
                    raise httpx.ConnectError("down")
                return responses[base]

        restricted = '{"system": "ro"}'
        with mock.patch.object(loss_overview, "get_node_client", return_value=FakeClient()),              mock.patch.object(loss_overview, "node_auth_headers", return_value={}):
            results = await check_from_servers(
                [server(1), server(2), server(3), server(4), server(5, restricted)], "62.50.146.225", 443,
            )
        self.assertEqual(
            [(r["name"], r["status"]) for r in results],
            [("n2", CheckStatus.OK), ("n1", CheckStatus.OK), ("n3", CheckStatus.UNSUPPORTED),
             ("n4", CheckStatus.UNREACHABLE), ("n5", CheckStatus.DENIED)],
        )

    async def test_check_asks_for_the_client_probe_and_a_silent_server_tops_the_list(self):
        import httpx
        from app.services import loss_overview

        asked: list[dict] = []
        answers = {"https://n1": True, "https://n2": False}

        class FakeClient:
            async def post(self, url, json, **_):
                asked.append(json)
                base = url.split("/api/")[0]
                return httpx.Response(200, json={"loss_pct": 0.0, "rtt_ms": 40.0, "samples": 20,
                                                 "answers": answers[base]})

        servers = [SimpleNamespace(id=i, name=f"n{i}", url=f"https://n{i}", node_capabilities=None) for i in (1, 2)]
        with mock.patch.object(loss_overview, "get_node_client", return_value=FakeClient()), \
                mock.patch.object(loss_overview, "node_auth_headers", return_value={}):
            results = await check_from_servers(servers, "62.50.146.225", 443, client_probe=True)
            await check_from_servers(servers[:1], "62.50.146.225", 443)
        self.assertEqual([entry.get("client_probe") for entry in asked], [True, True, None])
        # Подключения доходят с обоих, но второй сервер клиенту не отвечает
        self.assertEqual([(r["name"], r["answers"]) for r in results], [("n2", False), ("n1", True)])

    async def test_check_takes_only_a_plain_yes_or_no_for_the_client_probe_verdict(self):
        import httpx
        from app.services import loss_overview

        class FakeClient:
            async def post(self, url, **_):
                return httpx.Response(200, json={"loss_pct": 3.0, "rtt_ms": 40.0, "samples": 20, "answers": "no"})

        servers = [SimpleNamespace(id=1, name="n1", url="https://n1", node_capabilities=None)]
        with mock.patch.object(loss_overview, "get_node_client", return_value=FakeClient()), \
                mock.patch.object(loss_overview, "node_auth_headers", return_value={}):
            [result] = await check_from_servers(servers, "62.50.146.225", 443, client_probe=True)
        self.assertIsNone(result["answers"])
        self.assertEqual(result["loss_pct"], 3.0)


class AlerterWiringTests(unittest.IsolatedAsyncioTestCase):
    """Проверка по всему парку: реестр -> эпизоды -> одно сообщение на проверку."""

    def setUp(self):
        now = [0.0]
        self.clock = now
        self.registry = LossRegistry(clock=lambda: now[0])
        patcher = mock.patch.object(server_alerter, "get_loss_registry", return_value=self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.alerter = server_alerter.ServerAlerter()
        self.alerter._trigger_excluded = {"packet_loss": set()}
        self.alerter._save_loss_episodes = mock.AsyncMock()
        self.alerter._send_loss_digest = mock.AsyncMock()
        self.settings = SimpleNamespace(
            packet_loss_enabled=True, packet_loss_threshold=20.0, packet_loss_sustained_seconds=300,
            packet_loss_calm_seconds=900, packet_loss_reminder_hours=0, offline_enabled=True,
            packet_loss_scope="all", packet_loss_wide_share=50,
        )

    def feed(self, now: float, *relays: tuple[int, str, float]):
        self.clock[0] = now
        for relay_id, name, loss in relays:
            self.registry.update(relay_id, name, {"loss_probe": [
                {"ip": "62.50.146.225", "port": 8443, "loss_pct": loss, "rtt_ms": 45.0, "samples": 60},
            ]})

    async def test_one_digest_for_all_relays(self):
        for now in (0, 300):
            self.feed(now, (1, "VK", 40.0), (2, "Timeweb", 35.0))
            await self.alerter._check_packet_loss(self.settings, set(), now)
        self.alerter._send_loss_digest.assert_awaited_once()
        events = self.alerter._send_loss_digest.await_args.args[1]
        self.assertEqual([(e.kind, len(e.relays)) for e in events], [(EventKind.NEW, 2)])

    async def test_excluded_relays_do_not_count(self):
        self.alerter._trigger_excluded = {"packet_loss": {1}}
        for now in (0, 300):
            self.feed(now, (1, "VK", 40.0), (2, "Timeweb", 0.0))
            await self.alerter._check_packet_loss(self.settings, set(), now)
        self.alerter._send_loss_digest.assert_not_awaited()

    async def test_total_only_target_waits_for_total_loss(self):
        self.registry.set_excluded_targets([ExcludedTarget("62.50.146.225", None, total_only=True)])
        for now in (0, 300):
            self.feed(now, (1, "VK", 80.0))
            await self.alerter._check_packet_loss(self.settings, set(), now)
        self.alerter._send_loss_digest.assert_not_awaited()
        for now in (360, 660):
            self.feed(now, (1, "VK", 100.0))
            await self.alerter._check_packet_loss(self.settings, set(), now)
        events = self.alerter._send_loss_digest.await_args.args[1]
        self.assertEqual([(e.kind, e.level) for e in events], [(EventKind.NEW, TOTAL_LEVEL)])

    async def test_message_level_and_share_come_from_settings(self):
        self.settings.packet_loss_scope, self.settings.packet_loss_wide_share = "wide", 30
        policy = self.alerter._loss_policy(self.settings)
        self.assertEqual((policy.scope, policy.wide_share_pct), (LossScope.WIDE, 30))

    async def test_settings_row_from_before_the_update_means_all_events(self):
        self.settings.packet_loss_scope, self.settings.packet_loss_wide_share = None, None
        policy = self.alerter._loss_policy(self.settings)
        self.assertEqual((policy.scope, policy.wide_share_pct), (LossScope.ALL, WIDE_SHARE_PCT))

    async def test_single_relay_loss_reaches_the_digest_marked_for_history_only(self):
        self.settings.packet_loss_scope = "wide"
        for now in (0, 300):
            self.feed(now, (1, "VK", 40.0), (2, "Timeweb", 0.0), (3, "Selectel", 0.0))
            await self.alerter._check_packet_loss(self.settings, set(), now)
        events = self.alerter._send_loss_digest.await_args.args[1]
        self.assertEqual(told(events), [(EventKind.NEW, True)])

    async def test_disabling_drops_open_episodes(self):
        for now in (0, 300):
            self.feed(now, (1, "VK", 40.0))
            await self.alerter._check_packet_loss(self.settings, set(), now)
        self.settings.packet_loss_enabled = False
        await self.alerter._check_packet_loss(self.settings, set(), 360)
        self.assertEqual(self.alerter.loss_episodes(), {})


class SettingsDb:
    """alert_settings из одной строки."""

    def __init__(self):
        self.settings = AlertSettings()
        self.refresh = mock.AsyncMock()
        self.commit = mock.AsyncMock()

    async def execute(self, statement) -> SimpleNamespace:
        return SimpleNamespace(scalar_one_or_none=lambda: self.settings)


class MessageLevelSettingsTests(unittest.IsolatedAsyncioTestCase):
    """Уровень сообщений алерта «Потери с релеев» в настройках алертов."""

    async def test_row_from_before_the_update_reads_as_all_events(self):
        response = await alerts.get_alert_settings(SettingsDb())
        self.assertEqual(
            (response["packet_loss_scope"], response["packet_loss_wide_share"]), ("all", WIDE_SHARE_PCT),
        )

    async def test_level_and_share_are_saved_as_plain_values(self):
        db = SettingsDb()
        response = await alerts.update_alert_settings(
            alerts.AlertSettingsUpdate(packet_loss_scope="wide", packet_loss_wide_share=30), db,
        )
        self.assertEqual((response["packet_loss_scope"], response["packet_loss_wide_share"]), ("wide", 30))
        # В колонку уходит строка, а не член перечисления
        self.assertIs(type(db.settings.packet_loss_scope), str)

    async def test_saving_other_settings_keeps_the_level(self):
        db = SettingsDb()
        await alerts.update_alert_settings(alerts.AlertSettingsUpdate(packet_loss_scope="dead"), db)
        response = await alerts.update_alert_settings(alerts.AlertSettingsUpdate(packet_loss_threshold=30), db)
        self.assertEqual(response["packet_loss_scope"], "dead")

    def test_unknown_level_and_share_out_of_range_are_rejected(self):
        for bad in ({"packet_loss_scope": "loud"}, {"packet_loss_wide_share": 5}, {"packet_loss_wide_share": 101}):
            with self.assertRaises(ValidationError, msg=str(bad)):
                alerts.AlertSettingsUpdate(**bad)


EXIT = 9
EXIT_IP = "62.50.146.225"
OTHER_IP = "45.145.56.96"


def server(server_id: int, *, silent_for_seconds: float = 5, url: str | None = None) -> SimpleNamespace:
    last_seen = datetime.now(timezone.utc) - timedelta(seconds=silent_for_seconds)
    return SimpleNamespace(
        id=server_id, name=f"node-{server_id}", url=url or f"https://node-{server_id}.example:9100",
        last_seen=last_seen, proxy_url=None,
    )


def probe(ip: str, loss: float) -> dict:
    return {"loss_probe": [{"ip": ip, "port": 8443, "loss_pct": loss, "rtt_ms": None, "samples": 60}]}


class AlerterOutageTests(unittest.IsolatedAsyncioTestCase):
    """Про упавший сервер уже сказал алерт о недоступности: потери до его адресов
    и по его собственным замерам — та же новость второй раз."""

    def setUp(self):
        now = [0.0]
        self.clock = now
        self.registry = LossRegistry(clock=lambda: now[0])
        self.latest = LatestMetrics()
        for patcher in (
            mock.patch.object(server_alerter, "get_loss_registry", return_value=self.registry),
            mock.patch.object(server_alerter, "get_latest_metrics", return_value=self.latest),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.alerter = server_alerter.ServerAlerter()
        self.alerter._trigger_excluded = {"packet_loss": set(), "offline": set()}
        self.alerter._save_loss_episodes = mock.AsyncMock()
        self.alerter._send_loss_digest = mock.AsyncMock()
        self.settings = SimpleNamespace(
            packet_loss_enabled=True, packet_loss_threshold=20.0, packet_loss_sustained_seconds=60,
            packet_loss_calm_seconds=900, packet_loss_reminder_hours=0,
            packet_loss_scope="all", packet_loss_wide_share=50,
            offline_enabled=True, check_interval=60, offline_fail_threshold=3,
        )
        self.exit = server(EXIT, silent_for_seconds=3600)
        self.latest.put(EXIT, interfaces(EXIT_IP))

    def exit_is_down(self, down: bool = True) -> None:
        state = self.alerter._states.setdefault(EXIT, server_alerter.ServerAlertState())
        state.was_offline = down
        if not down:
            self.exit.last_seen = datetime.now(timezone.utc)

    async def check(self, now: float, loss_to_exit: float, was_down: frozenset[int] = frozenset()) -> None:
        """Релей прислал замер до адреса сервера EXIT, затем проверка потерь."""
        self.clock[0] = now
        self.registry.update(1, "relay-1", probe(EXIT_IP, loss_to_exit))
        await self.alerter._check_packet_loss(self.settings, set(), now, [self.exit], was_down)

    def reported(self) -> list[tuple[EventKind, str]]:
        return [kind for call in self.alerter._send_loss_digest.await_args_list for kind in kinds(call.args[1])]

    async def test_loss_to_a_down_server_is_not_reported(self):
        self.exit_is_down()
        for now in range(0, 900, 60):
            await self.check(now, 100.0, was_down=frozenset({EXIT}))
        self.assertEqual(self.reported(), [])

    async def test_stale_down_flag_does_not_hold_once_the_server_is_out_of_the_offline_check(self):
        # Сервер лежал, и проверку доступности для него выключили: пометку «лежит» больше некому снять
        self.exit_is_down()
        self.alerter._trigger_excluded["offline"] = {EXIT}
        await self.check(0, 100.0)
        await self.check(60, 100.0)
        self.assertEqual(self.reported(), [(EventKind.NEW, TARGET)])

    async def test_stale_down_flag_does_not_hold_once_offline_alerts_are_disabled(self):
        self.exit_is_down()
        self.settings.offline_enabled = False
        await self.check(0, 100.0)
        await self.check(60, 100.0)
        self.assertEqual(self.reported(), [(EventKind.NEW, TARGET)])

    async def test_address_from_the_server_url_is_enough(self):
        # После перезапуска панели последнего ответа упавшей ноды в памяти может не быть
        self.latest.forget(EXIT)
        self.exit.url = f"https://{EXIT_IP}:9100"
        self.exit_is_down()
        for now in (0, 60, 120):
            await self.check(now, 100.0, was_down=frozenset({EXIT}))
        self.assertEqual(self.reported(), [])

    async def test_returned_server_is_quiet_while_probe_windows_remember_the_outage(self):
        self.exit_is_down()
        await self.check(0, 100.0, was_down=frozenset({EXIT}))
        self.exit_is_down(False)
        await self.check(60, 100.0, was_down=frozenset({EXIT}))
        await self.check(120, 50.0)
        for now in range(180, 60 + OUTAGE_SETTLE_SEC + 300, 60):
            await self.check(now, 0.0)
        self.assertEqual(self.reported(), [])

    async def test_loss_that_stays_after_the_return_is_reported(self):
        # Сервер поднялся, а его порт — нет
        self.exit_is_down()
        await self.check(0, 100.0, was_down=frozenset({EXIT}))
        self.exit_is_down(False)
        await self.check(60, 100.0, was_down=frozenset({EXIT}))
        for now in range(120, 60 + OUTAGE_SETTLE_SEC, 60):
            await self.check(now, 100.0)
        self.assertEqual(self.reported(), [])
        await self.check(60 + OUTAGE_SETTLE_SEC, 100.0)
        self.assertEqual(self.reported(), [(EventKind.NEW, TARGET)])

    async def test_measurements_of_a_returned_server_do_not_count(self):
        # Нода пережила обрыв сети: её собственное окно полно потерь до всех её адресов
        self.exit_is_down(False)

        async def check(now: float, was_down: frozenset[int] = frozenset()) -> None:
            self.clock[0] = now
            self.registry.update(EXIT, "exit", probe(OTHER_IP, 100.0))
            self.registry.update(1, "relay-1", probe(OTHER_IP, 0.0))
            await self.alerter._check_packet_loss(self.settings, set(), now, [self.exit], was_down)

        await check(0, frozenset({EXIT}))
        for now in range(60, OUTAGE_SETTLE_SEC, 60):
            await check(now)
        self.assertEqual(self.reported(), [])
        # Простой ушёл из окна, а потери остались — теперь это замер, а не эхо простоя
        await check(OUTAGE_SETTLE_SEC)
        await check(OUTAGE_SETTLE_SEC + 60)
        self.assertEqual(self.reported(), [(EventKind.NEW, f"{OTHER_IP}:8443")])

    async def test_address_that_moved_to_a_live_server_is_reported(self):
        self.exit_is_down()
        for now in (0, 60):
            self.clock[0] = now
            self.registry.update(5, "standby", interfaces(EXIT_IP))
            await self.check(now, 100.0, was_down=frozenset({EXIT}))
        self.assertEqual(self.reported(), [(EventKind.NEW, TARGET)])

    async def exit_went_silent(self) -> None:
        """Нода ответила панели в последний раз в нулевую секунду; порог неответов ещё не пройден."""
        self.exit = server(EXIT, silent_for_seconds=VERDICT_WAIT_FROM_SEC + 15)
        self.registry.update(EXIT, "exit", interfaces(EXIT_IP))
        await self.check(0, 100.0)

    async def test_silent_server_waits_for_the_offline_verdict(self):
        await self.exit_went_silent()
        await self.check(60, 100.0)
        await self.check(120, 100.0)
        self.assertEqual(self.reported(), [])
        # Проба ответила: сервер жив, а адрес не отвечает — это уже новость
        self.exit.last_seen = datetime.now(timezone.utc) - timedelta(hours=1)
        await self.check(180, 100.0)
        self.assertEqual(self.reported(), [(EventKind.NEW, TARGET)])

    async def test_slow_offline_check_does_not_hold_loss_for_long(self):
        # Десять неответов до вердикта: столько алерт о потерях ждать не должен
        self.settings.offline_fail_threshold = 10
        await self.exit_went_silent()
        for now in range(60, VERDICT_WAIT_UNTIL_SEC + 1, 60):
            await self.check(now, 100.0)
        self.assertEqual(self.reported(), [])
        await self.check(VERDICT_WAIT_UNTIL_SEC + 60, 100.0)
        self.assertEqual(self.reported(), [(EventKind.NEW, TARGET)])

    async def test_silent_server_is_not_held_when_its_offline_check_is_off(self):
        self.alerter._trigger_excluded["offline"] = {EXIT}
        await self.exit_went_silent()
        await self.check(60, 100.0)
        self.assertEqual(self.reported(), [(EventKind.NEW, TARGET)])

    async def test_silent_server_is_not_held_when_offline_alerts_are_disabled(self):
        self.settings.offline_enabled = False
        await self.exit_went_silent()
        await self.check(60, 100.0)
        self.assertEqual(self.reported(), [(EventKind.NEW, TARGET)])


class FakeTelegram:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def send_message(self, bot_token: str, chat_id: str, text: str, **_delivery) -> bool:
        self.texts.append(text)
        return True


class FakeSession:
    """Подмена async_session: на запрос отдаёт серверы, записи принимает молча."""

    def __init__(self, servers: list) -> None:
        self.servers = servers

    def __call__(self) -> "FakeSession":
        return self

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def execute(self, statement) -> SimpleNamespace:
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.servers)), all=lambda: [])

    def add(self, row) -> None:
        pass

    def add_all(self, rows) -> None:
        pass

    async def commit(self) -> None:
        pass


class OutageEndToEndTest(unittest.IsolatedAsyncioTestCase):
    """Весь обход алертера: сервер упал и вернулся — два сообщения о нём самом
    и ни одного о потерях до него."""

    def setUp(self):
        now = [1000.0]
        self.clock = now
        self.registry = LossRegistry(clock=lambda: now[0])
        self.telegram = FakeTelegram()
        self.exit = server(EXIT, silent_for_seconds=3600, url=f"https://{EXIT_IP}:9100")
        self.relays = [server(1), server(2)]
        for patcher in (
            mock.patch.object(server_alerter, "get_loss_registry", return_value=self.registry),
            mock.patch.object(server_alerter, "get_latest_metrics", return_value=LatestMetrics()),
            mock.patch.object(server_alerter, "async_session", FakeSession([self.exit, *self.relays])),
            mock.patch.object(server_alerter.time, "time", lambda: now[0]),
            mock.patch("app.services.telegram_bot.get_telegram_bot_service", return_value=self.telegram),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.alerter = server_alerter.ServerAlerter()
        # Пропавшая нода не отвечает ни по API, ни по ICMP
        self.alerter._active_probe_sequence = mock.AsyncMock(return_value=(True, False))
        self.settings = AlertSettings(
            language="en", telegram_bot_token="123456:token", telegram_chat_id="-1001",
            check_interval=60, alert_cooldown=1800,
            offline_enabled=True, offline_fail_threshold=3, offline_recovery_notify=True,
            packet_loss_enabled=True, packet_loss_threshold=20.0,
            packet_loss_sustained_seconds=300, packet_loss_calm_seconds=900,
        )

    async def tick(self, now: float, loss_to_exit: float) -> None:
        self.clock[0] = now
        for relay_node in self.relays:
            relay_node.last_seen = datetime.now(timezone.utc)
            self.registry.update(relay_node.id, relay_node.name, probe(EXIT_IP, loss_to_exit))
        await self.alerter._check_all(self.settings)

    def exit_answered(self, now: float) -> None:
        self.clock[0] = now
        self.exit.last_seen = datetime.now(timezone.utc)
        self.registry.update(EXIT, self.exit.name, interfaces(EXIT_IP))

    def loss_messages(self) -> list[str]:
        return [text for text in self.telegram.texts if "Packet loss" in text]

    async def test_short_sustain_does_not_let_loss_overtake_the_offline_alert(self):
        self.settings.packet_loss_sustained_seconds = 60
        self.exit_answered(1000)
        await self.tick(1000, 100.0)
        # Нода молчит сто секунд: порог неответов ещё не пройден, а удержание потерь уже да
        self.exit.last_seen = datetime.now(timezone.utc) - timedelta(seconds=100)
        await self.tick(1100, 100.0)
        self.assertEqual(self.telegram.texts, [])
        self.exit.last_seen = datetime.now(timezone.utc) - timedelta(hours=1)
        for now in (1220, 1280, 1340):
            await self.tick(now, 100.0)
        self.assertEqual(len(self.telegram.texts), 1, msg=self.telegram.texts)
        self.assertIn(f"Server {self.exit.name} is offline", self.telegram.texts[0])

    async def test_server_that_returned_while_the_panel_restarted_is_quiet_too(self):
        # Пометку «лежит» панель подняла из истории алертов, окна релеев ещё полны простоя
        self.settings.packet_loss_sustained_seconds = 60
        self.alerter._states[EXIT] = server_alerter.ServerAlertState()
        self.alerter._states[EXIT].was_offline = True
        residual_loss = {1000: 100.0, 1060: 100.0, 1120: 50.0}
        for now in range(1000, 2400, 60):
            self.exit_answered(now)
            await self.tick(now, residual_loss.get(now, 0.0))
        self.assertEqual(len(self.telegram.texts), 1, msg=self.telegram.texts)
        self.assertEqual(self.loss_messages(), [])

    async def test_outage_is_two_messages_about_the_server_and_none_about_loss(self):
        for now in range(1000, 2200, 60):
            await self.tick(now, 100.0)
        self.assertEqual(len(self.telegram.texts), 1)
        self.assertIn(f"Server {self.exit.name} is offline", self.telegram.texts[0])

        self.exit.last_seen = datetime.now(timezone.utc)
        await self.tick(2200, 100.0)
        await self.tick(2260, 50.0)
        for now in range(2320, 4000, 60):
            await self.tick(now, 0.0)
        self.assertEqual(len(self.telegram.texts), 2, msg=self.telegram.texts)
        self.assertFalse([text for text in self.telegram.texts if "Packet loss" in text])


if __name__ == "__main__":
    unittest.main()
