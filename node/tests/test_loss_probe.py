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

from fastapi import HTTPException  # noqa: E402

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
from app.models.loss_probe import CHECK_ATTEMPTS_MAX, LossCheckRequest, ProbeStats  # noqa: E402
from app.services.haproxy_down_counter import DownCounter  # noqa: E402
from app.services.haproxy_traffic_watch import TrafficWatch  # noqa: E402
from app.routers import loss_probe as loss_probe_router  # noqa: E402
from app.services import loss_probe as loss_probe_module  # noqa: E402
from app.services.client_probe import ProxyHeader  # noqa: E402
from app.services.loss_probe import (  # noqa: E402
    CLIENT_PROBE_INTERVAL_SEC,
    CLIENT_PROBE_LEASE_SEC,
    CLIENT_PROBE_VALID_SEC,
    APPLY_SETTLE_SEC,
    MAX_SPARE_WAYS,
    MAX_TARGETS,
    TARGETS_REFRESH_SEC,
    UNKNOWN_GRACE_SEC,
    AttemptWindow,
    Discovery,
    LossProbe,
    Source,
    attempt,
    check_target,
    discover_all,
    discover_targets,
    dnat_targets,
    haproxy_proxy_headers,
    haproxy_targets,
    parse_addr,
    readable_sources,
    target_exits,
    target_proxy_headers,
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
        # С какого адреса релея ушла попытка; None — с адреса по умолчанию
        self.routes: list[tuple[tuple[str, int], object]] = []

    async def __call__(self, ip: str, port: int, local_ip=None):
        self.calls.append((ip, port))
        self.routes.append(((ip, port), local_ip))
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

    def test_attempt_leaves_from_the_given_address(self):
        # Выходной адрес из правила HAProxy: путь с него бывает другим, чем с адреса по умолчанию
        async def scenario():
            peers = []

            def accept(_reader, writer):
                peers.append(writer.get_extra_info("peername")[0])
                writer.close()

            server = await asyncio.start_server(accept, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                answered = await attempt("127.0.0.1", port, timeout=5.0, local_ip="127.0.0.2")
                await asyncio.sleep(0.05)
            finally:
                server.close()
                await server.wait_closed()
            return answered, peers

        answered, peers = asyncio.run(scenario())
        self.assertIsNotNone(answered)
        self.assertEqual(peers, ["127.0.0.2"])

    def test_exit_address_missing_on_the_host_counts_as_lost(self):
        # HAProxy с такого адреса тоже не подключится
        async def scenario():
            server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                return await attempt("127.0.0.1", port, timeout=5.0, local_ip="192.0.2.77")
            finally:
                server.close()
                await server.wait_closed()

        self.assertIsNone(asyncio.run(scenario()))


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

    def test_attempts_take_the_exit_addresses_of_the_rule_in_turn(self):
        # Копии сервера под выходные IP делят соединения по кругу — так же ходит и замер
        relayed, plain = ("10.0.0.5", 443), ("10.0.0.6", 443)
        targets = {relayed: {Source.HAPROXY}, plain: {Source.DNAT}}
        probe, fake = self.probe(targets, {})
        probe.refresh_targets(targets, exits={relayed: ("192.0.2.10", "192.0.2.11")})
        for _ in range(3):
            asyncio.run(probe.run_round())
        self.assertEqual([local_ip for target, local_ip in fake.routes if target == relayed],
                         ["192.0.2.10", "192.0.2.11", "192.0.2.10"])
        self.assertEqual({local_ip for target, local_ip in fake.routes if target == plain}, {None})

    def test_failed_stats_read_keeps_the_exit_addresses(self):
        relayed = ("10.0.0.5", 443)
        targets = {relayed: {Source.HAPROXY}}
        probe, fake = self.probe(targets, {})
        probe.refresh_targets(targets, exits={relayed: ("192.0.2.10",)})
        probe.refresh_targets(targets)
        asyncio.run(probe.run_round())
        self.assertEqual(fake.routes, [(relayed, "192.0.2.10")])

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

    def test_metrics_route_passes_the_panel_request_for_the_client_probe(self):
        from app.routers import metrics as metrics_router

        collector = mock.Mock(get_all_metrics=mock.AsyncMock(return_value={}))
        with mock.patch.object(metrics_router, "get_collector", return_value=collector):
            asyncio.run(metrics_router.get_all_metrics(window=None, client_probe=False))
            collector.request_client_probe.assert_not_called()
            asyncio.run(metrics_router.get_all_metrics(window=None, client_probe=True))
        collector.request_client_probe.assert_called_once_with()

    def test_probe_is_asked_only_for_what_the_panel_may_read(self):
        # Панель с правами «только мониторинг» бэкендов не видит — и рукопожатий к ним не заказывает
        from app.services.metrics_collector import MetricsCollector

        collector = MetricsCollector.__new__(MetricsCollector)
        collector._loss_probe = mock.Mock()
        collector._loss_probe_sources = {Source.DNAT}
        collector.request_client_probe()
        collector._loss_probe.request_client_probe.assert_called_once_with({Source.DNAT})

    def test_discovery_finds_the_header_of_a_server_named_by_a_domain(self):
        config = "backend backend_tcp_relay\n    server srv1 backend.example:443 resolvers mydns check send-proxy\n"
        found = self.discover_with(DownCounter(), STATS_DICT, config)
        self.assertEqual(found.proxy_headers, {("10.0.0.5", 443): ProxyHeader.V1})

    def test_failed_stats_read_leaves_the_headers_unknown(self):
        failed = self.discover_with(DownCounter(), {"available": False, "reason": "timeout", "proxies": []})
        self.assertIsNone(failed.proxy_headers)

    def test_discovery_finds_the_exit_address_of_a_server(self):
        config = "backend backend_tcp_relay\n    server srv1 10.0.0.5:443 check source 192.0.2.10\n"
        found = self.discover_with(DownCounter(), STATS_DICT, config)
        self.assertEqual(found.exits, {("10.0.0.5", 443): ("192.0.2.10",)})

    def test_failed_stats_read_leaves_the_exit_addresses_unknown(self):
        failed = self.discover_with(DownCounter(), {"available": False, "reason": "timeout", "proxies": []})
        self.assertIsNone(failed.exits)

    def test_manual_check_goes_from_the_exit_addresses_of_the_rule(self):
        from app.routers import loss_probe as loss_probe_router

        asked = {}

        async def series(ip: str, port: int, attempts: int, **kwargs) -> ProbeStats:
            asked.update(kwargs)
            return ProbeStats(loss_pct=0.0, rtt_ms=5.0, samples=attempts)

        probe = mock.Mock(exits_for=mock.Mock(return_value=("192.0.2.10",)))
        with mock.patch.object(loss_probe_router, "check_target", series), \
                mock.patch.object(loss_probe_router, "get_loss_probe", return_value=probe):
            asyncio.run(loss_probe_router.check(LossCheckRequest(ip="10.0.0.5", port=443)))
        probe.exits_for.assert_called_once_with(("10.0.0.5", 443))
        self.assertEqual(asked, {"exits": ("192.0.2.10",)})

    def test_trace_goes_from_the_first_exit_address_of_the_rule(self):
        from app.models.loss_probe import TargetRequest
        from app.routers import loss_probe as loss_probe_router

        manager = mock.Mock(start=mock.Mock(return_value=mock.Mock(id="trace-1")))
        probe = mock.Mock(exits_for=mock.Mock(return_value=("192.0.2.10", "192.0.2.11")))
        with mock.patch.object(loss_probe_router, "get_path_trace_manager", return_value=manager), \
                mock.patch.object(loss_probe_router, "get_loss_probe", return_value=probe):
            started = asyncio.run(loss_probe_router.start_trace(TargetRequest(ip="10.0.0.5", port=443)))
        self.assertEqual(started, {"id": "trace-1"})
        manager.start.assert_called_once_with("10.0.0.5", 443, local_ip="192.0.2.10")

    def test_manual_check_adds_the_client_probe_when_the_panel_asks(self):
        from app.routers import loss_probe as loss_probe_router

        async def series(ip: str, port: int, attempts: int, **_kwargs) -> ProbeStats:
            return ProbeStats(loss_pct=0.0, rtt_ms=5.0, samples=attempts)

        probe = mock.Mock(probe_as_client=mock.AsyncMock(return_value=False))
        with mock.patch.object(loss_probe_router, "check_target", series), \
                mock.patch.object(loss_probe_router, "get_loss_probe", return_value=probe):
            plain = asyncio.run(loss_probe_router.check(LossCheckRequest(ip="10.0.0.5", port=443)))
            asked = asyncio.run(loss_probe_router.check(LossCheckRequest(ip="10.0.0.5", port=443, client_probe=True)))
        self.assertEqual((plain.answers, asked.answers), (None, False))
        probe.probe_as_client.assert_awaited_once_with(("10.0.0.5", 443))

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

        async def fake(ip, port, local_ip=None):
            return next(answers)

        stats = asyncio.run(check_target("10.0.0.5", 443, attempts=4, attempt_fn=fake, spacing=0))
        self.assertEqual((stats.loss_pct, stats.rtt_ms, stats.samples), (50.0, 20.0, 4))

    def test_series_goes_through_the_exit_addresses_in_turn(self):
        left_from = []

        async def fake(ip, port, local_ip=None):
            left_from.append(local_ip)
            return 5.0

        asyncio.run(check_target("10.0.0.5", 443, attempts=4, attempt_fn=fake, spacing=0,
                                 exits=("192.0.2.10", None)))
        self.assertEqual(sorted(left_from, key=str), ["192.0.2.10", "192.0.2.10", None, None])

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
    """Подменяет проверку «как клиент»: вердикт по адресу либо по адресу и выходному
    адресу релея (ip, port, local_ip); None — подключиться не удалось."""

    def __init__(self, verdicts: dict[tuple, object]):
        self.verdicts = verdicts
        self.calls: list[tuple[tuple[str, int], object]] = []
        self.routes: list[tuple[tuple[str, int], object]] = []

    async def __call__(self, ip: str, port: int, proxy=None, local_ip=None):
        self.calls.append(((ip, port), proxy))
        self.routes.append(((ip, port), local_ip))
        return self.verdicts.get((ip, port, local_ip), self.verdicts.get((ip, port)))


class ProxyHeaderDiscoveryTests(unittest.TestCase):
    """Заголовок читается по имени сервера в правиле: адрес в конфиге бывает и доменом."""

    CONFIG = (
        "defaults\n    mode tcp\n\n"
        "backend backend_tcp_lb\n"
        "    server srv1 10.0.0.5:443 check send-proxy check-send-proxy\n"
        "    server srv2 10.0.0.6:443 check send-proxy-v2\n"
        "    server srv3 10.0.0.7:443 check\n"
        "    # server srv4 10.0.0.8:443 check send-proxy\n"
        "    server srv5 backend.example:443 resolvers mydns check send-proxy  # домен\n"
        "backend backend_tcp_other\n"
        "    server srv1 10.0.0.5:443 check send-proxy-v2\n"
        "    server srv2 10.0.0.6:443 check send-proxy\n"
    )

    @staticmethod
    def headers(config: str, *servers: tuple[str, str, str]) -> dict:
        rows = [((backend, name), parse_addr(addr), 0) for backend, name, addr in servers]
        return target_proxy_headers(rows, haproxy_proxy_headers(config))

    def test_header_kind_is_read_from_the_server_line(self):
        found = self.headers(
            self.CONFIG,
            ("backend_tcp_lb", "srv1", "10.0.0.5:443"), ("backend_tcp_lb", "srv2", "10.0.0.6:443"),
            ("backend_tcp_lb", "srv3", "10.0.0.7:443"), ("backend_tcp_lb", "srv4", "10.0.0.8:443"),
            ("backend_tcp_other", "srv1", "10.0.0.5:443"), ("backend_tcp_other", "srv2", "10.0.0.6:443"),
        )
        # Один адрес в двух правилах: сервер, ждущий v2, заголовок v1 не поймёт
        self.assertEqual(found, {("10.0.0.5", 443): ProxyHeader.V2, ("10.0.0.6", 443): ProxyHeader.V2})

    def test_server_named_by_a_domain_is_found_by_its_row(self):
        # В show stat у такого сервера уже IP, а в конфиге — домен
        found = self.headers(self.CONFIG, ("backend_tcp_lb", "srv5", "203.0.113.9:443"))
        self.assertEqual(found, {("203.0.113.9", 443): ProxyHeader.V1})

    def test_check_option_alone_is_not_a_header_for_clients(self):
        config = "backend b\n    server srv1 10.0.0.5:443 check check-send-proxy\n"
        self.assertEqual(self.headers(config, ("b", "srv1", "10.0.0.5:443")), {})

    def test_default_server_gives_the_header_to_the_servers_after_it(self):
        config = ("backend b\n    server before 10.0.0.4:443 check\n    default-server send-proxy-v2\n"
                  "    server after 10.0.0.5:443 check\n    server plain 10.0.0.6:443 check no-send-proxy-v2\n")
        found = self.headers(config, ("b", "before", "10.0.0.4:443"), ("b", "after", "10.0.0.5:443"),
                             ("b", "plain", "10.0.0.6:443"))
        self.assertEqual(found, {("10.0.0.5", 443): ProxyHeader.V2})

    def test_default_server_of_the_defaults_section_reaches_every_rule(self):
        config = ("defaults\n    default-server send-proxy\n\nbackend a\n    server s 10.0.0.5:443\n"
                  "listen b\n    bind :443\n    server s 10.0.0.6:443\n")
        self.assertEqual(self.headers(config, ("a", "s", "10.0.0.5:443"), ("b", "s", "10.0.0.6:443")),
                         {("10.0.0.5", 443): ProxyHeader.V1, ("10.0.0.6", 443): ProxyHeader.V1})

    def test_default_server_of_one_rule_does_not_leak_into_the_next(self):
        config = ("backend a\n    default-server send-proxy\n    server s 10.0.0.5:443\n"
                  "backend b\n    server s 10.0.0.6:443\n")
        self.assertEqual(self.headers(config, ("a", "s", "10.0.0.5:443"), ("b", "s", "10.0.0.6:443")),
                         {("10.0.0.5", 443): ProxyHeader.V1})

    def test_servers_of_a_template_take_its_header(self):
        config = "backend b\n    server-template node 3 nodes.example:443 check send-proxy-v2\n"
        found = self.headers(config, ("b", "node2", "10.0.0.5:443"), ("b", "nodeX", "10.0.0.6:443"))
        self.assertEqual(found, {("10.0.0.5", 443): ProxyHeader.V2})

    def test_same_server_name_in_another_rule_is_another_server(self):
        config = "backend a\n    server s 10.0.0.5:443 send-proxy\nbackend b\n    server s 10.0.0.6:443\n"
        self.assertEqual(self.headers(config, ("a", "s", "10.0.0.5:443"), ("b", "s", "10.0.0.6:443")),
                         {("10.0.0.5", 443): ProxyHeader.V1})


class ExitAddressDiscoveryTests(unittest.TestCase):
    """Проверки идут с того же адреса, что и трафик: `source` из правила HAProxy."""

    @staticmethod
    def exits(config: str, *servers: tuple[str, str, str]) -> dict:
        rows = [((backend, name), parse_addr(addr), 0) for backend, name, addr in servers]
        return target_exits(rows, haproxy_proxy_headers(config))

    def test_copies_of_a_server_give_every_exit_address(self):
        config = ("backend b\n    server srv1 10.0.0.5:443 check source 192.0.2.10\n"
                  "    server srv1_o2 10.0.0.5:443 check source 192.0.2.11\n"
                  "    server srv2 10.0.0.6:443 check\n")
        found = self.exits(config, ("b", "srv1", "10.0.0.5:443"), ("b", "srv1_o2", "10.0.0.5:443"),
                           ("b", "srv2", "10.0.0.6:443"))
        # Адрес, к которому ходят только с основного, в списке не нужен
        self.assertEqual(found, {("10.0.0.5", 443): ("192.0.2.10", "192.0.2.11")})

    def test_source_of_the_rule_reaches_all_its_servers(self):
        # `source` секции действует на все её серверы, где бы ни стояла; свой у сервера старше
        config = ("backend b\n    server before 10.0.0.5:443\n    source 192.0.2.10:0\n"
                  "    server own 10.0.0.6:443 source 192.0.2.11\n"
                  "backend other\n    balance source\n    server s 10.0.0.7:443\n")
        found = self.exits(config, ("b", "before", "10.0.0.5:443"), ("b", "own", "10.0.0.6:443"),
                           ("other", "s", "10.0.0.7:443"))
        self.assertEqual(found, {("10.0.0.5", 443): ("192.0.2.10",), ("10.0.0.6", 443): ("192.0.2.11",)})

    def test_source_of_the_defaults_section_reaches_every_rule_after_it(self):
        config = ("backend early\n    server s 10.0.0.4:443\n\ndefaults\n    source 192.0.2.9\n\n"
                  "backend a\n    server s 10.0.0.5:443\nlisten b\n    bind :443\n    source 192.0.2.10\n"
                  "    server s 10.0.0.6:443\n")
        found = self.exits(config, ("early", "s", "10.0.0.4:443"), ("a", "s", "10.0.0.5:443"),
                           ("b", "s", "10.0.0.6:443"))
        self.assertEqual(found, {("10.0.0.5", 443): ("192.0.2.9",), ("10.0.0.6", 443): ("192.0.2.10",)})

    def test_address_reached_with_and_without_a_source_is_probed_both_ways(self):
        config = "backend a\n    server s 10.0.0.5:443 source 192.0.2.10\nbackend b\n    server s 10.0.0.5:443\n"
        found = self.exits(config, ("a", "s", "10.0.0.5:443"), ("b", "s", "10.0.0.5:443"))
        self.assertEqual(found, {("10.0.0.5", 443): ("192.0.2.10", None)})

    def test_source_that_is_not_an_address_means_the_main_one(self):
        config = ("backend a\n    server s 10.0.0.5:443 source 0.0.0.0 usesrc clientip\n"
                  "    server t 10.0.0.6:443 source ${EXIT_IP}\n    server u 10.0.0.7:443 check source\n")
        found = self.exits(config, ("a", "s", "10.0.0.5:443"), ("a", "t", "10.0.0.6:443"), ("a", "u", "10.0.0.7:443"))
        self.assertEqual(found, {})

    def test_default_server_gives_its_source_to_the_servers_after_it(self):
        # Проверено на HAProxy 3.0.11: сервер после такой строки выходит с её адреса
        config = ("backend b\n    server before 10.0.0.4:443\n    default-server check source 192.0.2.10\n"
                  "    server after 10.0.0.5:443\n    server own 10.0.0.6:443 source 192.0.2.11\n"
                  "backend next\n    server s 10.0.0.7:443\n")
        found = self.exits(config, ("b", "before", "10.0.0.4:443"), ("b", "after", "10.0.0.5:443"),
                           ("b", "own", "10.0.0.6:443"), ("next", "s", "10.0.0.7:443"))
        self.assertEqual(found, {("10.0.0.5", 443): ("192.0.2.10",), ("10.0.0.6", 443): ("192.0.2.11",)})

    def test_default_server_of_the_defaults_section_gives_its_source_to_every_rule(self):
        config = ("defaults\n    default-server source 192.0.2.9\n\nbackend a\n    server s 10.0.0.5:443\n"
                  "backend b\n    default-server send-proxy\n    server s 10.0.0.6:443\n")
        found = self.exits(config, ("a", "s", "10.0.0.5:443"), ("b", "s", "10.0.0.6:443"))
        self.assertEqual(found, {("10.0.0.5", 443): ("192.0.2.9",), ("10.0.0.6", 443): ("192.0.2.9",)})

    def test_source_of_default_server_is_older_than_the_source_of_the_rule(self):
        config = "backend b\n    source 192.0.2.1\n    default-server source 192.0.2.2\n    server s 10.0.0.5:443\n"
        self.assertEqual(self.exits(config, ("b", "s", "10.0.0.5:443")), {("10.0.0.5", 443): ("192.0.2.2",)})

    def test_address_with_a_zone_means_the_main_one(self):
        # После % в адресе IPv6 может стоять что угодно, а адрес уходит в строку оболочки трассы
        config = "backend b\n    server s 10.0.0.5:443 source fe80::1%;id;\n    server t 10.0.0.6:443 source fe80::1%eth0\n"
        self.assertEqual(self.exits(config, ("b", "s", "10.0.0.5:443"), ("b", "t", "10.0.0.6:443")), {})

    def test_servers_of_a_template_take_its_source(self):
        config = "backend b\n    server-template node 3 nodes.example:443 check source [2001:db8::10]:0\n"
        found = self.exits(config, ("b", "node2", "10.0.0.5:443"), ("b", "nodeX", "10.0.0.6:443"))
        self.assertEqual(found, {("10.0.0.5", 443): ("2001:db8::10",)})

    def test_header_and_source_of_one_server_are_both_read(self):
        config = "backend b\n    server srv1 10.0.0.5:443 check send-proxy-v2 source 192.0.2.10\n"
        rows = [(("b", "srv1"), ("10.0.0.5", 443), 0)]
        rules = haproxy_proxy_headers(config)
        self.assertEqual(target_proxy_headers(rows, rules), {("10.0.0.5", 443): ProxyHeader.V2})
        self.assertEqual(target_exits(rows, rules), {("10.0.0.5", 443): ("192.0.2.10",)})


class ClientProbeRoundTests(unittest.IsolatedAsyncioTestCase):
    """Проверка «как клиент»: идёт только по просьбе панели, реже замера подключений."""

    BACKEND = ("10.0.0.5", 443)
    FORWARDED = ("10.0.0.6", 443)

    def make(self, verdicts: dict, proxies: dict | None = None, stalled_fn=lambda _target: None,
             exits: dict | None = None) -> LossProbe:
        self.clock = FakeClock()
        self.client = FakeClientProbe(verdicts)
        self.targets = {self.BACKEND: {Source.HAPROXY}, self.FORWARDED: {Source.DNAT}}
        self.proxies = proxies or {}
        self.exits = exits or {}
        probe = LossProbe(
            discover=lambda: Discovery(self.targets, proxy_headers=self.proxies, exits=self.exits),
            attempt_fn=FakeAttempts({}), client_probe_fn=self.client, stalled_fn=stalled_fn, clock=self.clock,
        )
        probe.refresh_targets(self.targets, proxy_headers=self.proxies, exits=self.exits)
        return probe

    async def round(self, probe: LossProbe, advance: float = 0.0, ask: bool = True,
                    sources: frozenset = frozenset(Source)) -> None:
        self.clock.now += advance
        if ask:
            probe.request_client_probe(set(sources))
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
        rounds_within_the_lease = int(CLIENT_PROBE_LEASE_SEC / CLIENT_PROBE_INTERVAL_SEC) - 1
        for _ in range(rounds_within_the_lease):
            await self.round(probe, advance=CLIENT_PROBE_INTERVAL_SEC, ask=False)
        self.assertIs(self.answers(probe)["10.0.0.5"], False)
        calls = len(self.client.calls)
        await self.round(probe, advance=CLIENT_PROBE_INTERVAL_SEC, ask=False)
        self.assertEqual(len(self.client.calls), calls)
        # Вердикту полминуты, но просьба кончилась: без проверки он уже не вердикт
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

    async def test_probe_leaves_from_the_exit_address_of_the_rule(self):
        probe = self.make({}, exits={self.BACKEND: ("192.0.2.10",)})
        await self.round(probe)
        self.assertEqual(sorted(self.client.routes, key=str), [(self.BACKEND, "192.0.2.10"), (self.FORWARDED, None)])

    async def test_server_answering_through_one_exit_address_is_not_silent(self):
        # Выходные адреса проверяются по очереди; мёртв наверняка только молчащий со всех
        dead_way, live_way = (*self.BACKEND, "192.0.2.10"), (*self.BACKEND, "192.0.2.11")
        probe = self.make({dead_way: False, live_way: True}, exits={self.BACKEND: ("192.0.2.10", "192.0.2.11")})
        await self.round(probe)
        # Очередь ещё не дошла до адреса, с которого сервер отвечает: молчание первого — не вердикт
        self.assertIsNone(self.answers(probe)["10.0.0.5"])
        await self.round(probe, advance=CLIENT_PROBE_INTERVAL_SEC)
        self.assertIs(self.answers(probe)["10.0.0.5"], True)
        await self.round(probe, advance=CLIENT_PROBE_INTERVAL_SEC)
        self.assertIs(self.answers(probe)["10.0.0.5"], True)

    async def test_exit_that_cannot_connect_does_not_hide_the_silence_of_the_rest(self):
        # С одного выходного адреса сервер молчит, с другого до него не подключиться: молчит со всех, откуда дошли
        probe = self.make({(*self.BACKEND, "192.0.2.10"): False}, exits={self.BACKEND: ("192.0.2.10", "192.0.2.11")})
        await self.round(probe)
        self.assertIsNone(self.answers(probe)["10.0.0.5"])
        await self.round(probe, advance=CLIENT_PROBE_INTERVAL_SEC)
        self.assertIs(self.answers(probe)["10.0.0.5"], False)

    async def test_new_exit_address_is_tried_before_the_server_is_called_silent(self):
        probe = self.make({(*self.BACKEND, "192.0.2.10"): False, (*self.BACKEND, "192.0.2.11"): True},
                          exits={self.BACKEND: ("192.0.2.10",)})
        await self.round(probe)
        self.assertIs(self.answers(probe)["10.0.0.5"], False)
        self.exits = {self.BACKEND: ("192.0.2.10", "192.0.2.11")}
        probe.refresh_targets(self.targets, exits=self.exits)
        self.assertIsNone(self.answers(probe)["10.0.0.5"])
        await self.round(probe, advance=CLIENT_PROBE_INTERVAL_SEC)
        self.assertIs(self.answers(probe)["10.0.0.5"], True)

    async def test_verdict_of_an_exit_address_lives_until_its_next_turn(self):
        ways = tuple(f"192.0.2.{last}" for last in (10, 11, 12, 13))
        verdicts = {(*self.BACKEND, way): False for way in ways} | {(*self.BACKEND, ways[0]): True}
        probe = self.make(verdicts, exits={self.BACKEND: ways})
        await self.round(probe)
        for _ in range(len(ways) - 1):
            await self.round(probe, advance=CLIENT_PROBE_INTERVAL_SEC)
            self.assertIs(self.answers(probe)["10.0.0.5"], True)

    async def test_server_silent_through_every_exit_address_is_silent(self):
        probe = self.make({self.BACKEND: False}, exits={self.BACKEND: ("192.0.2.10", "192.0.2.11")})
        await self.round(probe)
        await self.round(probe, advance=CLIENT_PROBE_INTERVAL_SEC)
        self.assertIs(self.answers(probe)["10.0.0.5"], False)

    async def test_verdict_of_a_removed_exit_address_is_forgotten(self):
        probe = self.make({(*self.BACKEND, "192.0.2.10"): True, (*self.BACKEND, "192.0.2.11"): False},
                          exits={self.BACKEND: ("192.0.2.10", "192.0.2.11")})
        await self.round(probe)
        self.assertIs(self.answers(probe)["10.0.0.5"], True)
        self.exits = {self.BACKEND: ("192.0.2.11",)}
        probe.refresh_targets(self.targets, exits=self.exits)
        await self.round(probe, advance=CLIENT_PROBE_INTERVAL_SEC)
        self.assertIs(self.answers(probe)["10.0.0.5"], False)

    async def test_manual_probe_tries_every_exit_address(self):
        dead_way, live_way = (*self.BACKEND, "192.0.2.10"), (*self.BACKEND, "192.0.2.11")
        probe = self.make({dead_way: False, live_way: True}, exits={self.BACKEND: ("192.0.2.10", "192.0.2.11")})
        self.assertIs(await probe.probe_as_client(self.BACKEND), True)
        self.assertEqual(sorted(local_ip for _, local_ip in self.client.routes), ["192.0.2.10", "192.0.2.11"])
        self.client.verdicts = {dead_way: False}
        self.assertIs(await probe.probe_as_client(self.BACKEND), False)
        self.client.verdicts = {}
        self.assertIsNone(await probe.probe_as_client(self.BACKEND))

    async def test_one_failing_probe_leaves_the_rest_of_the_round(self):
        async def flaky(ip: str, port: int, proxy=None, local_ip=None):
            if (ip, port) == self.BACKEND:
                raise RuntimeError("probe bug")
            return False

        probe = self.make({})
        probe._client_probe = flaky
        with self.assertLogs("app.services.loss_probe", level="ERROR"):
            await self.round(probe)
        self.assertEqual(self.answers(probe), {"10.0.0.5": None, "10.0.0.6": False})

    async def test_server_that_answers_its_clients_is_not_called_silent(self):
        # Счётчики HAProxy видят ответы клиентам: сервер просто не говорит с незнакомцем
        probe = self.make({self.BACKEND: False, self.FORWARDED: False}, stalled_fn={self.BACKEND: False}.get)
        await self.round(probe)
        self.assertEqual(self.answers(probe), {"10.0.0.5": None, "10.0.0.6": False})

    async def test_addresses_the_panel_may_not_read_are_left_alone(self):
        probe = self.make({})
        await self.round(probe, sources=frozenset({Source.DNAT}))
        self.assertEqual(self.client.calls, [(self.FORWARDED, None)])

    async def test_unread_haproxy_keeps_the_headers_it_gave_before(self):
        probe = self.make({}, proxies={self.BACKEND: ProxyHeader.V1})
        self.proxies = None
        await self.round(probe)
        self.assertIn((self.BACKEND, ProxyHeader.V1), self.client.calls)

    async def test_manual_check_probes_as_a_client_with_the_header_of_the_rule(self):
        probe = self.make({self.BACKEND: True}, proxies={self.BACKEND: ProxyHeader.V2})
        self.assertIs(await probe.probe_as_client(self.BACKEND), True)
        self.assertEqual(self.client.calls, [(self.BACKEND, ProxyHeader.V2)])


class RoutedAttempts:
    """Подменяет сеть: ответ зависит от адреса релея, с которого ушла попытка.
    Адреса без записи отвечают, с записью None — теряют."""

    def __init__(self, rtt_by_local_ip: dict | None = None):
        self.rtt = rtt_by_local_ip or {}
        self.routes: list[tuple[tuple[str, int], object]] = []

    async def __call__(self, ip: str, port: int, local_ip=None):
        self.routes.append(((ip, port), local_ip))
        return self.rtt.get(local_ip, 10.0)


class WaysTests(unittest.IsolatedAsyncioTestCase):
    """Потери и ответ сервера с каждого IP релея: видно, с какого адреса дорога есть, а с какого нет.
    Справочные проверки с неиспользуемых адресов ничего не решают — только показывают."""

    TARGET = ("10.0.0.5", 443)
    USED = "192.0.2.10"
    LOCAL = (USED, "192.0.2.11", "192.0.2.12", "192.0.2.13")
    SPARE = LOCAL[1:]

    def make(self, exits=(USED,), local_ips=LOCAL, rtt=None, verdicts=None, target=TARGET,
             source_ip: str | None = USED, attempts=None, stalled: bool | None = None) -> LossProbe:
        self.clock = FakeClock()
        self.attempts = attempts or RoutedAttempts(rtt)
        self.client = FakeClientProbe(verdicts or {})
        self.targets = {target: {Source.HAPROXY}}
        self.exits = {target: tuple(exits)} if exits else {}
        self.local_ips = tuple(local_ips)
        probe = LossProbe(
            discover=lambda: Discovery(self.targets, exits=self.exits, local_ips=self.local_ips),
            attempt_fn=self.attempts, client_probe_fn=self.client, clock=self.clock,
            source_ip_fn=lambda ip, port: source_ip, stalled_fn=lambda _target: stalled,
        )
        probe.refresh_targets(self.targets, exits=self.exits, local_ips=self.local_ips)
        return probe

    @staticmethod
    def by_ip(rows: list[dict]) -> dict:
        return {row["local_ip"]: row for row in rows}

    async def rounds(self, probe: LossProbe, main: int = 0, reference: int = 0) -> None:
        for _ in range(main):
            await probe.run_round()
        for _ in range(reference):
            await probe.run_reference_round()

    async def test_main_attempts_are_recorded_per_exit(self):
        probe = self.make(exits=("192.0.2.10", "192.0.2.11"))
        await self.rounds(probe, main=4)
        rows = probe.ways(self.TARGET)
        self.assertEqual([(row["local_ip"], row["used"], row["samples"]) for row in rows],
                         [("192.0.2.10", True, 2), ("192.0.2.11", True, 2)])
        self.assertEqual(probe.stats_for(self.TARGET).samples, 4)
        # Попыток ровно столько же, сколько без разбивки: окна путей пишутся из тех же
        self.assertEqual(len(self.attempts.routes), 4)

    async def test_reference_round_probes_unused_addresses_in_turn(self):
        probe = self.make()
        await self.rounds(probe, reference=4)
        self.assertEqual([local_ip for _, local_ip in self.attempts.routes], [*self.SPARE, self.SPARE[0]])

    async def test_reference_results_do_not_touch_the_main_figure(self):
        probe = self.make(rtt={ip: None for ip in self.SPARE})
        await self.rounds(probe, main=3)
        before = probe.snapshot(set(Source))
        await self.rounds(probe, reference=6)
        self.assertEqual(probe.snapshot(set(Source)), before)
        self.assertEqual(probe.stats_for(self.TARGET).loss_pct, 0.0)
        rows = self.by_ip(probe.ways(self.TARGET))
        self.assertEqual([rows[ip]["loss_pct"] for ip in self.SPARE], [100.0] * 3)
        self.assertEqual((rows[self.USED]["loss_pct"], rows[self.USED]["used"]), (0.0, True))
        self.assertEqual({rows[ip]["used"] for ip in self.SPARE}, {False})

    async def test_default_exit_shows_the_real_source_address(self):
        probe = self.make(exits=None, source_ip="192.0.2.11")
        await self.rounds(probe, main=1)
        [used] = probe.ways(self.TARGET)
        self.assertEqual((used["local_ip"], used["used"]), ("192.0.2.11", True))
        self.assertEqual(probe.spare_for(self.TARGET), ("192.0.2.10", "192.0.2.12", "192.0.2.13"))
        # Сама попытка идёт как раньше — без привязки к адресу
        self.assertEqual(self.attempts.routes, [(self.TARGET, None)])

    async def test_unknown_source_address_leaves_every_address_spare(self):
        probe = self.make(exits=None, source_ip=None)
        await self.rounds(probe, main=1)
        [used] = probe.ways(self.TARGET)
        self.assertIsNone(used["local_ip"])
        self.assertEqual(probe.spare_for(self.TARGET), self.LOCAL)

    async def test_ipv6_target_has_no_spare_addresses(self):
        target = ("2001:db8::5", 443)
        probe = self.make(exits=None, target=target, source_ip="2001:db8::1")
        self.assertEqual(probe.spare_for(target), ())
        await self.rounds(probe, reference=2)
        self.assertEqual(self.attempts.routes, [])

    async def test_spare_addresses_are_capped(self):
        probe = self.make(local_ips=[f"192.0.2.{index}" for index in range(10, 22)])
        self.assertEqual(len(probe.spare_for(self.TARGET)), MAX_SPARE_WAYS)

    async def test_relay_with_a_single_address_has_only_the_used_way(self):
        probe = self.make(local_ips=(self.USED,))
        await self.rounds(probe, main=2, reference=2)
        self.assertEqual([row["local_ip"] for row in probe.ways(self.TARGET)], [self.USED])
        self.assertEqual(len(self.attempts.routes), 2)

    async def test_ways_forget_removed_addresses_and_targets(self):
        probe = self.make()
        await self.rounds(probe, main=1, reference=3)
        self.assertEqual(set(self.by_ip(probe.ways(self.TARGET))), set(self.LOCAL))

        probe.refresh_targets(self.targets, exits=self.exits, local_ips=self.LOCAL[:2])
        self.assertEqual(set(self.by_ip(probe.ways(self.TARGET))), set(self.LOCAL[:2]))

        probe.refresh_targets({}, exits={}, local_ips=self.LOCAL)
        self.assertEqual(probe.ways(self.TARGET), [])

    async def test_address_that_became_an_exit_starts_a_fresh_window(self):
        probe = self.make()
        await self.rounds(probe, reference=3)
        probe.refresh_targets(self.targets, exits={self.TARGET: (self.USED, self.SPARE[0])}, local_ips=self.LOCAL)
        self.assertNotIn(self.SPARE[0], self.by_ip(probe.ways(self.TARGET)))

    async def test_unread_local_addresses_keep_the_previous_list(self):
        probe = self.make()
        probe.refresh_targets(self.targets, exits=self.exits)
        self.assertEqual(probe.spare_for(self.TARGET), self.SPARE)

    async def test_slow_reference_round_does_not_delay_the_main_round(self):
        # Адрес без маршрута держит попытку до таймаута: основной замер его не ждёт
        release = asyncio.Event()

        async def attempt_fn(ip, port, local_ip=None):
            if local_ip != self.USED:
                await release.wait()
            return 10.0

        probe = self.make(attempts=attempt_fn)
        await asyncio.wait_for(probe.tick(self.clock.now), 1)
        first = probe._reference_task
        self.assertFalse(first.done())
        self.assertEqual(probe.stats_for(self.TARGET).samples, 1)

        await asyncio.wait_for(probe.tick(self.clock.now), 1)
        self.assertIs(probe._reference_task, first)
        self.assertEqual(probe.stats_for(self.TARGET).samples, 2)

        release.set()
        await first
        self.assertIn(self.SPARE[0], self.by_ip(probe.ways(self.TARGET)))

    async def test_stop_cancels_reference_tasks(self):
        async def attempt_fn(ip, port, local_ip=None):
            if local_ip != self.USED:
                await asyncio.Event().wait()
            return 10.0

        probe = self.make(attempts=attempt_fn)
        await probe.tick(self.clock.now)
        first = probe._reference_task
        await probe.stop()
        self.assertTrue(first.cancelled())
        self.assertIsNone(probe._reference_task)

    async def client_round(self, probe: LossProbe, ask: bool = True, sources: frozenset = frozenset(Source)) -> None:
        if ask:
            probe.request_client_probe(set(sources))
        await probe.tick(self.clock.now)
        for task in (probe._client_task, probe._reference_client_task, probe._reference_task):
            if task is not None:
                await task

    async def test_reference_client_probe_is_kept_apart(self):
        verdicts = {(*self.TARGET, self.USED): True, **{(*self.TARGET, ip): False for ip in self.SPARE}}
        probe = self.make(verdicts=verdicts)
        await self.client_round(probe)
        self.assertIs(probe.stats_for(self.TARGET).answers, True)
        rows = self.by_ip(probe.ways(self.TARGET))
        self.assertIs(rows[self.USED]["answers"], True)
        self.assertIs(rows[self.SPARE[0]]["answers"], False)

    async def test_silence_to_a_stranger_is_not_shown_when_clients_are_answered(self):
        # Счётчики HAProxy видят ответы клиентам: сервер просто не говорит с незнакомцем. Общий вердикт
        # адреса это молчание гасит — и в разбивке оно не должно выглядеть поводом менять выход
        probe = self.make(verdicts={(*self.TARGET, ip): False for ip in self.LOCAL}, stalled=False)
        await self.rounds(probe, reference=1)
        await self.client_round(probe)
        self.assertIsNone(probe.stats_for(self.TARGET).answers)
        rows = probe.ways(self.TARGET)
        self.assertEqual({row["used"] for row in rows}, {True, False})
        self.assertEqual({row["answers"] for row in rows}, {None})

    async def test_reference_client_probe_runs_only_while_asked_and_readable(self):
        probe = self.make()
        await self.client_round(probe, ask=False)
        self.assertEqual(self.client.routes, [])

        # Раздел HAProxy панели закрыт: адрес она не увидит, и рукопожатия ему ни к чему
        await self.client_round(probe, sources=frozenset({Source.DNAT}))
        self.assertEqual(self.client.routes, [])

        self.clock.now += CLIENT_PROBE_INTERVAL_SEC
        await self.client_round(probe)
        # По одной проверке: с выходного адреса правила и с очередного запасного
        routes = [local_ip for _, local_ip in self.client.routes]
        self.assertEqual(len(routes), 2)
        self.assertIn(self.USED, routes)
        self.assertLess(set(routes) - {self.USED}, set(self.SPARE))

    async def test_reference_verdicts_expire_with_the_request(self):
        probe = self.make(verdicts={(*self.TARGET, ip): False for ip in self.SPARE})
        await self.rounds(probe, reference=1)
        await self.client_round(probe)
        self.assertIs(self.by_ip(probe.ways(self.TARGET))[self.SPARE[0]]["answers"], False)

        self.clock.now += CLIENT_PROBE_LEASE_SEC + 1
        await self.client_round(probe, ask=False)
        self.assertIsNone(self.by_ip(probe.ways(self.TARGET))[self.SPARE[0]]["answers"])

    async def test_span_reflects_the_queue_length(self):
        probe = self.make()
        await self.rounds(probe, main=4, reference=6)
        rows = self.by_ip(probe.ways(self.TARGET))
        self.assertEqual((rows[self.USED]["samples"], rows[self.USED]["span_sec"]), (4, 8))
        self.assertEqual((rows[self.SPARE[0]]["samples"], rows[self.SPARE[0]]["span_sec"]), (2, 12))

    async def test_ways_over_real_sockets(self):
        try:
            with socket.socket() as bound:
                bound.bind(("127.0.0.2", 0))
        except OSError as exc:
            self.skipTest(f"127.0.0.2 is not a local address here: {exc}")
        server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
        target = ("127.0.0.1", server.sockets[0].getsockname()[1])
        targets, exits = {target: {Source.HAPROXY}}, {target: ("127.0.0.2",)}
        probe = LossProbe(discover=lambda: Discovery(targets))
        probe.refresh_targets(targets, exits=exits, local_ips=("127.0.0.2", "127.0.0.3"))
        try:
            await self.rounds(probe, main=3, reference=3)
        finally:
            server.close()
            await server.wait_closed()
        rows = self.by_ip(probe.ways(target))
        self.assertEqual((rows["127.0.0.2"]["used"], rows["127.0.0.2"]["loss_pct"], rows["127.0.0.2"]["samples"]),
                         (True, 0.0, 3))
        self.assertEqual((rows["127.0.0.3"]["used"], rows["127.0.0.3"]["loss_pct"], rows["127.0.0.3"]["samples"]),
                         (False, 0.0, 3))


class WaysRouteTests(unittest.IsolatedAsyncioTestCase):
    """Запрос разбивки по адресам релея: то же, что видно в метриках, и не больше."""

    TARGET = ("10.0.0.5", 443)
    PATH = "/api/loss-probe/ways"

    async def probe(self) -> LossProbe:
        targets = {self.TARGET: {Source.HAPROXY}}
        probe = LossProbe(discover=lambda: Discovery(targets), attempt_fn=RoutedAttempts(),
                          source_ip_fn=lambda ip, port: "192.0.2.10")
        probe.refresh_targets(targets, local_ips=("192.0.2.10", "192.0.2.11"))
        await probe.run_round()
        await probe.run_reference_round()
        return probe

    async def call(self, capabilities: str = "", ip: str = "10.0.0.5", port: int = 443):
        with mock.patch.object(loss_probe_router, "get_loss_probe", return_value=await self.probe()), \
                mock.patch.object(loss_probe_router, "get_policy", return_value=parse_capabilities(capabilities)):
            return await loss_probe_router.ways(ip=ip, port=port)

    async def test_ways_route_returns_the_breakdown(self):
        response = await self.call()
        self.assertEqual((response.ip, response.port), self.TARGET)
        self.assertEqual([(way.local_ip, way.used, way.loss_pct, way.samples, way.span_sec) for way in response.ways],
                         [("192.0.2.10", True, 0.0, 1, 2), ("192.0.2.11", False, 0.0, 1, 2)])

    async def all_call(self, capabilities: str = ""):
        with mock.patch.object(loss_probe_router, "get_loss_probe", return_value=await self.probe()),                 mock.patch.object(loss_probe_router, "get_policy", return_value=parse_capabilities(capabilities)):
            return await loss_probe_router.all_ways()

    async def test_all_ways_list_every_target_at_once(self):
        # Странице HAProxy нужны все бэкенды релея сразу: по запросу на каждый их было бы десятки
        response = await self.all_call()
        self.assertEqual([(entry.ip, entry.port, [way.local_ip for way in entry.ways]) for entry in response.targets],
                         [("10.0.0.5", 443, ["192.0.2.10", "192.0.2.11"])])

    async def test_all_ways_skip_targets_of_unreadable_sections(self):
        self.assertEqual((await self.all_call(capabilities="system")).targets, [])

    async def test_all_ways_skip_targets_without_attempts(self):
        targets = {self.TARGET: {Source.HAPROXY}}
        probe = LossProbe(discover=lambda: Discovery(targets), attempt_fn=RoutedAttempts())
        probe.refresh_targets(targets)
        self.assertEqual(probe.all_ways({Source.HAPROXY}), [])

    async def test_ways_of_unknown_target_are_empty(self):
        self.assertEqual((await self.call(ip="10.9.9.9")).ways, [])

    async def test_ways_hide_targets_of_unreadable_sections(self):
        # Панели закрыт раздел HAProxy: в метриках этого адреса нет — нет его и здесь
        self.assertEqual((await self.call(capabilities="system")).ways, [])
        self.assertEqual(len((await self.call(capabilities="system haproxy:ro")).ways), 2)

    async def test_ways_reject_a_bad_address(self):
        for bad in ("0.0.0.0", "224.0.0.1", "example.com"):
            with self.subTest(ip=bad), self.assertRaises(HTTPException) as caught:
                await self.call(ip=bad)
            self.assertEqual(caught.exception.status_code, 422)

    def test_ways_follow_the_system_domain(self):
        self.assertIsNone(parse_capabilities("").check("GET", self.PATH))
        self.assertIsNone(parse_capabilities("system:ro haproxy").check("GET", self.PATH))
        self.assertIsNotNone(parse_capabilities("haproxy").check("GET", self.PATH))


class LocalAddressDiscoveryTests(unittest.TestCase):
    def discover(self, addresses) -> Discovery:
        collector = mock.Mock(accounted_ipv4_addresses=mock.Mock(side_effect=addresses))
        with mock.patch.object(loss_probe_module, "discover_targets", return_value=Discovery({})), \
                mock.patch("app.services.metrics_collector.get_collector", return_value=collector):
            return discover_all()

    def test_discovery_adds_the_addresses_of_the_relay(self):
        self.assertEqual(self.discover([["10.0.0.1", "10.0.0.2"]]).local_ips, ("10.0.0.1", "10.0.0.2"))

    def test_unreadable_addresses_keep_the_previous_ones(self):
        # None — «не прочитаны»: адреса назначения всё равно обновляются, запасные остаются прежними
        self.assertIsNone(self.discover(OSError("no /proc")).local_ips)

    def test_the_running_probe_discovers_local_addresses(self):
        self.addCleanup(setattr, loss_probe_module, "_probe", loss_probe_module._probe)
        loss_probe_module._probe = None
        self.assertIs(loss_probe_module.get_loss_probe()._discover, discover_all)


if __name__ == "__main__":
    unittest.main()
