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
from app.models.loss_probe import CHECK_ATTEMPTS_MAX, LossCheckRequest  # noqa: E402
from app.services.haproxy_down_counter import DownCounter  # noqa: E402
from app.services.haproxy_traffic_watch import TrafficWatch  # noqa: E402
from app.services.client_probe import ProxyHeader  # noqa: E402
from app.services.loss_probe import (  # noqa: E402
    CLIENT_PROBE_INTERVAL_SEC,
    CLIENT_PROBE_LEASE_SEC,
    CLIENT_PROBE_VALID_SEC,
    APPLY_SETTLE_SEC,
    MAX_TARGETS,
    TARGETS_REFRESH_SEC,
    UNKNOWN_GRACE_SEC,
    AttemptWindow,
    Discovery,
    LossProbe,
    Source,
    attempt,
    check_target,
    discover_targets,
    dnat_targets,
    haproxy_proxy_headers,
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
    def probe(self, targets: dict, answers: dict, **kwargs) -> tuple[LossProbe, FakeAttempts]:
        fake = FakeAttempts(answers)
        probe = LossProbe(discover=lambda: Discovery(targets), attempt_fn=fake, **kwargs)
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

    def test_annotate_haproxy_marks_servers_only(self):
        probe, _ = self.probe({("10.0.0.5", 443): {Source.HAPROXY}}, {("10.0.0.5", 443): [8.0]})
        asyncio.run(probe.run_round())
        stats = stats_with("10.0.0.5:443", "10.0.0.9:443")
        annotated = probe.annotate_haproxy(stats)
        servers = annotated.proxies[0].servers
        self.assertEqual(servers[0].probe.rtt_ms, 8.0)
        self.assertIsNone(servers[1].probe)
        self.assertIsNone(annotated.proxies[0].backend.probe)

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
            [{"ip": "10.0.0.5", "port": 443, "loss_pct": 0.0, "rtt_ms": 6.0, "samples": 1,
              "answers": None, "downs": 0, "stalled": None}],
        )

    def test_snapshot_reports_downs_for_haproxy_targets_only(self):
        backend, forwarded = ("10.0.0.5", 443), ("10.0.0.6", 443)
        probe, _ = self.probe(
            {backend: {Source.HAPROXY}, forwarded: {Source.DNAT}}, {backend: [6.0], forwarded: [6.0]},
            downs_fn={backend: 4, forwarded: 9}.get,
        )
        asyncio.run(probe.run_round())
        downs = {entry["ip"]: entry["downs"] for entry in probe.snapshot(set(Source))}
        # None — адреса нет среди серверов HAProxy: панель такой не отключает
        self.assertEqual(downs, {"10.0.0.5": 4, "10.0.0.6": None})

    def test_snapshot_hides_downs_when_haproxy_is_closed_to_panel(self):
        shared = ("10.0.0.5", 443)
        probe, _ = self.probe({shared: {Source.HAPROXY, Source.DNAT}}, {shared: [6.0]}, downs_fn=lambda target: 4)
        asyncio.run(probe.run_round())
        self.assertIsNone(probe.snapshot({Source.DNAT})[0]["downs"])

    def test_snapshot_reports_stalled_for_haproxy_targets_only(self):
        backend, forwarded = ("10.0.0.5", 443), ("10.0.0.6", 443)
        probe, _ = self.probe(
            {backend: {Source.HAPROXY}, forwarded: {Source.DNAT}}, {backend: [6.0], forwarded: [6.0]},
            stalled_fn={backend: True, forwarded: True}.get,
        )
        asyncio.run(probe.run_round())
        stalled = {entry["ip"]: entry["stalled"] for entry in probe.snapshot(set(Source))}
        self.assertEqual(stalled, {"10.0.0.5": True, "10.0.0.6": None})

    def test_snapshot_hides_stalled_when_haproxy_is_closed_to_panel(self):
        shared = ("10.0.0.5", 443)
        probe, _ = self.probe({shared: {Source.HAPROXY, Source.DNAT}}, {shared: [6.0]}, stalled_fn=lambda target: True)
        asyncio.run(probe.run_round())
        self.assertIsNone(probe.snapshot({Source.DNAT})[0]["stalled"])

    def test_unreadable_haproxy_keeps_its_targets_and_history(self):
        backend, forwarded = ("10.0.0.5", 443), ("10.0.0.6", 443)
        probe, _ = self.probe({backend: {Source.HAPROXY}, forwarded: {Source.DNAT}}, {backend: [5.0]})
        asyncio.run(probe.run_round())
        # show stat не ответил (reload): серверы никуда не делись, а DNAT прочитан и пуст
        probe.refresh_targets({}, unknown=frozenset({Source.HAPROXY}), now=100.0)
        self.assertEqual(probe.stats_for(backend).samples, 1)
        self.assertIsNone(probe.stats_for(forwarded))

    def test_unreadable_source_keeps_only_its_own_share_of_a_target(self):
        shared = ("10.0.0.5", 443)
        probe, _ = self.probe({shared: {Source.HAPROXY, Source.DNAT}}, {shared: [5.0]})
        asyncio.run(probe.run_round())
        probe.refresh_targets({}, unknown=frozenset({Source.HAPROXY}), now=100.0)
        self.assertEqual(probe.snapshot({Source.DNAT}), [])
        self.assertEqual(len(probe.snapshot({Source.HAPROXY})), 1)

    def test_source_unreadable_for_long_loses_its_targets(self):
        backend = ("10.0.0.5", 443)
        probe, _ = self.probe({backend: {Source.HAPROXY}}, {backend: [5.0]})
        asyncio.run(probe.run_round())
        unknown = frozenset({Source.HAPROXY})
        probe.refresh_targets({}, unknown=unknown, now=100.0)
        probe.refresh_targets({}, unknown=unknown, now=100.0 + UNKNOWN_GRACE_SEC - 1)
        self.assertIsNotNone(probe.stats_for(backend))
        # HAProxy остановлен насовсем — проверять его адреса дальше незачем
        probe.refresh_targets({}, unknown=unknown, now=100.0 + UNKNOWN_GRACE_SEC)
        self.assertIsNone(probe.stats_for(backend))

    def test_grace_restarts_after_a_successful_read(self):
        backend = ("10.0.0.5", 443)
        targets = {backend: {Source.HAPROXY}}
        probe, _ = self.probe(targets, {backend: [5.0]})
        asyncio.run(probe.run_round())
        unknown = frozenset({Source.HAPROXY})
        probe.refresh_targets({}, unknown=unknown, now=100.0)
        probe.refresh_targets(targets, now=110.0)
        probe.refresh_targets({}, unknown=unknown, now=100.0 + UNKNOWN_GRACE_SEC)
        self.assertIsNotNone(probe.stats_for(backend))


