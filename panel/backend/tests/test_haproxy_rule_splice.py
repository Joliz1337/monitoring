"""Tests for editing HAProxy profile rules without rebuilding the whole config.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Правка правила через форму меняет только блок этого правила: ручные правки
global/defaults, чужие секции и остальные правила остаются как были, а свои
строки внутри правила (например `no option tcp-smart-connect` поверх defaults)
переезжают в пересобранный блок.
"""

import os
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.haproxy_config import (  # noqa: E402
    BackendServer,
    BalancerOptions,
    HAProxyRule,
    ProfileOptions,
    RuleBlockNotFoundError,
    SniMode,
    get_config_generator,
)

NO_OPTIONS = ProfileOptions()
SNI_ON = ProfileOptions(sni_filter_enabled=True, sni_filter_domains=("www.google.com",))

gen = get_config_generator()


def tcp_rule(name: str, port: int, target: str = "10.0.0.5", **kwargs) -> HAProxyRule:
    return HAProxyRule(name=name, rule_type="tcp", listen_port=port, target_ip=target, target_port=8443, **kwargs)


def https_rule(name: str, port: int) -> HAProxyRule:
    return HAProxyRule(name=name, rule_type="https", listen_port=port, target_ip="10.0.0.7",
                       target_port=80, cert_domain="example.com")


def balancer_rule(name: str, port: int) -> HAProxyRule:
    return HAProxyRule(
        name=name, rule_type="tcp", listen_port=port, target_ip="10.0.1.1", target_port=443,
        is_balancer=True, balancer_options=BalancerOptions(algorithm="leastconn"),
        servers=[BackendServer(name="srv1", address="10.0.1.1", port=443, weight=100),
                 BackendServer(name="srv2", address="10.0.1.2", port=443, weight=100)],
    )


RULES = [tcp_rule("a", 443), https_rule("web", 8443), balancer_rule("lb", 9443)]


def section(config: str, header: str) -> list[str]:
    lines = config.split("\n")
    start = lines.index(header)
    end = start + 1
    while end < len(lines) and lines[end].startswith(("    ", "#    ")):
        end += 1
    return [line.strip() for line in lines[start + 1:end]]


def with_line_after(config: str, anchor: str, line: str) -> str:
    return config.replace(anchor + "\n", anchor + "\n" + line + "\n", 1)


class PristineConfigTests(unittest.TestCase):
    """Нетронутый руками конфиг после правки совпадает с полной пересборкой."""

    def test_add_matches_full_generation(self):
        config = gen.generate_full_config(RULES[:2], NO_OPTIONS)
        self.assertEqual(gen.add_rule_to_config(config, RULES[2], NO_OPTIONS),
                         gen.generate_full_config(RULES, NO_OPTIONS))

    def test_add_to_empty_profile(self):
        config = gen.generate_full_config([], NO_OPTIONS)
        self.assertEqual(gen.add_rule_to_config(config, RULES[0], NO_OPTIONS),
                         gen.generate_full_config(RULES[:1], NO_OPTIONS))

    def test_update_matches_full_generation(self):
        config = gen.generate_full_config(RULES, NO_OPTIONS)
        changed = tcp_rule("a", 2053, target="10.9.9.9", send_proxy=True)
        self.assertEqual(gen.replace_rule_in_config(config, RULES[0], changed, NO_OPTIONS, NO_OPTIONS),
                         gen.generate_full_config([changed, *RULES[1:]], NO_OPTIONS))

    def test_delete_matches_full_generation(self):
        config = gen.generate_full_config(RULES, NO_OPTIONS)
        for index, rule in enumerate(RULES):
            rest = RULES[:index] + RULES[index + 1:]
            self.assertEqual(gen.remove_rule_from_config(config, rule),
                             gen.generate_full_config(rest, NO_OPTIONS))

    def test_delete_last_rule_leaves_bare_template(self):
        config = gen.generate_full_config(RULES[:1], NO_OPTIONS)
        self.assertEqual(gen.remove_rule_from_config(config, RULES[0]), gen.generate_base_config())

    def test_options_match_full_generation(self):
        config = gen.generate_full_config(RULES, NO_OPTIONS)
        self.assertEqual(gen.apply_options(config, NO_OPTIONS, SNI_ON), gen.generate_full_config(RULES, SNI_ON))
        self.assertEqual(gen.apply_options(gen.generate_full_config(RULES, SNI_ON), SNI_ON, NO_OPTIONS),
                         gen.generate_full_config(RULES, NO_OPTIONS))

    def test_unchanged_rule_keeps_config_identical(self):
        config = gen.generate_full_config(RULES, NO_OPTIONS)
        self.assertEqual(gen.replace_rule_in_config(config, RULES[2], RULES[2], NO_OPTIONS, NO_OPTIONS), config)


