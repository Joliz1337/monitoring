"""Tests for the pure parts of app.services.haproxy_manager.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Менеджер целиком разговаривает с systemd и файловой системой, но разбор конфига
и вычисление maxconn — чистые преобразования, и именно они определяют, что
окажется в haproxy.cfg. Ошибка здесь тихая: конфиг применится, а правило
потеряет параметр или соберётся с чужим таргетом.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.haproxy_manager import (  # noqa: E402
    MAXCONN_MAX,
    MAXCONN_MIN,
    HAProxyManager,
)


def make_manager() -> HAProxyManager:
    """Менеджер без побочных эффектов конструктора."""
    return HAProxyManager.__new__(HAProxyManager)


class DomainDetectionTests(unittest.TestCase):
    def test_ipv4_and_ipv6_are_not_domains(self):
        for target in ("10.0.0.1", "127.0.0.1", "::1", "2001:db8::1"):
            with self.subTest(target=target):
                self.assertFalse(HAProxyManager._is_domain(target))

    def test_hostnames_are_domains(self):
        for target in ("example.com", "node-01.internal", "localhost"):
            with self.subTest(target=target):
                self.assertTrue(HAProxyManager._is_domain(target))


class DnsResolverPatchTests(unittest.TestCase):
    """Домен за server-строкой обязан получить resolvers, иначе HAProxy
    зарезолвит его один раз при старте и будет ходить на протухший IP."""

    def test_domain_target_gets_resolver_params(self):
        config = "backend b\n    server srv1 example.com:443 check\n"
        patched = HAProxyManager._patch_dns_resolvers(config)
        self.assertIn(
            "server srv1 example.com:443 resolvers mydns resolve-prefer ipv4 init-addr none check",
            patched,
        )

    def test_ip_target_is_left_alone(self):
        config = "backend b\n    server srv1 10.0.0.1:443 check\n"
        self.assertEqual(HAProxyManager._patch_dns_resolvers(config), config)

    def test_existing_resolvers_are_not_duplicated(self):
        config = "backend b\n    server srv1 example.com:443 resolvers mydns check\n"
        self.assertEqual(HAProxyManager._patch_dns_resolvers(config), config)

    def test_params_land_before_other_options_not_at_line_end(self):
        # send-proxy после resolvers — важен порядок: HAProxy разбирает
        # server-строку позиционно относительно host:port
        config = "backend b\n    server srv1 example.com:443 send-proxy check\n"
        patched = HAProxyManager._patch_dns_resolvers(config)
        line = patched.splitlines()[1]
        self.assertLess(line.index("resolvers"), line.index("send-proxy"))


class ServerLineParsingTests(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()

    def test_defaults_when_no_options_given(self):
        srv = self.manager._parse_server_line("    server srv1 10.0.0.1:8443")
        self.assertEqual((srv.name, srv.address, srv.port), ("srv1", "10.0.0.1", 8443))
        self.assertEqual((srv.weight, srv.fall, srv.rise, srv.inter), (1, 3, 2, "5s"))
        self.assertFalse(srv.check)
        self.assertFalse(srv.backup)
        self.assertFalse(srv.disabled)
        self.assertIsNone(srv.maxconn)

    def test_full_option_set(self):
        srv = self.manager._parse_server_line(
            "    server srv2 node.example.com:443 weight 5 maxconn 200 check "
            "inter 3s fall 2 rise 4 backup slowstart 30s disabled"
        )
        self.assertEqual(srv.weight, 5)
        self.assertEqual(srv.maxconn, 200)
        self.assertTrue(srv.check)
        self.assertEqual((srv.inter, srv.fall, srv.rise), ("3s", 2, 4))
        self.assertTrue(srv.backup)
        self.assertEqual(srv.slowstart, "30s")
        self.assertTrue(srv.disabled)

    def test_send_proxy_v2_does_not_also_set_send_proxy(self):
        # Обе строки — подстроки друг друга, поэтому наивная проверка "in"
        # выставила бы оба флага и HAProxy получил бы два взаимоисключающих
        v2 = self.manager._parse_server_line("    server s 10.0.0.1:443 send-proxy-v2")
        self.assertTrue(v2.send_proxy_v2)
        self.assertFalse(v2.send_proxy)

        v1 = self.manager._parse_server_line("    server s 10.0.0.1:443 send-proxy")
        self.assertTrue(v1.send_proxy)
        self.assertFalse(v1.send_proxy_v2)

    def test_non_server_line_yields_none(self):
        self.assertIsNone(self.manager._parse_server_line("    balance roundrobin"))


class BalancerOptionsParsingTests(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()

    def test_algorithm_with_parameter(self):
        opts = self.manager._parse_balancer_options("    balance hdr(Host)\n")
        self.assertEqual(opts.algorithm, "hdr")
        self.assertEqual(opts.algorithm_param, "Host")

    def test_algorithm_without_parameter(self):
        opts = self.manager._parse_balancer_options("    balance roundrobin\n")
        self.assertEqual(opts.algorithm, "roundrobin")
        self.assertIsNone(opts.algorithm_param)

    def test_http_health_check_wins_over_tcp_check(self):
        block = (
            "    option tcp-check\n"
            "    option httpchk GET /health\n"
            "    http-check expect status 200\n"
        )
        opts = self.manager._parse_balancer_options(block)
        self.assertEqual(opts.health_check_type, "httpchk")
        self.assertEqual(opts.httpchk_method, "GET")
        self.assertEqual(opts.httpchk_uri, "/health")
        self.assertEqual(opts.httpchk_expect, "status 200")

    def test_cookie_stickiness(self):
        opts = self.manager._parse_balancer_options("    cookie SRVID insert indirect nocache\n")
        self.assertEqual(opts.sticky_type, "cookie")
        self.assertEqual(opts.cookie_name, "SRVID")
        self.assertEqual(opts.cookie_options, "insert indirect nocache")

    def test_empty_block_leaves_defaults(self):
        opts = self.manager._parse_balancer_options("")
        self.assertIsNone(opts.algorithm_param)


SINGLE_RULE_CONFIG = """\
global
    maxconn 1000