# Ровно то, что отдаёт HAProxyManager.get_stats(): словарь, а не модель —
# на нём и упали первые ноды с новой проверкой
STATS_DICT = {
    "available": True,
    "proxies": [{
        "name": "backend_tcp_relay",
        "backend": {"name": "BACKEND", "kind": "backend", "status": "UP"},
        "servers": [{"name": "srv1", "kind": "server", "status": "UP", "addr": "10.0.0.5:443"}],
    }],
}


class RealDataPathTests(unittest.TestCase):
    def test_discovery_reads_manager_dict_and_dnat_state(self):
        haproxy = mock.Mock(get_stats=mock.Mock(return_value=STATS_DICT), get_config=mock.Mock(return_value=""))
        dnat = mock.Mock(load_state=mock.Mock(return_value=([dnat_rule(target_ip="10.0.0.6")], None)))
        with mock.patch("app.services.haproxy_manager.get_haproxy_manager", return_value=haproxy),              mock.patch("app.services.dnat_manager.get_dnat_manager", return_value=dnat):
            found = discover_targets()
        self.assertEqual(found.targets, {("10.0.0.5", 443): {Source.HAPROXY}, ("10.0.0.6", 443): {Source.DNAT}})
        self.assertEqual(found.unknown, frozenset())

    def discover_with(self, counter: DownCounter, stats: dict, config: str = "") -> Discovery:
        haproxy = mock.Mock(get_stats=mock.Mock(return_value=stats), get_config=mock.Mock(return_value=config))
        dnat = mock.Mock(load_state=mock.Mock(return_value=([], None)))
        with mock.patch("app.services.haproxy_manager.get_haproxy_manager", return_value=haproxy),              mock.patch("app.services.dnat_manager.get_dnat_manager", return_value=dnat),              mock.patch("app.services.loss_probe.get_down_counter", return_value=counter):
            return discover_targets()

    @staticmethod
    def stats_with_chkdown(chkdown: int) -> dict:
        server = {**STATS_DICT["proxies"][0]["servers"][0], "chkdown": chkdown}
        return {"available": True, "proxies": [{**STATS_DICT["proxies"][0], "servers": [server]}]}

    def test_discovery_feeds_the_down_counter(self):
        counter = DownCounter()
        self.discover_with(counter, self.stats_with_chkdown(1))
        self.discover_with(counter, self.stats_with_chkdown(3))
        self.assertEqual(counter.downs(("10.0.0.5", 443)), 2)

    def test_failed_stats_read_is_unknown_not_empty(self):
        counter = DownCounter()
        self.discover_with(counter, self.stats_with_chkdown(1))
        failed = self.discover_with(counter, {"available": False, "reason": "timeout", "proxies": []})
        self.assertEqual(failed.unknown, frozenset({Source.HAPROXY}))
        self.assertEqual(failed.targets, {})
        # Неудачное чтение не стирает точку отсчёта: падения за это время не теряются
        self.discover_with(counter, self.stats_with_chkdown(3))
        self.assertEqual(counter.downs(("10.0.0.5", 443)), 2)

    def silent_server_seen_with(self, config: str) -> TrafficWatch:
        """Шестьдесят новых сессий прислали 300 КБ, сервер не ответил ни байтом."""
        watch = TrafficWatch()
        for stot, bytes_in in ((100, 4_000_000), (160, 4_300_000)):
            server = {**STATS_DICT["proxies"][0]["servers"][0], "stot": stot, "bin": bytes_in, "bout": 90_000_000}
            stats = {"available": True, "proxies": [{**STATS_DICT["proxies"][0], "servers": [server]}]}
            with mock.patch("app.services.loss_probe.get_traffic_watch", return_value=watch):
                self.discover_with(DownCounter(), stats, config)
        return watch

    def test_discovery_feeds_the_traffic_watch(self):
        watch = self.silent_server_seen_with("defaults\n    mode tcp\n    option contstats\n")
        self.assertIs(watch.stalled(("10.0.0.5", 443)), True)

    def test_traffic_is_not_judged_while_haproxy_counts_bytes_at_session_end(self):
        watch = self.silent_server_seen_with("defaults\n    mode tcp\n")
        self.assertIsNone(watch.stalled(("10.0.0.5", 443)))

    def test_probe_singleton_reports_the_traffic_watch_verdict(self):
        from app.services import loss_probe as loss_probe_module

        target = ("10.0.0.5", 443)
        watch = mock.Mock(stalled=mock.Mock(return_value=True))
        with mock.patch.object(loss_probe_module, "_probe", None), \
                mock.patch.object(loss_probe_module, "get_traffic_watch", return_value=watch):
            probe = loss_probe_module.get_loss_probe()
        probe.refresh_targets({target: {Source.HAPROXY}})
        probe._windows[target].record(5.0)
        self.assertIs(probe.snapshot({Source.HAPROXY})[0]["stalled"], True)

    def test_stats_route_annotates_manager_dict(self):
        from app.routers import haproxy as haproxy_router

        probe, _ = LossProbeTests().probe({("10.0.0.5", 443): {Source.HAPROXY}}, {("10.0.0.5", 443): [7.0]})
        asyncio.run(probe.run_round())
        manager = mock.Mock(get_stats=mock.Mock(return_value=STATS_DICT))
        quarantine = mock.Mock(quarantined_rows=mock.Mock(return_value=frozenset()))
        with mock.patch.object(haproxy_router, "get_haproxy_manager", return_value=manager),              mock.patch.object(haproxy_router, "get_loss_probe", return_value=probe),              mock.patch.object(haproxy_router, "get_quarantine", return_value=quarantine):
            response = asyncio.run(haproxy_router.get_haproxy_stats())
        self.assertEqual(response.proxies[0].servers[0].probe.rtt_ms, 7.0)
        # Кэш менеджера — тот же словарь, его разметка не трогает
        self.assertNotIn("probe", STATS_DICT["proxies"][0]["servers"][0])


