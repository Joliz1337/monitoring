"""Проверка живости бэкенда: генерация, разбор обратно и валидация.

Обычный tcp-check доказывает только то, что порт принял SYN. Проверка
сайта-маскировки идёт до конца цепочки: TLS-рукопожатие с SNI сервера, HTTP-
запрос и внятный ответ — так ловится задушенный или мёртвый xray за живым портом.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.haproxy_config import (  # noqa: E402
    BackendServer,
    BalancerOptions,
    HAProxyConfigGenerator,
    HAProxyRule,
)


def tls_check(sni: str = "www.microsoft.com") -> BalancerOptions:
    return BalancerOptions(health_check_type="tls-check", check_sni=sni)


def simple_rule(**kwargs) -> HAProxyRule:
    defaults = dict(name="vless", rule_type="tcp", listen_port=443,
                    target_ip="10.0.0.5", target_port=8443)
    defaults.update(kwargs)
    return HAProxyRule(**defaults)


def balancer_rule(**kwargs) -> HAProxyRule:
    defaults = dict(
        name="pool", rule_type="tcp", listen_port=443,
        target_ip="", target_port=0, is_balancer=True,
        servers=[BackendServer(name="srv1", address="10.0.0.5", port=8443)],
        balancer_options=tls_check(),
    )
    defaults.update(kwargs)
    return HAProxyRule(**defaults)


class SimpleRuleTest(unittest.TestCase):
    def setUp(self):
        self.gen = HAProxyConfigGenerator()

    def test_default_stays_tcp_check(self):
        block = self.gen.generate_rule_block(simple_rule())
        self.assertIn("    option tcp-check\n", block)
        self.assertNotIn("tcp-check connect", block)
        self.assertIn("check inter 5s fall 3 rise 2", block)

    def test_tls_check_probes_masking_site(self):
        block = self.gen.generate_rule_block(simple_rule(balancer_options=tls_check()))
        self.assertIn("    option tcp-check\n", block)
        self.assertIn("tcp-check connect default ssl sni www.microsoft.com", block)
        self.assertIn("tcp-check send GET\\ /\\ HTTP/1.1\\r\\n", block)
        self.assertIn("tcp-check send Host:\\ www.microsoft.com\\r\\n", block)
        self.assertIn("tcp-check expect rstring ^HTTP/1\\..\\ [234][0-9][0-9]", block)

    def test_tls_check_disables_certificate_verification(self):
        """Без verify none HAProxy не стартует: CA-файла для проверки нет."""
        block = self.gen.generate_rule_block(simple_rule(balancer_options=tls_check()))
        self.assertIn(" verify none check inter", block)

    def test_tls_check_uses_slower_interval(self):
        """Каждая проверка — рукопожатие плюс запрос ядра на сайт-маскировку."""
        block = self.gen.generate_rule_block(simple_rule(balancer_options=tls_check()))
        self.assertIn("check inter 30s fall 3 rise 2", block)
        self.assertNotIn("inter 5s", block)

    def test_proxy_protocol_survives(self):
        block = self.gen.generate_rule_block(
            simple_rule(send_proxy=True, balancer_options=tls_check()))
        self.assertIn("send-proxy-v2 verify none check", block)

    def test_options_without_check_type_fall_back_to_tcp(self):
        block = self.gen.generate_rule_block(
            simple_rule(balancer_options=BalancerOptions()))
        self.assertIn("    option tcp-check\n", block)
        self.assertNotIn("tcp-check connect", block)


class BalancerTest(unittest.TestCase):
    def setUp(self):
        self.gen = HAProxyConfigGenerator()

    def test_tls_check_applies_to_pool(self):
        block = self.gen.generate_rule_block(balancer_rule())
        self.assertIn("tcp-check connect default ssl sni www.microsoft.com", block)
        self.assertIn("verify none", block)

    def test_tcp_check_pool_unchanged(self):
        block = self.gen.generate_rule_block(
            balancer_rule(balancer_options=BalancerOptions(health_check_type="tcp-check")))
        self.assertIn("    option tcp-check\n", block)
        self.assertNotIn("verify none", block)

    def test_httpchk_pool_unchanged(self):
        opts = BalancerOptions(health_check_type="httpchk", httpchk_method="GET",
                               httpchk_uri="/health", httpchk_expect="status 200")
        block = self.gen.generate_rule_block(balancer_rule(balancer_options=opts))
        self.assertIn("option httpchk GET /health", block)
        self.assertIn("http-check expect status 200", block)


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.gen = HAProxyConfigGenerator()

    def test_simple_rule_round_trip(self):
        config = self.gen.generate_full_config([simple_rule(balancer_options=tls_check("dl.google.com"))])
        parsed = self.gen.parse_rules_from_config(config)
        self.assertEqual(len(parsed), 1)
        opts = parsed[0].balancer_options
        self.assertIsNotNone(opts)
        self.assertEqual(opts.health_check_type, "tls-check")
        self.assertEqual(opts.check_sni, "dl.google.com")
        self.assertFalse(parsed[0].send_proxy)

    def test_plain_simple_rule_keeps_no_options(self):
        """Старые профили не должны обрасти пустым объектом настроек."""
        config = self.gen.generate_full_config([simple_rule()])
        parsed = self.gen.parse_rules_from_config(config)
        self.assertIsNone(parsed[0].balancer_options)

    def test_balancer_round_trip(self):
        config = self.gen.generate_full_config([balancer_rule()])
        parsed = self.gen.parse_rules_from_config(config)
        opts = parsed[0].balancer_options
        self.assertEqual(opts.health_check_type, "tls-check")
        self.assertEqual(opts.check_sni, "www.microsoft.com")
        self.assertEqual(len(parsed[0].servers), 1)
        self.assertTrue(parsed[0].servers[0].check)

    def test_proxy_protocol_not_confused_with_check(self):
        config = self.gen.generate_full_config(
            [simple_rule(send_proxy=True, balancer_options=tls_check())])
        parsed = self.gen.parse_rules_from_config(config)
        self.assertTrue(parsed[0].send_proxy)


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.gen = HAProxyConfigGenerator()

    def test_sni_required(self):
        rule = simple_rule(balancer_options=BalancerOptions(health_check_type="tls-check"))
        ok, msg = self.gen.validate_rule(rule)
        self.assertFalse(ok)
        self.assertIn("SNI", msg)

    def test_injection_rejected(self):
        for bad in ("example.com\n    tcp-request content reject",
                    "example.com stats socket /tmp/x",
                    "-example.com", "example..com", "example.com/"):
            rule = simple_rule(balancer_options=tls_check(bad))
            ok, _ = self.gen.validate_rule(rule)
            self.assertFalse(ok, f"принят опасный SNI: {bad!r}")

    def test_valid_sni_accepted(self):
        for good in ("www.microsoft.com", "dl.google.com", "cdn-1.example.co.uk", "localhost"):
            ok, msg = self.gen.validate_rule(simple_rule(balancer_options=tls_check(good)))
            self.assertTrue(ok, f"{good}: {msg}")

    def test_unknown_check_type_rejected(self):
        rule = simple_rule(balancer_options=BalancerOptions(health_check_type="magic"))
        ok, _ = self.gen.validate_rule(rule)
        self.assertFalse(ok)

    def test_tls_check_not_allowed_for_https(self):
        rule = simple_rule(rule_type="https", cert_domain="example.com",
                           balancer_options=tls_check())
        ok, msg = self.gen.validate_rule(rule)
        self.assertFalse(ok)
        self.assertIn("TCP", msg)


if __name__ == "__main__":
    unittest.main()