class ManualEditsTests(unittest.TestCase):
    def setUp(self):
        self.config = gen.generate_full_config(RULES, NO_OPTIONS).replace(
            "    option tcp-smart-connect\n", "    no option tcp-smart-connect\n    timeout tunnel 4h\n")
        self.config = self.config.replace(
            "# === RULES END ===",
            "# === RULES END ===\n\nlisten stats\n    bind *:8404\n    mode http\n    stats enable",
        )

    def test_defaults_and_foreign_sections_survive_every_operation(self):
        results = [
            gen.add_rule_to_config(self.config, tcp_rule("b", 2083), NO_OPTIONS),
            gen.replace_rule_in_config(self.config, RULES[0], tcp_rule("a", 444), NO_OPTIONS, NO_OPTIONS),
            gen.remove_rule_from_config(self.config, RULES[1]),
            gen.remove_rule_from_config(self.config, RULES[2]),
            gen.apply_options(self.config, NO_OPTIONS, SNI_ON),
        ]
        for result in results:
            self.assertIn("    no option tcp-smart-connect\n    timeout tunnel 4h\n", result)
            self.assertIn("listen stats\n    bind *:8404\n    mode http\n    stats enable", result)

    def test_new_rule_goes_before_end_marker(self):
        result = gen.add_rule_to_config(self.config, tcp_rule("b", 2083), NO_OPTIONS)
        self.assertLess(result.index("frontend tcp_b"), result.index("# === RULES END ==="))

    def test_other_rules_stay_byte_for_byte(self):
        tweaked = self.config.replace(
            "    server srv2 10.0.1.2:443 weight 100 check inter 5s fall 3 rise 2",
            "    server srv2 10.0.1.2:443 weight 100 check inter 30s fall 3 rise 2 on-marked-down shutdown-sessions",
        )
        result = gen.replace_rule_in_config(tweaked, RULES[0], tcp_rule("a", 444), NO_OPTIONS, NO_OPTIONS)
        self.assertIn("inter 30s fall 3 rise 2 on-marked-down shutdown-sessions", result)

    def test_untouched_section_of_edited_rule_stays_as_written(self):
        tweaked = self.config.replace(
            "    server srv1 10.0.0.5:8443 check inter 5s fall 3 rise 2",
            "    server srv1 10.0.0.5:8443 check inter 30s",
        )
        result = gen.replace_rule_in_config(tweaked, RULES[0], tcp_rule("a", 444), NO_OPTIONS, NO_OPTIONS)
        self.assertIn("bind *:444", result)
        self.assertIn("    server srv1 10.0.0.5:8443 check inter 30s\n", result)