class ManualCheckTests(unittest.TestCase):
    """Проверка адреса по запросу панели: разовая серия попыток с этой ноды."""

    def test_series_counts_loss_and_rtt(self):
        answers = iter([10.0, None, 30.0, None])

        async def fake(ip, port):
            return next(answers)

        stats = asyncio.run(check_target("10.0.0.5", 443, attempts=4, attempt_fn=fake, spacing=0))
        self.assertEqual((stats.loss_pct, stats.rtt_ms, stats.samples), (50.0, 20.0, 4))

    def test_request_defaults_and_limits(self):
        request = LossCheckRequest(ip="62.50.146.225")
        self.assertEqual((request.port, request.attempts), (443, 20))
        for bad in ({"ip": "example.com"}, {"ip": "0.0.0.0"}, {"ip": "224.0.0.1"},
                    {"ip": "10.0.0.5", "port": 0}, {"ip": "10.0.0.5", "attempts": CHECK_ATTEMPTS_MAX + 1}):
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                LossCheckRequest(**bad)

    def test_needs_full_system_access_on_restricted_node(self):
        # Нода по запросу подключается к любому адресу — на ограниченной ноде
        # это право полного доступа к «Системе», а не чтения
        path = "/api/loss-probe/check"
        self.assertIsNone(parse_capabilities("").check("POST", path))
        self.assertIsNone(parse_capabilities("system").check("POST", path))
        self.assertIsNotNone(parse_capabilities("system:ro haproxy").check("POST", path))


