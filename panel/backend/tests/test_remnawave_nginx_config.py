"""Tests for the Remnawave nginx config generator.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests
(pytest picks these up too — they are ordinary unittest TestCases.)

Главное свойство — round-trip: generate → parse → generate даёт идентичный
конфиг, иначе CRUD правил на панели молча терял бы данные.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.remnawave_nginx_config import (  # noqa: E402
    AUTO_MARKER,
    DOMAIN_PLACEHOLDER,
    GrpcRule,
    MissingMarkersError,
    OptionsValidationError,
    ProfileOptions,
    ProxyRule,
    RuleValidationError,
    detect_domain,
    generate_full_config,
    has_markers,
    parse_rules_from_config,
    render_for_server,
    replace_domain_with_placeholder,
    splice_rules,
    validate_options,
    validate_rules,
)

GRPC_RULES = [
    GrpcRule(name="trgrpc", service_path="trgrpc", port=8443),
    GrpcRule(name="vlgrpc", service_path="vlgrpc", port=8444),
]
PROXY_RULES = [
    ProxyRule(name="fallback", path="/", target_url="https://example.com"),
]


class RoundTripTests(unittest.TestCase):
    def _assert_round_trip(self, options: ProfileOptions):
        config = generate_full_config(options, GRPC_RULES, PROXY_RULES)
        grpc, proxy = parse_rules_from_config(config)
        self.assertEqual(grpc, GRPC_RULES)
        self.assertEqual(proxy, PROXY_RULES)
        regenerated = generate_full_config(options, grpc, proxy)
        self.assertEqual(config, regenerated)

    def test_scheme_direct(self):
        self._assert_round_trip(ProfileOptions())

    def test_scheme_cdn(self):
        self._assert_round_trip(ProfileOptions(
            cdn_enabled=True, cdn_ranges=["173.245.48.0/20", "2400:cb00::/32"],
        ))

    def test_scheme_proxy_protocol(self):
        self._assert_round_trip(ProfileOptions(
            proxy_protocol_enabled=True, proxy_protocol_port=8449, haproxy_ip="10.0.0.1",
        ))

    def test_scheme_universal(self):
        self._assert_round_trip(ProfileOptions(
            cdn_enabled=True, cdn_ranges=["173.245.48.0/20"],
            proxy_protocol_enabled=True, proxy_protocol_port=8449, haproxy_ip="10.0.0.1",
            reject_default_server=True,
        ))

    def test_scheme_with_fallback(self):
        config = generate_full_config(
            ProfileOptions(fallback_url="https://example.com"), GRPC_RULES, [],
        )
        grpc, proxy = parse_rules_from_config(config)
        self.assertEqual(grpc, GRPC_RULES)
        self.assertEqual(proxy, [])
        regenerated = generate_full_config(
            ProfileOptions(fallback_url="https://example.com"), grpc, proxy,
        )
        self.assertEqual(config, regenerated)

    def test_splice_preserves_manual_edits_outside_markers(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES, PROXY_RULES)
        edited = config.replace("keepalive_timeout 75s;", "keepalive_timeout 90s;")
        new_rules = GRPC_RULES + [GrpcRule(name="vmgrpc", service_path="vmgrpc", port=8445)]
        spliced = splice_rules(edited, new_rules, PROXY_RULES, ProfileOptions())
        self.assertIn("keepalive_timeout 90s;", spliced)
        grpc, proxy = parse_rules_from_config(spliced)
        self.assertEqual(grpc, new_rules)
        self.assertEqual(proxy, PROXY_RULES)


class ContentTests(unittest.TestCase):
    def test_grpc_uses_grpc_set_header_overwrite(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES, [])
        self.assertIn("grpc_set_header X-Forwarded-For $remote_addr;", config)
        self.assertNotIn("$proxy_add_x_forwarded_for", config)

    def test_cdn_switches_to_client_ip(self):
        config = generate_full_config(
            ProfileOptions(cdn_enabled=True, cdn_ranges=["173.245.48.0/20"]),
            GRPC_RULES, PROXY_RULES,
        )
        self.assertIn("grpc_set_header X-Forwarded-For $client_ip;", config)
        self.assertIn("geo $remote_addr $from_edge", config)
        self.assertIn("173.245.48.0/20 1;", config)

    def test_proxy_protocol_listen_and_realip(self):
        config = generate_full_config(
            ProfileOptions(proxy_protocol_enabled=True, proxy_protocol_port=8449,
                           haproxy_ip="10.0.0.1"),
            GRPC_RULES, [],
        )
        self.assertIn("listen 8449 ssl proxy_protocol;", config)
        self.assertIn("set_real_ip_from 10.0.0.1;", config)
        self.assertIn("real_ip_header proxy_protocol;", config)

    def test_proxy_protocol_without_haproxy_ip_trusts_all(self):
        # Пустой IP HAProxy = принимать PP-заголовок от всех, защита — файрвол
        config = generate_full_config(
            ProfileOptions(proxy_protocol_enabled=True, proxy_protocol_port=8449),
            GRPC_RULES, [],
        )
        self.assertIn("set_real_ip_from 0.0.0.0/0;", config)

    def test_fallback_generates_locations_and_error_page(self):
        config = generate_full_config(
            ProfileOptions(fallback_url="https://example.com"), GRPC_RULES, [],
        )
        self.assertIn("proxy_pass https://example.com;", config)
        self.assertIn("error_page 502 503 504 = @fallback;", config)
        self.assertIn("location @fallback {", config)
        self.assertIn("location / {", config)

    def test_no_fallback_no_error_page(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES, [])
        self.assertNotIn("@fallback", config)

    def test_fallback_conflicts_with_root_proxy_rule(self):
        with self.assertRaises(RuleValidationError):
            generate_full_config(
                ProfileOptions(fallback_url="https://1.2.3.4:8445"),
                GRPC_RULES, PROXY_RULES,  # PROXY_RULES содержит path="/"
            )

    def test_host_specific_limits_are_marked_auto(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES, [])
        for directive in ("worker_rlimit_nofile", "worker_connections", "ssl_session_cache"):
            line = next(l for l in config.splitlines() if l.strip().startswith(directive))
            self.assertIn(AUTO_MARKER, line, f"{directive} должен пересчитываться нодой")

    def test_grpc_stream_not_limited_by_body_size(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES, [])
        self.assertIn("client_max_body_size 0;", config)

    def test_response_is_indistinguishable_from_backend(self):
        """Никаких своих заголовков в ответ: клиент должен получать ровно то,
        что отдала бы заглушка при прямом обращении."""
        config = generate_full_config(
            ProfileOptions(fallback_url="https://1.2.3.4:8445"), GRPC_RULES, [],
        )
        self.assertNotIn("add_header", config)
        self.assertIn("proxy_pass_header Server;", config)

    def test_dead_fallback_drops_connection(self):
        config = generate_full_config(
            ProfileOptions(fallback_url="https://1.2.3.4:8445"), GRPC_RULES, [],
        )
        self.assertIn("error_page 502 503 504 = @drop;", config)
        self.assertIn("location @drop {", config)
        self.assertIn("return 444;", config)

    def test_rules_do_not_reference_drop_without_fallback(self):
        """@drop генерируется только вместе с fallback — правило внутри
        маркеров не должно на неё ссылаться, иначе импортированный конфиг
        без этой локации не пройдёт проверку nginx."""
        config = generate_full_config(ProfileOptions(), GRPC_RULES, PROXY_RULES)
        self.assertNotIn("@drop", config)

    def test_proxy_locations_use_http11_and_websocket(self):
        config = generate_full_config(
            ProfileOptions(fallback_url="https://1.2.3.4:8445"), GRPC_RULES, [],
        )
        self.assertIn("proxy_http_version 1.1;", config)
        self.assertIn("proxy_set_header Upgrade $http_upgrade;", config)
        self.assertIn("proxy_set_header Connection $connection_upgrade;", config)
        self.assertIn("map $http_upgrade $connection_upgrade {", config)

    def test_domain_placeholder_render(self):
        config = generate_full_config(ProfileOptions(), GRPC_RULES, [])
        self.assertIn(f"server_name {DOMAIN_PLACEHOLDER};", config)
        self.assertIn(f"/etc/letsencrypt/live/{DOMAIN_PLACEHOLDER}/fullchain.pem", config)
        rendered = render_for_server(config, "node1.example.com")
        self.assertNotIn(DOMAIN_PLACEHOLDER, rendered)
        self.assertIn("server_name node1.example.com;", rendered)
        self.assertIn("/etc/letsencrypt/live/node1.example.com/fullchain.pem", rendered)


class ValidationTests(unittest.TestCase):
    def test_trust_everyone_allowed(self):
        # Осознанный выбор оператора: 0.0.0.0/0 допустим (заголовок с IP
        # будет приниматься от любого источника)
        validate_options(ProfileOptions(cdn_enabled=True, cdn_ranges=["0.0.0.0/0"]))
        validate_options(ProfileOptions(cdn_enabled=True, cdn_ranges=["::/0"]))

    def test_bad_cidr_rejected(self):
        with self.assertRaises(OptionsValidationError):
            validate_options(ProfileOptions(cdn_enabled=True, cdn_ranges=["not-a-cidr"]))

    def test_pp_bad_haproxy_ip_rejected(self):
        with self.assertRaises(OptionsValidationError):
            validate_options(ProfileOptions(proxy_protocol_enabled=True, haproxy_ip="not-an-ip"))

    def test_bad_fallback_url_rejected(self):
        with self.assertRaises(OptionsValidationError):
            validate_options(ProfileOptions(fallback_url="ftp://bad"))

    def test_pp_port_cannot_clash_with_public_ports(self):
        with self.assertRaises(OptionsValidationError):
            validate_options(ProfileOptions(
                proxy_protocol_enabled=True, proxy_protocol_port=443, haproxy_ip="10.0.0.1",
            ))

    def test_duplicate_rule_names_rejected(self):
        with self.assertRaises(RuleValidationError):
            validate_rules(
                [GrpcRule(name="a", service_path="x", port=8443)],
                [ProxyRule(name="a", path="/", target_url="https://1.2.3.4")],
            )

    def test_bad_service_path_rejected(self):
        with self.assertRaises(RuleValidationError):
            validate_rules([GrpcRule(name="a", service_path="x y", port=8443)], [])


class ImportHelpersTests(unittest.TestCase):
    USER_CONFIG = """
    server {
        listen 443 ssl;
        server_name example.com;
        ssl_certificate /etc/letsencrypt/live/example.com/fullchain.pem;
    }
    """

    def test_detect_domain(self):
        self.assertEqual(detect_domain(self.USER_CONFIG), "example.com")
        self.assertIsNone(detect_domain("server_name _;"))

    def test_replace_domain_with_placeholder(self):
        result = replace_domain_with_placeholder(self.USER_CONFIG, "example.com")
        self.assertNotIn("example.com", result)
        self.assertIn(f"server_name {DOMAIN_PLACEHOLDER};", result)

    def test_imported_config_without_markers(self):
        self.assertFalse(has_markers(self.USER_CONFIG))
        with self.assertRaises(MissingMarkersError):
            parse_rules_from_config(self.USER_CONFIG)


if __name__ == "__main__":
    unittest.main()