class OwnRuleLinesTests(unittest.TestCase):
    def test_backend_override_survives_rule_edit(self):
        config = with_line_after(gen.generate_full_config(RULES, NO_OPTIONS),
                                 "\nbackend backend_tcp_a", "    no option tcp-smart-connect")
        changed = tcp_rule("a", 443, target="10.9.9.9")
        result = gen.replace_rule_in_config(config, RULES[0], changed, NO_OPTIONS, NO_OPTIONS)
        backend = section(result, "backend backend_tcp_a")
        self.assertEqual(backend[0], "no option tcp-smart-connect")
        self.assertIn("server srv1 10.9.9.9:8443 check inter 5s fall 3 rise 2", backend)

    def test_override_follows_rule_rename_and_balancer_switch(self):
        config = with_line_after(gen.generate_full_config(RULES, NO_OPTIONS),
                                 "    option tcp-check", "    timeout server 2h")
        result = gen.replace_rule_in_config(config, RULES[0], balancer_rule("renamed", 443),
                                            NO_OPTIONS, NO_OPTIONS)
        self.assertNotIn("backend backend_tcp_a", result)
        self.assertIn("timeout server 2h", section(result, "backend backend_tcp_renamed"))
        self.assertEqual(result.count("timeout server 2h"), 1)

    def test_own_line_keeps_place_among_template_lines(self):
        config = with_line_after(gen.generate_full_config(RULES[:1], SNI_ON),
                                 "    tcp-request inspect-delay 5s", "    tcp-request content accept if { src 1.2.3.4 }")
        result = gen.replace_rule_in_config(config, RULES[0], tcp_rule("a", 444), SNI_ON, SNI_ON)
        frontend = section(result, "frontend tcp_a")
        self.assertLess(frontend.index("tcp-request content accept if { src 1.2.3.4 }"),
                        frontend.index("tcp-request content silent-drop unless sni_allowed"))

    def test_form_owned_lines_are_taken_from_form(self):
        config = gen.generate_full_config([balancer_rule("lb", 9443)], NO_OPTIONS)
        config = config.replace("    bind *:9443\n", "    bind *:9443 tfo\n").replace(
            "    balance leastconn\n", "    balance leastconn\n    retries 3\n")
        changed = replace(balancer_rule("lb", 9444), balancer_options=BalancerOptions(algorithm="source", retries=5))
        result = gen.replace_rule_in_config(config, balancer_rule("lb", 9443), changed, NO_OPTIONS, NO_OPTIONS)
        self.assertEqual([line for line in section(result, "frontend tcp_lb") if line.startswith("bind")],
                         ["bind *:9444"])
        backend = section(result, "backend backend_tcp_lb")
        self.assertEqual([line for line in backend if line.startswith(("balance", "retries"))],
                         ["balance source", "retries 5"])

    def test_sni_lines_are_not_duplicated_when_filter_changes(self):
        config = with_line_after(gen.generate_full_config(RULES[:1], SNI_ON),
                                 "    bind *:443\n    mode tcp", "    tcp-request connection reject if { src 6.6.6.6 }")
        new_options = ProfileOptions(sni_filter_enabled=True, sni_filter_domains=("yahoo.com",))
        frontend = section(gen.apply_options(config, SNI_ON, new_options), "frontend tcp_a")
        self.assertEqual([line for line in frontend if line.startswith("acl")],
                         ["acl sni_allowed req.ssl_sni -i yahoo.com"])
        self.assertIn("tcp-request connection reject if { src 6.6.6.6 }", frontend)

    def test_rule_switched_to_off_keeps_single_marker(self):
        config = gen.generate_full_config(RULES[:1], SNI_ON)
        result = gen.replace_rule_in_config(config, RULES[0], tcp_rule("a", 443, sni_mode=SniMode.OFF),
                                            SNI_ON, SNI_ON)
        self.assertEqual(section(result, "frontend tcp_a").count("# sni-filter (off)"), 1)
        self.assertNotIn("sni-filter (profile)", result)


class SectionBoundaryTests(unittest.TestCase):
    def test_comment_heading_next_section_is_kept_on_delete(self):
        config = gen.generate_full_config(RULES[:2], NO_OPTIONS).replace(
            "\nfrontend https_web", "\n# сайт клиента\nfrontend https_web")
        result = gen.remove_rule_from_config(config, RULES[0])
        self.assertIn("# сайт клиента\nfrontend https_web", result)

    def test_commented_out_directive_inside_block_stays_inside(self):
        config = with_line_after(gen.generate_full_config(RULES[:1], NO_OPTIONS),
                                 "    option tcp-check", "#    option tcp-smart-connect")
        result = gen.replace_rule_in_config(config, RULES[0], tcp_rule("a", 443, target="10.9.9.9"),
                                            NO_OPTIONS, NO_OPTIONS)
        self.assertEqual(result.count("server srv1"), 1)
        self.assertIn("#    option tcp-smart-connect", section(result, "backend backend_tcp_a"))

    def test_config_without_markers_gets_rule_appended(self):
        result = gen.add_rule_to_config("defaults\n    mode tcp\n", RULES[0], NO_OPTIONS)
        self.assertTrue(result.startswith("defaults\n    mode tcp\n\nfrontend tcp_a\n"))
        self.assertEqual(gen.parse_rules_from_config(result)[0].name, "a")

    def test_missing_block_is_reported(self):
        config = gen.generate_full_config(RULES[:1], NO_OPTIONS).replace("\nbackend backend_tcp_a", "\nbackend other")
        with self.assertRaises(RuleBlockNotFoundError):
            gen.remove_rule_from_config(config, RULES[0])


if __name__ == "__main__":
    unittest.main()