class RefreshScheduleTests(unittest.TestCase):
    """Адреса перечитываются раз в TARGETS_REFRESH_SEC, а после смены конфига — сразу по паузе."""

    def setUp(self):
        self.discovered = []

        def discover():
            self.discovered.append(1)
            return Discovery({("10.0.0.5", 443): {Source.HAPROXY}})

        self.probe = LossProbe(discover=discover, attempt_fn=FakeAttempts({}))

    def test_regular_cadence(self):
        asyncio.run(self.probe.tick(100.0))
        asyncio.run(self.probe.tick(100.0 + TARGETS_REFRESH_SEC - 1))
        self.assertEqual(len(self.discovered), 1)
        asyncio.run(self.probe.tick(100.0 + TARGETS_REFRESH_SEC))
        self.assertEqual(len(self.discovered), 2)

    def test_config_change_brings_refresh_forward(self):
        import time
        now = time.monotonic()
        asyncio.run(self.probe.tick(now))
        self.probe.request_refresh()
        asyncio.run(self.probe.tick(now + APPLY_SETTLE_SEC - 0.5))
        self.assertEqual(len(self.discovered), 1)
        asyncio.run(self.probe.tick(now + APPLY_SETTLE_SEC + 0.5))
        self.assertEqual(len(self.discovered), 2)


class ReadableSourcesTests(unittest.TestCase):
    def test_unrestricted_node_shows_everything(self):
        self.assertEqual(readable_sources(parse_capabilities("")), set(Source))

    def test_follows_node_capabilities(self):
        self.assertEqual(readable_sources(parse_capabilities("haproxy:ro traffic")), {Source.HAPROXY})
        self.assertEqual(readable_sources(parse_capabilities("monitoring")), set())


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeClientProbe:
    """Подменяет проверку «как клиент»: вердикт по адресу; None — подключиться не удалось."""

    def __init__(self, verdicts: dict[tuple[str, int], object]):
        self.verdicts = verdicts
        self.calls: list[tuple[tuple[str, int], object]] = []

    async def __call__(self, ip: str, port: int, proxy=None):
        self.calls.append(((ip, port), proxy))
        return self.verdicts.get((ip, port))


class ProxyHeaderDiscoveryTests(unittest.TestCase):
    CONFIG = (
        "defaults\n    mode tcp\n\n"
        "backend backend_tcp_lb\n"
        "    server srv1 10.0.0.5:443 check send-proxy check-send-proxy\n"
        "    server srv2 10.0.0.6:443 check send-proxy-v2\n"
        "    server srv3 10.0.0.7:443 check\n"
        "    # server srv4 10.0.0.8:443 check send-proxy\n"
        "backend backend_tcp_other\n"
        "    server srv1 10.0.0.5:443 check send-proxy-v2\n"
        "    server srv9 backend.example:443 check send-proxy\n"
    )

    def test_header_kind_is_read_from_the_server_line(self):
        self.assertEqual(haproxy_proxy_headers(self.CONFIG), {
            # Один адрес в двух правилах: сервер с v2 заголовок v1 не поймёт
            ("10.0.0.5", 443): ProxyHeader.V2,
            ("10.0.0.6", 443): ProxyHeader.V2,
        })

    def test_check_option_alone_is_not_a_header_for_clients(self):
        config = "backend b\n    server srv1 10.0.0.5:443 check check-send-proxy\n"
        self.assertEqual(haproxy_proxy_headers(config), {})

    def test_plain_header(self):
        config = "backend b\n    server srv1 10.0.0.5:443 send-proxy check\n"
        self.assertEqual(haproxy_proxy_headers(config), {("10.0.0.5", 443): ProxyHeader.V1})


