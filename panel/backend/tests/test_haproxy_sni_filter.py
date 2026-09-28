"""Tests for the HAProxy SNI filter (profile-wide list and per-rule override).

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Правило без своей настройки берёт список профиля; свой список правила или
выключение перекрывают профиль. Режим правила обязан переживать разбор
конфига обратно — иначе смена общего списка затёрла бы переопределения.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.haproxy_addresses import ServerAddresses, render_for_server  # noqa: E402
from app.services.haproxy_config import (  # noqa: E402
    MAX_SNI_DOMAINS,
    SNI_DOMAINS_PER_LINE,
    BackendServer,
    HAProxyRule,
    InvalidSniError,
    ProfileOptions,
    SniMode,
    get_config_generator,
    normalize_sni_domains,
)

PROFILE_SNI = ProfileOptions(sni_filter_enabled=True, sni_filter_domains=("www.google.com", "yahoo.com"))


def tcp_rule(name: str, port: int, **kwargs) -> HAProxyRule:
    return HAProxyRule(name=name, rule_type="tcp", listen_port=port, target_ip="10.0.0.5", target_port=8443, **kwargs)


def frontend(config: str, name: str) -> str:
    start = config.index(f"frontend tcp_{name}\n")
    return config[start:config.index("\nbackend ", start)]


def generate(rules: list[HAProxyRule], options: ProfileOptions) -> str:
    return get_config_generator().generate_full_config(rules, options)


class ProfileFilterTests(unittest.TestCase):
    def test_disabled_profile_adds_nothing(self):
        config = generate([tcp_rule("a", 443)], ProfileOptions(sni_filter_domains=("www.google.com",)))
        self.assertNotIn("sni-filter", config)
        self.assertNotIn("req.ssl_sni", config)

    def test_rule_inherits_profile_list(self):
        block = frontend(generate([tcp_rule("a", 443)], PROFILE_SNI), "a")
        self.assertIn("# sni-filter (profile)", block)
        self.assertIn("tcp-request inspect-delay 5s", block)
        self.assertIn("acl sni_allowed req.ssl_sni -i www.google.com yahoo.com", block)
        self.assertIn("tcp-request content silent-drop unless sni_allowed", block)

    def test_acl_is_declared_before_drop_and_backend_switch(self):
        lines = [line.strip() for line in frontend(generate([tcp_rule("a", 443)], PROFILE_SNI), "a").splitlines()]
        acl = next(i for i, line in enumerate(lines) if line.startswith("acl sni_allowed"))
        drop = lines.index("tcp-request content silent-drop unless sni_allowed")
        backend = next(i for i, line in enumerate(lines) if line.startswith("default_backend"))
        self.assertLess(acl, drop)
        self.assertLess(drop, backend)

    def test_foreign_sni_gets_no_reply(self):
        # reject закрыл бы соединение RST-ом — сканер понял бы, что за портом кто-то есть
        self.assertNotIn("content reject", generate([tcp_rule("a", 443)], PROFILE_SNI))

    def test_balancer_rule_is_filtered_too(self):
        rule = HAProxyRule(
            name="lb", rule_type="tcp", listen_port=8443, target_ip="", target_port=0, is_balancer=True,
            servers=[BackendServer(name="a", address="10.0.0.5", port=443)],
        )
        self.assertIn("acl sni_allowed req.ssl_sni -i www.google.com yahoo.com", frontend(generate([rule], PROFILE_SNI), "lb"))

    def test_https_rule_is_not_filtered(self):
        rule = HAProxyRule(name="web", rule_type="https", listen_port=443, target_ip="10.0.0.5",
                           target_port=80, cert_domain="example.com")
        config = generate([rule], PROFILE_SNI)
        self.assertNotIn("sni-filter", config)
        self.assertNotIn("ssl_sni", config)


class RuleOverrideTests(unittest.TestCase):
    def test_custom_list_overrides_profile(self):
        rule = tcp_rule("a", 443, sni_mode=SniMode.CUSTOM, sni_domains=("vk.com",))
        block = frontend(generate([rule], PROFILE_SNI), "a")
        self.assertIn("# sni-filter (custom)", block)
        self.assertIn("acl sni_allowed req.ssl_sni -i vk.com", block)
        self.assertNotIn("google", block)

    def test_custom_list_works_with_disabled_profile(self):
        rule = tcp_rule("a", 443, sni_mode=SniMode.CUSTOM, sni_domains=("vk.com",))
        self.assertIn("req.ssl_sni -i vk.com", generate([rule], ProfileOptions()))

    def test_off_disables_profile_filter_for_rule(self):
        config = generate([tcp_rule("a", 443, sni_mode=SniMode.OFF), tcp_rule("b", 444)], PROFILE_SNI)
        block = frontend(config, "a")
        self.assertIn("# sni-filter (off)", block)
        self.assertNotIn("req.ssl_sni", block)
        self.assertIn("req.ssl_sni", frontend(config, "b"))

    def test_long_list_is_split_into_ored_acl_lines(self):
        domains = tuple(f"host{i}.example.com" for i in range(SNI_DOMAINS_PER_LINE * 2 + 5))
        rule = tcp_rule("a", 443, sni_mode=SniMode.CUSTOM, sni_domains=domains)
        acl_lines = [line for line in frontend(generate([rule], ProfileOptions()), "a").splitlines()
                     if "acl sni_allowed" in line]
        self.assertEqual(len(acl_lines), 3)
        self.assertEqual(tuple(d for line in acl_lines for d in line.split()[4:]), domains)


class WildcardTests(unittest.TestCase):
    def block(self, *domains: str) -> str:
        rule = tcp_rule("a", 443, sni_mode=SniMode.CUSTOM, sni_domains=domains)
        return frontend(generate([rule], ProfileOptions()), "a")

    def test_wildcard_allows_apex_and_subdomains(self):
        block = self.block("vk.com", "*.nexyonn.com")
        self.assertIn("acl sni_allowed req.ssl_sni -i vk.com nexyonn.com", block)
        self.assertIn("acl sni_allowed req.ssl_sni -i -m end .nexyonn.com", block)

    def test_suffix_keeps_leading_dot(self):
        # Без точки -m end пропустил бы и evilnexyonn.com
        suffix_lines = [line for line in self.block("*.nexyonn.com").splitlines() if "-m end" in line]
        self.assertTrue(all(pattern.startswith(".") for line in suffix_lines for pattern in line.split()[6:]))

    def test_explicit_apex_next_to_wildcard_is_not_duplicated(self):
        self.assertEqual(self.block("nexyonn.com", "*.nexyonn.com").count(" nexyonn.com"), 1)

    def test_profile_list_supports_wildcards(self):
        options = ProfileOptions(sni_filter_enabled=True, sni_filter_domains=("*.nexyonn.com",))
        self.assertIn("-m end .nexyonn.com", frontend(generate([tcp_rule("a", 443)], options), "a"))

    def test_wildcards_survive_parsing(self):
        domains = ("vk.com",) + tuple(f"*.z{i}.io" for i in range(SNI_DOMAINS_PER_LINE + 2))
        rule = tcp_rule("a", 443, sni_mode=SniMode.CUSTOM, sni_domains=domains)
        config = generate([rule], ProfileOptions())
        parsed = get_config_generator().parse_rules_from_config(config)
        self.assertEqual(parsed[0].sni_domains, domains)
        self.assertEqual(generate(parsed, ProfileOptions()), config)


class RoundTripTests(unittest.TestCase):
    RULES = [
        tcp_rule("inherit", 443),
        tcp_rule("own", 444, sni_mode=SniMode.CUSTOM, sni_domains=tuple(f"h{i}.io" for i in range(23))),
        tcp_rule("plain", 445, sni_mode=SniMode.OFF),
    ]

    def parse(self, config: str) -> dict[str, HAProxyRule]:
        return {r.name: r for r in get_config_generator().parse_rules_from_config(config)}

    def test_modes_and_own_list_survive_parsing(self):
        for options in (PROFILE_SNI, ProfileOptions()):
            with self.subTest(profile_enabled=options.sni_filter_enabled):
                parsed = self.parse(generate(self.RULES, options))
                self.assertEqual(parsed["inherit"].sni_mode, SniMode.PROFILE)
                self.assertEqual(parsed["inherit"].sni_domains, ())
                self.assertEqual(parsed["own"].sni_mode, SniMode.CUSTOM)
                self.assertEqual(parsed["own"].sni_domains, self.RULES[1].sni_domains)
                self.assertEqual(parsed["plain"].sni_mode, SniMode.OFF)

    def test_regeneration_is_stable(self):
        config = generate(self.RULES, PROFILE_SNI)
        self.assertEqual(generate(list(self.parse(config).values()), PROFILE_SNI), config)

    def test_new_profile_list_reaches_only_inheriting_rules(self):
        config = generate(self.RULES, PROFILE_SNI)
        updated = generate(list(self.parse(config).values()),
                           ProfileOptions(sni_filter_enabled=True, sni_filter_domains=("vk.com",)))
        self.assertIn("req.ssl_sni -i vk.com", frontend(updated, "inherit"))
        self.assertNotIn("vk.com", frontend(updated, "own"))
        self.assertNotIn("req.ssl_sni", frontend(updated, "plain"))

    def test_server_addresses_keep_filter_lines(self):
        config = generate([tcp_rule("a", 443)], PROFILE_SNI)
        rendered = render_for_server(config, ServerAddresses(listen=("1.1.1.1",)))
        block = frontend(rendered, "a")
        self.assertIn("bind 1.1.1.1:443", block)
        self.assertIn("tcp-request content silent-drop unless sni_allowed", block)


class ValidationTests(unittest.TestCase):
    def test_normalize_lowercases_dedupes_and_skips_blanks(self):
        self.assertEqual(
            normalize_sni_domains([" WWW.Google.com ", "www.google.com.", "", "yahoo.com"]),
            ("www.google.com", "yahoo.com"),
        )

    def test_normalize_rejects_values_that_break_acl(self):
        for bad in ("two words", "evil#comment", "a..b", "{x}", "*", "*.com", "a.*.com", "**.x.com", "*x.com"):
            with self.subTest(value=bad):
                with self.assertRaises(InvalidSniError):
                    normalize_sni_domains([bad])

    def test_normalize_drops_apex_covered_by_wildcard(self):
        self.assertEqual(
            normalize_sni_domains(["nexyonn.com", "*.NEXYONN.com", "vk.com"]),
            ("*.nexyonn.com", "vk.com"),
        )

    def test_normalize_limits_list_size(self):
        with self.assertRaises(InvalidSniError):
            normalize_sni_domains(f"h{i}.io" for i in range(MAX_SNI_DOMAINS + 1))

    def test_custom_mode_needs_domains(self):
        ok, _ = get_config_generator().validate_rule(tcp_rule("a", 443, sni_mode=SniMode.CUSTOM))
        self.assertFalse(ok)

    def test_enabled_profile_needs_domains(self):
        ok, _ = get_config_generator().validate_options(ProfileOptions(sni_filter_enabled=True))
        self.assertFalse(ok)
        ok, _ = get_config_generator().validate_options(ProfileOptions())
        self.assertTrue(ok)

    def test_options_from_garbage_fall_back_to_defaults(self):
        for data in (None, [], {"sni_filter_domains": "vk.com"}, {"sni_filter_domains": [1, "vk.com"]}):
            with self.subTest(data=data):
                options = ProfileOptions.from_dict(data)
                self.assertFalse(options.sni_filter_enabled)
                self.assertIn(options.sni_filter_domains, ((), ("vk.com",)))

    def test_options_dict_round_trip(self):
        self.assertEqual(ProfileOptions.from_dict(PROFILE_SNI.to_dict()), PROFILE_SNI)


if __name__ == "__main__":
    unittest.main()
