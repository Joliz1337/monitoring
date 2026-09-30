"""Tests for the relay loss probe (services/loss_probe).

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Ошибка здесь тихая: неверный разбор адреса — пустая колонка «Потери», неверный
счёт окна — зелёный адрес, до которого теряется половина подключений.
"""

import asyncio
import errno
import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.capabilities import parse_capabilities  # noqa: E402
from app.models.dnat import (  # noqa: E402
    DnatRule,
    DnatRuleCounters,
    DnatStateResponse,
    DnatTargetCounters,
)
from app.models.haproxy import (  # noqa: E402
    HAProxyProxyStats,
    HAProxyStatRow,
    HAProxyStatsResponse,
)
from app.services.loss_probe import (  # noqa: E402
    MAX_TARGETS,
    AttemptWindow,
    LossProbe,
    Source,
    attempt,
    dnat_targets,
    haproxy_targets,
    parse_addr,
    readable_sources,
)


def server_row(name: str, addr: str) -> HAProxyStatRow:
    return HAProxyStatRow(name=name, kind="server", status="UP", addr=addr)


def stats_with(*addrs: str) -> HAProxyStatsResponse:
    return HAProxyStatsResponse(
        available=True,
        proxies=[HAProxyProxyStats(
            name="backend_tcp_relay",
            backend=HAProxyStatRow(name="BACKEND", kind="backend", status="UP"),
            servers=[server_row(f"srv{i}", addr) for i, addr in enumerate(addrs)],
        )],
    )


def dnat_rule(**overrides) -> DnatRule:
    fields = {"name": "relay", "listen_port": 443, "target_ip": "10.0.0.5"}
    fields.update(overrides)
    return DnatRule(**fields)


class FakeAttempts:
    """Подменяет сеть: ответы по адресу берутся из очереди, пустая — потеря."""

    def __init__(self, answers: dict[tuple[str, int], list]):
        self.answers = {target: list(values) for target, values in answers.items()}
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, ip: str, port: int):
        self.calls.append((ip, port))
        queue = self.answers.get((ip, port), [])
        return queue.pop(0) if queue else None


class ParseAddrTests(unittest.TestCase):
    def test_ipv4(self):
        self.assertEqual(parse_addr("10.0.0.5:443"), ("10.0.0.5", 443))

    def test_ipv6_in_brackets(self):
        self.assertEqual(parse_addr("[2001:db8::1]:443"), ("2001:db8::1", 443))

    def test_rejects_what_cannot_be_probed(self):
        for bad in ("", "backend.local:443", "10.0.0.5", "10.0.0.5:0", "0.0.0.0:443", "10.0.0.5:70000"):
            with self.subTest(addr=bad):
                self.assertIsNone(parse_addr(bad))


class TargetDiscoveryTests(unittest.TestCase):
    def test_haproxy_servers_deduplicated(self):
        # Копии сервера под каждый исходящий IP (srv, srv_o2) бьют в один адрес
        stats = stats_with("10.0.0.5:443", "10.0.0.5:443", "10.0.0.6:8449")
        self.assertEqual(haproxy_targets(stats), {("10.0.0.5", 443), ("10.0.0.6", 8449)})

    def test_stopped_haproxy_has_no_targets(self):
        self.assertEqual(haproxy_targets(HAProxyStatsResponse(available=False)), set())

    def test_dnat_target_port_zero_means_listen_port(self):
        rules = [dnat_rule(target_port=0), dnat_rule(name="other", listen_port=8443, target_port=9443)]
        self.assertEqual(dnat_targets(rules), {("10.0.0.5", 443), ("10.0.0.5", 9443)})

    def test_dnat_probes_every_target_of_balanced_rule(self):
        rules = [dnat_rule(target_ip="10.0.0.5,10.0.0.6", distribution="round_robin")]
        self.assertEqual(dnat_targets(rules), {("10.0.0.5", 443), ("10.0.0.6", 443)})

    def test_dnat_skips_udp_only_and_disabled_rules(self):
        rules = [
            dnat_rule(name="udp", protocol="udp"),
            dnat_rule(name="both", protocol="both", target_ip="10.0.0.7"),
            dnat_rule(name="off", enabled=False, target_ip="10.0.0.8"),
        ]
        self.assertEqual(dnat_targets(rules), {("10.0.0.7", 443)})


class AttemptWindowTests(unittest.TestCase):
    def test_empty_window_has_no_stats(self):
        self.assertIsNone(AttemptWindow().stats())

    def test_loss_and_mean_rtt_of_answered(self):
        window = AttemptWindow()
        for rtt in (10.0, None, 30.0, None):
            window.record(rtt)
        stats = window.stats()
        self.assertEqual((stats.loss_pct, stats.rtt_ms, stats.samples), (50.0, 20.0, 4))

    def test_all_lost_has_no_rtt(self):
        window = AttemptWindow()
        window.record(None)
        self.assertEqual((window.stats().loss_pct, window.stats().rtt_ms), (100.0, None))

    def test_window_keeps_only_last_attempts(self):
        window = AttemptWindow(size=3)
        for rtt in (None, None, None, 5.0, 5.0, 5.0):
            window.record(rtt)
        self.assertEqual((window.stats().loss_pct, window.stats().samples), (0.0, 3))