frontend tcp_relay
    bind *:8443
    default_backend backend_tcp_relay

backend backend_tcp_relay
    server srv1 10.0.0.5:443 send-proxy

frontend https_web
    bind *:443 ssl crt /etc/letsencrypt/live/example.com/combined.pem accept-proxy
    default_backend backend_https_web

backend backend_https_web
    balance roundrobin
    server web1 10.0.0.6:8080 check
    server web2 10.0.0.7:8080 check backup
"""


class RuleParsingTests(unittest.TestCase):
    """parse_rules читает конфиг с диска — подменяем только чтение."""

    def parse(self, content: str):
        manager = make_manager()
        with mock.patch.object(HAProxyManager, "_read_config", return_value=content):
            return manager.parse_rules()

    def test_single_target_and_balancer_rules_are_both_recognized(self):
        rules = {r.name: r for r in self.parse(SINGLE_RULE_CONFIG)}
        self.assertEqual(set(rules), {"relay", "web"})

        relay = rules["relay"]
        self.assertEqual(relay.rule_type, "tcp")
        self.assertEqual(relay.listen_port, 8443)
        self.assertEqual((relay.target_ip, relay.target_port), ("10.0.0.5", 443))
        self.assertTrue(relay.send_proxy)
        self.assertFalse(relay.is_balancer)
        self.assertFalse(relay.accept_proxy)

        web = rules["web"]
        self.assertEqual(web.rule_type, "https")
        self.assertEqual(web.listen_port, 443)
        self.assertEqual(web.cert_domain, "example.com")
        self.assertTrue(web.accept_proxy)
        self.assertTrue(web.is_balancer)
        self.assertEqual([s.name for s in web.servers], ["web1", "web2"])
        self.assertTrue(web.servers[1].backup)

    def test_backend_without_frontend_is_ignored(self):
        content = "backend backend_tcp_orphan\n    server s 10.0.0.1:443\n"
        self.assertEqual(self.parse(content), [])

    def test_empty_config_yields_no_rules(self):
        self.assertEqual(self.parse(""), [])


class CertDomainTests(unittest.TestCase):
    def test_parent_domain_extraction(self):
        self.assertEqual(HAProxyManager._extract_parent_domain("sub.example.com"), "example.com")
        self.assertEqual(HAProxyManager._extract_parent_domain("a.b.example.com"), "b.example.com")
        self.assertEqual(HAProxyManager._extract_parent_domain("example.com"), "example.com")

    def test_wildcard_flag_selects_parent(self):
        manager = make_manager()
        self.assertEqual(manager._resolve_cert_domain("sub.example.com", True), "example.com")
        self.assertEqual(manager._resolve_cert_domain("sub.example.com", False), "sub.example.com")


class MaxconnTests(unittest.TestCase):
    """maxconn выше реального лимита дескрипторов не даёт HAProxy стартовать
    (strict-limits с 2.5), а ниже минимума — бессмысленен."""

    def compute(self, ram_mb: int, nofile: int) -> int:
        manager = make_manager()
        fake_mem = mock.Mock(total=ram_mb * 1024 * 1024)
        with mock.patch("app.services.haproxy_manager.psutil.virtual_memory", return_value=fake_mem), \
             mock.patch.object(HAProxyManager, "_read_nofile_limit", return_value=nofile):
            return manager._compute_maxconn()

    def test_scales_with_ram_when_descriptors_allow(self):
        self.assertEqual(self.compute(ram_mb=16384, nofile=1_048_576), 163840)

    def test_descriptor_limit_caps_the_result(self):
        # 64 ГБ дали бы 655360, но лимит юнита 65536 разрешает лишь (65536-1024)//3
        self.assertEqual(self.compute(ram_mb=65536, nofile=65536), 21504)

    def test_never_below_floor_on_tiny_hosts(self):
        self.assertEqual(self.compute(ram_mb=512, nofile=65536), MAXCONN_MIN)

    def test_never_above_ceiling(self):
        self.assertEqual(self.compute(ram_mb=1_000_000, nofile=10_000_000), MAXCONN_MAX)


class GlobalMaxconnTests(unittest.TestCase):
    def patch_config(self, content: str, computed: int = 40000) -> str:
        manager = make_manager()
        with mock.patch.object(HAProxyManager, "_compute_maxconn", return_value=computed):
            return manager._ensure_global_maxconn(content)

    def test_inserted_when_absent(self):
        result = self.patch_config("global\n    no log\n\ndefaults\n    mode tcp\n")
        self.assertIn("    maxconn 40000\n", result)
        self.assertLess(result.index("maxconn 40000"), result.index("no log"))

    def test_explicit_value_from_profile_is_left_alone(self):
        content = "global\n    maxconn 12345\n    no log\n"
        self.assertEqual(self.patch_config(content), content)

    def test_config_without_global_section_is_untouched(self):
        content = "defaults\n    mode tcp\n"
        self.assertEqual(self.patch_config(content), content)

    def test_idempotent(self):
        once = self.patch_config("global\n    no log\n")
        self.assertEqual(self.patch_config(once), once)


class NofileLimitTests(unittest.TestCase):
    def test_reads_value_from_facts_file(self):
        manager = make_manager()
        facts = "MEM_MB=16384\nNOFILE_LIMIT=262144\nDOCKER_NOFILE=1048576\n"
        with mock.patch("pathlib.Path.read_text", return_value=facts):
            self.assertEqual(manager._read_nofile_limit(), 262144)

    def test_falls_back_when_no_facts_file(self):
        manager = make_manager()
        with mock.patch("pathlib.Path.read_text", side_effect=OSError):
            self.assertEqual(manager._read_nofile_limit(), 1048576)


if __name__ == "__main__":
    unittest.main()
