"""Тесты маскирования секретов и выгрузки результатов.

Голый unittest, без сети и БД.

Секреты утекают тремя путями: ссылка в логе задачи, вывод ядра (xray при ошибке
печатает куски конфига вместе с UUID) и ответы API. Закрыты должны быть все
три, иначе рабочий ключ уедет в лог контейнера.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.xray_test.export import (  # noqa: E402
    as_csv,
    as_subscription,
    working_links,
)
from app.services.xray_test.errors import LinkParseError  # noqa: E402
from app.services.xray_test.models import Protocol, ProxyEndpoint  # noqa: E402
from app.services.xray_test.parsers import json_config, parse_link  # noqa: E402
from app.services.xray_test.sanitize import (  # noqa: E402
    mask_secret,
    sanitize_link,
    sanitize_output,
)

UUID = "11111111-2222-3333-4444-555555555555"
PBK = "p0by5Raay70X-2hllYoFctRFOd6ONT7y9RPWz2KAHUU"


class MaskSecretTest(unittest.TestCase):
    def test_long_value_keeps_edges(self):
        masked = mask_secret("abcdefghijklmnop")
        self.assertTrue(masked.startswith("abcd"))
        self.assertTrue(masked.endswith("mnop"))
        self.assertNotIn("efghijkl", masked)

    def test_short_value_hidden_entirely(self):
        self.assertNotIn("pw", mask_secret("pw"))

    def test_empty_value(self):
        self.assertEqual(mask_secret(""), "")


class SanitizeLinkTest(unittest.TestCase):
    def test_uuid_masked_but_host_visible(self):
        link = f"vless://{UUID}@server.example:443?security=tls&sni=a.com#node"
        result = sanitize_link(link)

        self.assertNotIn(UUID, result)
        self.assertIn("server.example", result)
        self.assertIn("443", result)
        self.assertIn("sni=a.com", result)

    def test_reality_key_masked(self):
        link = f"vless://{UUID}@h.io:443?security=reality&pbk={PBK}&sid=00aabb#n"
        result = sanitize_link(link)

        self.assertNotIn(PBK, result)
        self.assertIn("security=reality", result)

    def test_trojan_password_masked(self):
        result = sanitize_link("trojan://SuperSecretPassword@h.io:443#n")
        self.assertNotIn("SuperSecretPassword", result)
        self.assertIn("h.io", result)

    def test_vmess_payload_masked(self):
        payload = base64.b64encode(b'{"add":"h.io","id":"secret-uuid-value"}').decode()
        result = sanitize_link(f"vmess://{payload}")
        self.assertNotIn("secret-uuid-value", result)

    def test_broken_link_does_not_raise(self):
        self.assertIsInstance(sanitize_link("vless://[unclosed"), str)


class SanitizeOutputTest(unittest.TestCase):
    def test_uuid_in_core_log_masked(self):
        line = f'failed to parse: user id {UUID} rejected'
        result = sanitize_output(line)

        self.assertNotIn(UUID, result)
        self.assertIn("failed to parse", result)

    def test_json_secret_fields_masked(self):
        line = '{"id": "%s", "password": "hunter2000pass", "address": "h.io"}' % UUID
        result = sanitize_output(line)

        self.assertNotIn(UUID, result)
        self.assertNotIn("hunter2000pass", result)
        self.assertIn("h.io", result)

    def test_long_token_masked(self):
        result = sanitize_output(f"invalid public key: {PBK}")
        self.assertNotIn(PBK, result)
        self.assertIn("invalid public key", result)

    def test_plain_message_survives(self):
        self.assertIn("connection refused", sanitize_output("dial tcp: connection refused"))


class ExportTest(unittest.TestCase):
    def _results(self):
        return [
            {"verdict": "ok", "link": "vless://a@h.io:443#a", "remark": "a", "rtt_ms": 120},
            {"verdict": "degraded", "link": "trojan://b@h.io:443#b", "remark": "b", "rtt_ms": 1900},
            {"verdict": "fail", "link": "vless://c@h.io:443#c", "remark": "c", "rtt_ms": None},
            {"verdict": "ok", "link": "vless://a@h.io:443#a", "remark": "a", "rtt_ms": 130},
        ]

    def test_working_links_include_degraded(self):
        links = working_links(self._results())
        self.assertEqual(len(links), 2)
        self.assertNotIn("vless://c@h.io:443#c", links)

    def test_working_links_strict(self):
        links = working_links(self._results(), include_degraded=False)
        self.assertEqual(links, ["vless://a@h.io:443#a"])

    def test_duplicates_removed(self):
        self.assertEqual(len(working_links(self._results())), 2)

    def test_subscription_is_decodable(self):
        encoded = as_subscription(["vless://a@h.io:443#a", "trojan://b@h.io:443#b"])
        decoded = base64.b64decode(encoded).decode()
        self.assertIn("vless://a@h.io:443#a", decoded)
        self.assertEqual(len(decoded.splitlines()), 2)

    def test_csv_has_header_and_rows(self):
        report = as_csv(self._results())
        lines = report.strip().splitlines()

        self.assertEqual(len(lines), 5)
        self.assertIn("Протокол", lines[0])
        self.assertIn("ok", lines[1])


class EndpointFieldValidationTest(unittest.TestCase):
    """Адрес, SNI и имя из ссылки попадают в задание ноды: управляющие символы недопустимы."""

    def test_control_chars_in_address_rejected(self):
        for char in ("\n", "\t", "\r", "\x00", "\x7f", " "):
            with self.subTest(char=char):
                with self.assertRaises(LinkParseError):
                    ProxyEndpoint(Protocol.VLESS, f"host{char}x.io", 443)

    def test_control_chars_in_sni_rejected(self):
        for encoded in ("%0A", "%09"):
            with self.subTest(encoded=encoded):
                with self.assertRaises(LinkParseError):
                    parse_link(f"vless://{UUID}@h.io:443?security=tls&sni=a{encoded}b.io")

    def test_control_chars_in_transport_host_rejected(self):
        with self.assertRaises(LinkParseError):
            parse_link(f"vless://{UUID}@h.io:443?type=ws&host=a%0Ab.io")

    def test_control_chars_in_remark_rejected(self):
        with self.assertRaises(LinkParseError):
            parse_link(f"vless://{UUID}@h.io:443?security=tls#na%0Ame")

    def test_overlong_host_rejected(self):
        with self.assertRaises(LinkParseError):
            parse_link(f"vless://{UUID}@{'a' * 254}:443?security=tls")

    def test_punycode_and_underscore_hosts_pass(self):
        endpoint = parse_link(f"vless://{UUID}@xn--80ak6aa92e.xn--p1ai:443?security=tls&sni=my_host-1.example.io")
        self.assertEqual(endpoint.address, "xn--80ak6aa92e.xn--p1ai")
        self.assertEqual(endpoint.tls.sni, "my_host-1.example.io")

    def test_bracketed_ipv6_passes(self):
        endpoint = parse_link(f"vless://{UUID}@[2001:db8::1]:443?security=tls")
        self.assertEqual(endpoint.port, 443)

    def test_unicode_hostname_and_remark_pass(self):
        endpoint = parse_link(f"vless://{UUID}@пример.рф:443?security=tls#Германия 1")
        self.assertEqual(endpoint.address, "пример.рф")

    def test_json_import_skips_outbound_with_bad_address(self):
        def outbound(address: str) -> dict:
            return {
                "protocol": "vless",
                "settings": {"vnext": [{"address": address, "port": 443, "users": [{"id": UUID}]}]},
            }

        text = json.dumps({"outbounds": [outbound("bad\nhost.io"), outbound("good.io")]})
        endpoints, _ = json_config.parse_config(text)
        self.assertEqual([e.address for e in endpoints], ["good.io"])


if __name__ == "__main__":
    unittest.main()
