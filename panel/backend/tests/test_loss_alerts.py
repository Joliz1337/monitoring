"""Tests for the packet-loss alert: registry, episodes and the Telegram digest.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Инварианты: сообщение — только при смене статуса адреса (новые / усилились /
снизились), мигающий адрес не шлёт пар «есть / нет», отсутствие данных не
изменение, сообщение сгруппировано по адресу и делится только между адресами.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import server_alerter  # noqa: E402
from app.services.loss_alerts import (  # noqa: E402
    MIN_SAMPLES,
    Episode,
    NO_DATA_CLOSE_SEC,
    EventKind,
    LossPolicy,
    LossTracker,
    RelayReading,
    collect_observations,
    format_digest,
    history_text,
)
from app.services.loss_overview import (  # noqa: E402
    CheckStatus,
    TargetParseError,
    build_overview,
    check_from_servers,
    parse_target,
)
from app.services.loss_registry import (  # noqa: E402
    LossReading,
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
            packet_loss_calm_seconds=900, packet_loss_reminder_hours=0,
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

    async def test_disabling_drops_open_episodes(self):
        for now in (0, 300):
            self.feed(now, (1, "VK", 40.0))
            await self.alerter._check_packet_loss(self.settings, set(), now)
        self.settings.packet_loss_enabled = False
        await self.alerter._check_packet_loss(self.settings, set(), 360)
        self.assertEqual(self.alerter.loss_episodes(), {})


if __name__ == "__main__":
    unittest.main()
