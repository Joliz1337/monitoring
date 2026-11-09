"""Tests for the load-based weights flag of a balancer rule.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Флаг живёт маркером в секции backend: правило без флага должно собираться
байт-в-байт как раньше, иначе у всех серверов разошлись бы хэши профиля, а
маркер не должен дублироваться при правке формы и теряться при сборке под сервер.
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

gen = get_config_generator()


def lb_rule(load_weights: bool, algorithm: str = "leastconn") -> HAProxyRule:
    servers = [BackendServer(f"srv{i}", f"10.0.0.{i}", 443, weight=100) for i in (1, 2, 3)]
    return HAProxyRule(name="lb", rule_type="tcp", listen_port=443, target_ip="", target_port=0,
                       is_balancer=True, servers=servers,
                       balancer_options=BalancerOptions(algorithm=algorithm, load_weights=load_weights))


class MarkerTests(unittest.TestCase):
    def test_marker_follows_balance_line(self):
        lines = gen.generate_rule_block(lb_rule(True), ProfileOptions()).split("\n")
        i = lines.index("    balance leastconn")
        self.assertEqual(lines[i + 1], "    # load-weights")

    def test_no_marker_without_flag(self):
        self.assertNotIn("# load-weights", gen.generate_rule_block(lb_rule(False), ProfileOptions()))

    def test_parse_roundtrip(self):
        for flag in (True, False):
            config = gen.generate_full_config([lb_rule(flag)], ProfileOptions())
            [rule] = gen.parse_rules_from_config(config)
            self.assertIs(rule.balancer_options.load_weights, flag)

    def test_toggle_off_removes_marker_and_on_twice_keeps_one(self):
        config = gen.generate_full_config([lb_rule(True)], ProfileOptions())
        off = gen.replace_rule_in_config(config, lb_rule(True), lb_rule(False), ProfileOptions(), ProfileOptions())
        self.assertNotIn("# load-weights", off)
        changed = lb_rule(True)
        changed.servers[0].weight = 50
        again = gen.replace_rule_in_config(config, lb_rule(True), changed, ProfileOptions(), ProfileOptions())
        self.assertEqual(again.count("# load-weights"), 1)

    def test_marker_survives_source_copies(self):
        config = gen.generate_full_config([lb_rule(True)], ProfileOptions())
        rendered = render_for_server(config, ServerAddresses(source=("192.0.2.1", "192.0.2.2")))
        self.assertEqual(rendered.count("# load-weights"), 1)

    def test_validate_rejects_static_algorithms(self):
        for algorithm, ok in (("leastconn", True), ("roundrobin", True), ("random", True),
                              ("static-rr", False), ("source", False), ("first", False)):
            self.assertIs(gen.validate_rule(lb_rule(True, algorithm))[0], ok, algorithm)


if __name__ == "__main__":
    unittest.main()
