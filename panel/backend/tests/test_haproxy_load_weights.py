"""Tests for load-based weights of HAProxy balancer backends.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Ошибка в формуле не видна до часа пик: забитый сервер продолжит получать
клиентов, а нули у всех бэкендов оставили бы релей без серверов вовсе.
Числа — из примера оператора: 3 Гбит/с и два по 10 Гбит/с.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.haproxy_addresses import ServerAddresses, render_for_server  # noqa: E402
from app.services.haproxy_config import (  # noqa: E402
    BackendServer,
    BalancerOptions,
    HAProxyRule,
    ProfileOptions,
    get_config_generator,
)
from app.services.haproxy_load_weights import (  # noqa: E402
    NodeLoad,
    load_factor,
    relay_weights,
    rule_line_weights,
    rule_views,
    should_push,
    utilization,
)

MBIT = 1_000_000 / 8


def line(name, address, port=443, **kw) -> BackendServer:
    return BackendServer(name, address, port, weight=100, **kw)


NL = [line("srv1", "10.0.0.1"), line("srv2", "10.0.0.2"), line("srv3", "10.0.0.3")]
OWNERS = {"10.0.0.1": 1, "10.0.0.2": 2, "10.0.0.3": 3}
CAPS = {1: 3000, 2: 10000, 3: 10000}


def net(mbit, cpu=0.0) -> NodeLoad:
    return NodeLoad(1 * MBIT, mbit * MBIT, cpu)


def weights(lines) -> list[int]:
    return [w.weight for w in lines]


class FormulaTests(unittest.TestCase):
    def test_load_factor_steps(self):
        for util, expected in ((0.5, 1), (0.70, 1), (0.80, 0.6), (0.90, 0.2), (0.95, 0), (1.2, 0)):
            self.assertAlmostEqual(load_factor(util), expected, places=6, msg=util)

    def test_bigger_direction_counts(self):
        self.assertAlmostEqual(utilization(NodeLoad(100 * MBIT, 2700 * MBIT, 0), 3000), 0.9)
        self.assertAlmostEqual(utilization(NodeLoad(2700 * MBIT, 100 * MBIT, 0), 3000), 0.9)

    def test_cpu_dominates(self):
        [w] = rule_line_weights([NL[1]], OWNERS, CAPS, {2: net(4000, cpu=85)})
        self.assertEqual(w.weight, 40)

    def test_idle_follows_capacity(self):
        loads = {1: net(0), 2: net(0), 3: net(0)}
        self.assertEqual(weights(rule_line_weights(NL, OWNERS, CAPS, loads)), [30, 100, 100])

    def test_operator_example(self):
        loads = {1: net(2700), 2: net(4000), 3: net(4000)}
        self.assertEqual(weights(rule_line_weights(NL, OWNERS, CAPS, loads)), [6, 100, 100])

    def test_all_saturated_fall_back_to_capacity(self):
        loads = {1: net(2900), 2: net(9600), 3: net(9600)}
        self.assertEqual(weights(rule_line_weights(NL, OWNERS, CAPS, loads)), [30, 100, 100])

    def test_missing_load_keeps_base(self):
        result = rule_line_weights(NL[:2], OWNERS, CAPS, {2: net(9000)})
        self.assertEqual(weights(result), [30, 20])
        self.assertIsNone(result[0].utilization)
        self.assertAlmostEqual(result[1].utilization, 0.9)

    def test_unowned_and_uncapped_lines_are_skipped(self):
        lines = NL + [line("ext", "203.0.113.9")]
        result = rule_line_weights(lines, OWNERS, {1: 3000, 2: 10000}, {})
        self.assertEqual([w.server_name for w in result], ["srv1", "srv2"])

    def test_two_addresses_of_one_node_split(self):
        lines = [line("a1", "10.0.0.2"), line("a2", "10.0.0.9"), line("b", "10.0.0.3")]
        owners = {"10.0.0.2": 2, "10.0.0.9": 2, "10.0.0.3": 3}
        self.assertEqual(weights(rule_line_weights(lines, owners, CAPS, {})), [50, 50, 100])

    def test_source_copies_share_weight(self):
        lines = [line("srv2", "10.0.0.2"), line("srv2_o2", "10.0.0.2"), line("srv3", "10.0.0.3")]
        self.assertEqual(weights(rule_line_weights(lines, OWNERS, CAPS, {})), [100, 100, 100])

    def test_backup_and_disabled_ignored(self):
        lines = [NL[0], line("bk", "10.0.0.2", backup=True), line("off", "10.0.0.3", disabled=True)]
        self.assertEqual([w.server_name for w in rule_line_weights(lines, OWNERS, CAPS, {})], ["srv1"])

    def test_small_weight_never_rounds_to_zero(self):
        lines = [line("a1", "10.0.0.1"), line("a2", "10.0.0.7"), line("a3", "10.0.0.8"), line("b", "10.0.0.3")]
        owners = {"10.0.0.1": 1, "10.0.0.7": 1, "10.0.0.8": 1, "10.0.0.3": 3}
        result = rule_line_weights(lines, owners, {1: 300, 3: 10000}, {1: net(270)})
        self.assertEqual(weights(result), [1, 1, 1, 100])

    def test_domain_lookup_is_case_insensitive(self):
        [w] = rule_line_weights([line("d", "ND1.Example.com")], {"nd1.example.com": 2}, CAPS, {})
        self.assertEqual((w.panel_server_id, w.capacity_mbit, w.weight), (2, 10000, 100))


def profile_config(algorithm="leastconn", load_weights=True) -> str:
    rule = HAProxyRule(name="lb", rule_type="tcp", listen_port=443, target_ip="", target_port=0,
                       is_balancer=True, servers=[line(s.name, s.address) for s in NL],
                       balancer_options=BalancerOptions(algorithm=algorithm, load_weights=load_weights))
    return get_config_generator().generate_full_config([rule], ProfileOptions())


class RelayWeightsTests(unittest.TestCase):
    def test_source_copies_get_same_weight(self):
        rendered = render_for_server(profile_config(), ServerAddresses(source=("192.0.2.1", "192.0.2.2")))
        self.assertEqual(relay_weights(rendered, OWNERS, CAPS, {}), {
            ("backend_tcp_lb", "srv1"): 30, ("backend_tcp_lb", "srv1_o2"): 30,
            ("backend_tcp_lb", "srv2"): 100, ("backend_tcp_lb", "srv2_o2"): 100,
            ("backend_tcp_lb", "srv3"): 100, ("backend_tcp_lb", "srv3_o2"): 100,
        })

    def test_rule_without_flag_ignored(self):
        self.assertEqual(relay_weights(profile_config(load_weights=False), OWNERS, CAPS, {}), {})

    def test_hand_written_marker_on_static_algorithm_ignored(self):
        config = profile_config().replace("balance leastconn", "balance static-rr")
        self.assertEqual(relay_weights(config, OWNERS, CAPS, {}), {})

    def test_rule_views_keyed_by_rule_name(self):
        views = rule_views(profile_config(), OWNERS, CAPS, {})
        self.assertEqual([w.weight for w in views["lb"]], [30, 100, 100])


class ShouldPushTests(unittest.TestCase):
    K1, K2 = ("b", "s1"), ("b", "s2")

    def test_small_change_is_quiet(self):
        self.assertFalse(should_push({self.K1: 100, self.K2: 30}, {self.K1: 97, self.K2: 30}, 10, False))

    def test_step_change_pushes(self):
        self.assertTrue(should_push({self.K1: 100, self.K2: 30}, {self.K1: 95, self.K2: 30}, 10, False))

    def test_zero_crossing_pushes(self):
        self.assertTrue(should_push({self.K1: 1}, {self.K1: 0}, 10, False))
        self.assertTrue(should_push({self.K1: 0}, {self.K1: 1}, 10, False))

    def test_changed_keys_push(self):
        self.assertTrue(should_push({self.K1: 100, self.K2: 30}, {self.K1: 100}, 10, False))

    def test_resend_and_force_and_first_push(self):
        prev = {self.K1: 100}
        self.assertTrue(should_push(prev, prev, 60, False))
        self.assertTrue(should_push(prev, prev, 10, True))
        self.assertTrue(should_push(None, prev, None, False))

    def test_nothing_to_send(self):
        self.assertFalse(should_push({self.K1: 100}, {}, 10, True))


if __name__ == "__main__":
    unittest.main()