class AttemptTests(unittest.TestCase):
    """Настоящие сокеты на loopback: семантика «ответил / потерян»."""

    def test_listening_port_answers(self):
        async def scenario():
            server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                return await attempt("127.0.0.1", port)
            finally:
                server.close()
                await server.wait_closed()

        self.assertIsNotNone(asyncio.run(scenario()))

    def test_refused_port_counts_as_answer(self):
        # Отказ (RST) — это ответ: сеть до адреса жива, закрыт только порт.
        # Запас по таймауту — Windows повторяет connect после RST ~2 с
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.assertIsNotNone(asyncio.run(attempt("127.0.0.1", port, timeout=5.0)))

    def test_silence_counts_as_lost(self):
        # Подмена, а не «немой» адрес: TUN-режим VPN-клиента на машине
        # разработчика отвечает за любой IP
        async def hang(*_args, **_kwargs):
            await asyncio.sleep(3600)

        with mock.patch("app.services.loss_probe.asyncio.open_connection", hang):
            self.assertIsNone(asyncio.run(attempt("10.0.0.5", 443, timeout=0.05)))

    def test_network_error_counts_as_lost(self):
        unreachable = mock.AsyncMock(side_effect=OSError(errno.ENETUNREACH, "Network is unreachable"))
        with mock.patch("app.services.loss_probe.asyncio.open_connection", unreachable):
            self.assertIsNone(asyncio.run(attempt("10.0.0.5", 443)))


class LossProbeTests(unittest.TestCase):
    def probe(self, targets: dict, answers: dict) -> tuple[LossProbe, FakeAttempts]:
        fake = FakeAttempts(answers)
        probe = LossProbe(discover=lambda: targets, attempt_fn=fake)
        probe.refresh_targets(targets)
        return probe, fake

    def test_round_makes_one_attempt_per_target(self):
        targets = {("10.0.0.5", 443): {Source.HAPROXY}, ("10.0.0.6", 443): {Source.DNAT}}
        probe, fake = self.probe(targets, {("10.0.0.5", 443): [12.0]})
        asyncio.run(probe.run_round())
        self.assertEqual(sorted(fake.calls), sorted(targets))
        self.assertEqual(probe.stats_for(("10.0.0.5", 443)).loss_pct, 0.0)
        self.assertEqual(probe.stats_for(("10.0.0.6", 443)).loss_pct, 100.0)

    def test_refresh_drops_vanished_targets_and_keeps_history(self):
        kept, gone = ("10.0.0.5", 443), ("10.0.0.6", 443)
        probe, _ = self.probe({kept: {Source.HAPROXY}, gone: {Source.HAPROXY}}, {kept: [5.0]})
        asyncio.run(probe.run_round())
        probe.refresh_targets({kept: {Source.HAPROXY}})
        self.assertEqual(probe.stats_for(kept).samples, 1)
        self.assertIsNone(probe.stats_for(gone))

    def test_target_count_is_capped(self):
        targets = {(f"10.0.{i // 250}.{i % 250 + 1}", 443): {Source.HAPROXY} for i in range(MAX_TARGETS + 5)}
        probe, fake = self.probe(targets, {})
        asyncio.run(probe.run_round())
        self.assertEqual(len(fake.calls), MAX_TARGETS)

    def test_annotate_haproxy_marks_servers_and_leaves_input_intact(self):
        probe, _ = self.probe({("10.0.0.5", 443): {Source.HAPROXY}}, {("10.0.0.5", 443): [8.0]})
        asyncio.run(probe.run_round())
        stats = stats_with("10.0.0.5:443", "10.0.0.9:443")
        annotated = probe.annotate_haproxy(stats)
        servers = annotated.proxies[0].servers
        self.assertEqual(servers[0].probe.rtt_ms, 8.0)
        self.assertIsNone(servers[1].probe)
        self.assertIsNone(annotated.proxies[0].backend.probe)
        # Ответ show stat кэшируется в менеджере — его нельзя портить
        self.assertIsNone(stats.proxies[0].servers[0].probe)

    def test_annotate_dnat_uses_rule_target_port(self):
        probe, _ = self.probe({("10.0.0.5", 9443): {Source.DNAT}}, {("10.0.0.5", 9443): [4.0]})
        asyncio.run(probe.run_round())
        state = DnatStateResponse(
            available=True, ip_forward=True, rules_hash="h", healthy=True,
            rules=[dnat_rule(target_port=9443), dnat_rule(name="udp", protocol="udp", target_ip="10.0.0.5")],
            counters=[
                DnatRuleCounters(name="relay", present=True, targets=[DnatTargetCounters(ip="10.0.0.5", present=True)]),
                DnatRuleCounters(name="udp", present=True, targets=[DnatTargetCounters(ip="10.0.0.5", present=True)]),
            ],
        )
        annotated = probe.annotate_dnat(state)
        self.assertEqual(annotated.counters[0].targets[0].probe.rtt_ms, 4.0)
        self.assertIsNone(annotated.counters[1].targets[0].probe)

    def test_snapshot_hides_sources_the_panel_cannot_read(self):
        target = ("10.0.0.5", 443)
        probe, _ = self.probe({target: {Source.HAPROXY}}, {target: [6.0]})
        asyncio.run(probe.run_round())
        self.assertEqual(probe.snapshot({Source.DNAT}), [])
        self.assertEqual(
            probe.snapshot({Source.HAPROXY}),
            [{"ip": "10.0.0.5", "port": 443, "loss_pct": 0.0, "rtt_ms": 6.0, "samples": 1}],
        )


class ReadableSourcesTests(unittest.TestCase):
    def test_unrestricted_node_shows_everything(self):
        self.assertEqual(readable_sources(parse_capabilities("")), set(Source))

    def test_follows_node_capabilities(self):
        self.assertEqual(readable_sources(parse_capabilities("haproxy:ro traffic")), {Source.HAPROXY})
        self.assertEqual(readable_sources(parse_capabilities("monitoring")), set())


if __name__ == "__main__":
    unittest.main()