class ClientProbeRoundTests(unittest.IsolatedAsyncioTestCase):
    """Проверка «как клиент»: идёт только по просьбе панели, реже замера подключений."""

    BACKEND = ("10.0.0.5", 443)
    FORWARDED = ("10.0.0.6", 443)

    def make(self, verdicts: dict, proxies: dict | None = None) -> LossProbe:
        self.clock = FakeClock()
        self.client = FakeClientProbe(verdicts)
        targets = {self.BACKEND: {Source.HAPROXY}, self.FORWARDED: {Source.DNAT}}
        self.targets = targets
        probe = LossProbe(
            discover=lambda: Discovery(self.targets), attempt_fn=FakeAttempts({}),
            client_probe_fn=self.client, proxy_headers_fn=lambda: proxies or {}, clock=self.clock,
        )
        probe.refresh_targets(targets)
        return probe

    async def round(self, probe: LossProbe, advance: float = 0.0, ask: bool = True) -> None:
        self.clock.now += advance
        if ask:
            probe.request_client_probe()
        await probe.tick(self.clock.now)
        if probe._client_task is not None:
            await probe._client_task

    def answers(self, probe: LossProbe) -> dict:
        return {entry["ip"]: entry["answers"] for entry in probe.snapshot(set(Source))}

    async def test_nothing_is_probed_until_the_panel_asks(self):
        probe = self.make({self.BACKEND: True})
        await self.round(probe, ask=False)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.answers(probe), {"10.0.0.5": None, "10.0.0.6": None})

    async def test_asked_probe_tells_who_answers(self):
        probe = self.make({self.BACKEND: True, self.FORWARDED: False})
        await self.round(probe)
        self.assertEqual(self.answers(probe), {"10.0.0.5": True, "10.0.0.6": False})

    async def test_badge_of_the_address_carries_the_verdict(self):
        probe = self.make({self.BACKEND: False})
        await self.round(probe)
        self.assertIs(probe.stats_for(self.BACKEND).answers, False)

    async def test_rounds_are_half_a_minute_apart(self):
        probe = self.make({self.BACKEND: True})
        await self.round(probe)
        await self.round(probe, advance=CLIENT_PROBE_INTERVAL_SEC - 1)
        self.assertEqual(len(self.client.calls), 2)
        await self.round(probe, advance=1)
        self.assertEqual(len(self.client.calls), 4)

    async def test_probing_stops_when_the_panel_stops_asking(self):
        probe = self.make({self.BACKEND: False})
        await self.round(probe)
        await self.round(probe, advance=CLIENT_PROBE_LEASE_SEC, ask=False)
        self.assertEqual(len(self.client.calls), 2)
        # Старый вердикт без проверки — не вердикт
        self.assertEqual(self.answers(probe), {"10.0.0.5": None, "10.0.0.6": None})

    async def test_failed_connection_keeps_the_last_verdict_for_a_while(self):
        probe = self.make({self.BACKEND: False})
        await self.round(probe)
        self.client.verdicts = {}
        await self.round(probe, advance=CLIENT_PROBE_INTERVAL_SEC)
        await self.round(probe, advance=CLIENT_PROBE_VALID_SEC - CLIENT_PROBE_INTERVAL_SEC)
        self.assertIs(self.answers(probe)["10.0.0.5"], False)
        await self.round(probe, advance=1)
        self.assertIsNone(self.answers(probe)["10.0.0.5"])

    async def test_rule_with_proxy_protocol_is_probed_with_its_header(self):
        probe = self.make({}, proxies={self.BACKEND: ProxyHeader.V2})
        await self.round(probe)
        self.assertEqual(sorted(self.client.calls), [(self.BACKEND, ProxyHeader.V2), (self.FORWARDED, None)])

    async def test_verdict_of_a_removed_address_is_forgotten(self):
        probe = self.make({self.BACKEND: False, self.FORWARDED: False})
        await self.round(probe)
        self.targets = {self.BACKEND: {Source.HAPROXY}}
        probe.refresh_targets(self.targets)
        await self.round(probe, advance=CLIENT_PROBE_INTERVAL_SEC)
        self.targets = {self.BACKEND: {Source.HAPROXY}, self.FORWARDED: {Source.DNAT}}
        probe.refresh_targets(self.targets)
        await probe.run_round()
        self.assertEqual(self.answers(probe), {"10.0.0.5": False, "10.0.0.6": None})

    async def test_failing_round_does_not_stop_the_loss_probe(self):
        async def broken(ip: str, port: int, proxy=None):
            raise RuntimeError("probe bug")

        probe = self.make({})
        probe._client_probe = broken
        with self.assertLogs("app.services.loss_probe", level="ERROR"):
            await self.round(probe)
        self.assertEqual(self.answers(probe), {"10.0.0.5": None, "10.0.0.6": None})


if __name__ == "__main__":
    unittest.main()
