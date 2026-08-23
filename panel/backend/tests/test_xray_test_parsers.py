"""Тесты разбора прокси-ссылок (app/services/xray_test/parsers).

Голый unittest, без сети и БД.

Ссылки в подписках приходят из десятка разных клиентов, и каждый пишет их
чуть по-своему. Таблица ниже — снимок того, что реально встречается: обе формы
vmess, три формы ss, IPv6 в скобках, percent-encoded пароли. Если парсер
сломается на любой из них, оператор увидит «не работает» на живом ключе.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.xray_test.errors import (  # noqa: E402
    LinkParseError,
    UnsupportedProtocolError,
)
from app.services.xray_test.models import (  # noqa: E402
    Protocol,
    Security,
    Transport,
)
from app.services.xray_test.parsers import parse_link  # noqa: E402


class VlessTest(unittest.TestCase):
    def test_reality_vision(self):
        link = (
            "vless://11111111-2222-3333-4444-555555555555@example.com:443"
            "?security=reality&sni=www.microsoft.com&fp=chrome"
            "&pbk=abcdefghijklmnopqrstuvwxyz0123456789ABCDEFX"
            "&sid=00aabb&spx=%2F&flow=xtls-rprx-vision&type=tcp#Node%20One"
        )
        ep = parse_link(link)

        self.assertEqual(ep.protocol, Protocol.VLESS)
        self.assertEqual(ep.address, "example.com")
        self.assertEqual(ep.port, 443)
        self.assertEqual(ep.uuid, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(ep.remark, "Node One")
        self.assertEqual(ep.flow, "xtls-rprx-vision")
        self.assertEqual(ep.tls.security, Security.REALITY)
        self.assertEqual(ep.tls.sni, "www.microsoft.com")
        self.assertEqual(ep.tls.fingerprint, "chrome")
        self.assertEqual(ep.tls.reality_short_id, "00aabb")
        self.assertEqual(ep.tls.reality_spider_x, "/")
        self.assertEqual(ep.transport.kind, Transport.TCP)

    def test_ws_tls_percent_encoded_path(self):
        link = (
            "vless://uuid-1@1.2.3.4:8443?type=ws&security=tls"
            "&path=%2Fsome%2Fpath%3Fed%3D2048&host=cdn.example.org&alpn=h2%2Chttp%2F1.1#WS"
        )
        ep = parse_link(link)

        self.assertEqual(ep.transport.kind, Transport.WS)
        self.assertEqual(ep.transport.path, "/some/path?ed=2048")
        self.assertEqual(ep.transport.host, "cdn.example.org")
        self.assertEqual(ep.tls.alpn, ("h2", "http/1.1"))

    def test_grpc(self):
        ep = parse_link("vless://u@h.io:443?type=grpc&serviceName=my%2Fsvc&mode=gun&security=tls#g")
        self.assertEqual(ep.transport.kind, Transport.GRPC)
        self.assertEqual(ep.transport.service_name, "my/svc")
        self.assertEqual(ep.transport.mode, "gun")

    def test_xhttp(self):
        ep = parse_link("vless://u@h.io:443?type=xhttp&path=%2Fx&mode=stream-up&security=tls#x")
        self.assertEqual(ep.transport.kind, Transport.XHTTP)
        self.assertEqual(ep.transport.mode, "stream-up")

    def test_splithttp_is_xhttp(self):
        ep = parse_link("vless://u@h.io:443?type=splithttp&security=tls#x")
        self.assertEqual(ep.transport.kind, Transport.XHTTP)

    def test_ipv6_host(self):
        ep = parse_link("vless://u@[2001:db8::1]:443?security=tls#v6")
        self.assertEqual(ep.address, "2001:db8::1")
        self.assertEqual(ep.port, 443)

    def test_reality_detected_without_security_param(self):
        ep = parse_link("vless://u@h.io:443?pbk=KEY123&sni=a.com#r")
        self.assertEqual(ep.tls.security, Security.REALITY)

    def test_unknown_query_goes_to_extra(self):
        ep = parse_link("vless://u@h.io:443?security=tls&customflag=7#e")
        self.assertIn(("customflag", "7"), ep.extra)

    def test_unknown_transport_rejected(self):
        with self.assertRaises(LinkParseError):
            parse_link("vless://u@h.io:443?type=carrier-pigeon#x")


class TrojanTest(unittest.TestCase):
    def test_ws(self):
        ep = parse_link("trojan://p%40ss@h.io:443?type=ws&path=%2Ftr&sni=h.io#T")
        self.assertEqual(ep.protocol, Protocol.TROJAN)
        self.assertEqual(ep.password, "p@ss")
        self.assertEqual(ep.transport.kind, Transport.WS)

    def test_tls_implied(self):
        ep = parse_link("trojan://pass@h.io:443#T")
        self.assertEqual(ep.tls.security, Security.TLS)


class VmessTest(unittest.TestCase):
    def test_legacy_base64_json(self):
        payload = {
            "v": "2", "ps": "Legacy", "add": "1.2.3.4", "port": "443",
            "id": "aaaa-bbbb", "aid": "0", "scy": "auto", "net": "ws",
            "type": "none", "host": "cdn.io", "path": "/ws", "tls": "tls", "sni": "cdn.io",
        }
        raw = base64.b64encode(json.dumps(payload).encode()).decode()
        ep = parse_link(f"vmess://{raw}")

        self.assertEqual(ep.protocol, Protocol.VMESS)
        self.assertEqual(ep.address, "1.2.3.4")
        self.assertEqual(ep.port, 443)
        self.assertEqual(ep.remark, "Legacy")
        self.assertEqual(ep.transport.kind, Transport.WS)
        self.assertEqual(ep.transport.path, "/ws")
        self.assertEqual(ep.tls.security, Security.TLS)

    def test_legacy_base64_without_padding(self):
        payload = {"add": "h.io", "port": 443, "id": "x", "net": "tcp"}
        raw = base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        ep = parse_link(f"vmess://{raw}")
        self.assertEqual(ep.address, "h.io")

    def test_uri_form(self):
        ep = parse_link("vmess://uuid-9@h.io:443?type=tcp&security=tls#Modern")
        self.assertEqual(ep.uuid, "uuid-9")
        self.assertEqual(ep.remark, "Modern")

    def test_grpc_service_name_from_path(self):
        payload = {"add": "h.io", "port": 443, "id": "x", "net": "grpc", "path": "svc"}
        raw = base64.b64encode(json.dumps(payload).encode()).decode()
        ep = parse_link(f"vmess://{raw}")
        self.assertEqual(ep.transport.service_name, "svc")


class ShadowsocksTest(unittest.TestCase):
    def test_sip002(self):
        userinfo = base64.b64encode(b"aes-256-gcm:secretpass").decode()
        ep = parse_link(f"ss://{userinfo}@1.2.3.4:8388#SS")

        self.assertEqual(ep.protocol, Protocol.SHADOWSOCKS)
        self.assertEqual(ep.method, "aes-256-gcm")
        self.assertEqual(ep.password, "secretpass")
        self.assertEqual(ep.port, 8388)

    def test_legacy_whole_body_base64(self):
        body = base64.b64encode(b"chacha20-ietf-poly1305:pw@5.6.7.8:1080").decode()
        ep = parse_link(f"ss://{body}#Old")
        self.assertEqual(ep.address, "5.6.7.8")
        self.assertEqual(ep.method, "chacha20-ietf-poly1305")

    def test_ss2022_plain_userinfo(self):
        link = "ss://2022-blake3-aes-128-gcm:AAABBBCCC%3D@h.io:443#New"
        ep = parse_link(link)
        self.assertEqual(ep.method, "2022-blake3-aes-128-gcm")
        self.assertEqual(ep.password, "AAABBBCCC=")

    def test_plugin_kept_in_extra(self):
        userinfo = base64.b64encode(b"aes-128-gcm:p").decode()
        ep = parse_link(f"ss://{userinfo}@h.io:443?plugin=v2ray-plugin%3Btls#P")
        self.assertTrue(any(key == "plugin" for key, _ in ep.extra))


class QuicTest(unittest.TestCase):
    def test_hysteria2_with_obfs(self):
        link = "hysteria2://pw@h.io:443?sni=a.com&obfs=salamander&obfs-password=zzz&insecure=1#H"
        ep = parse_link(link)

        self.assertEqual(ep.protocol, Protocol.HYSTERIA2)
        self.assertEqual(ep.password, "pw")
        self.assertEqual(ep.obfs, ("salamander", "zzz"))
        self.assertTrue(ep.tls.allow_insecure)
        self.assertTrue(ep.is_udp_protocol)

    def test_hy2_alias(self):
        ep = parse_link("hy2://pw@h.io:443#H")
        self.assertEqual(ep.protocol, Protocol.HYSTERIA2)

    def test_tuic(self):
        ep = parse_link("tuic://uuid-1:pass@h.io:443?alpn=h3&congestion_control=bbr#T")
        self.assertEqual(ep.protocol, Protocol.TUIC)
        self.assertEqual(ep.uuid, "uuid-1")
        self.assertEqual(ep.password, "pass")
        self.assertEqual(ep.tls.alpn, ("h3",))


class SimpleProtocolTest(unittest.TestCase):
    def test_anytls(self):
        ep = parse_link("anytls://pw@h.io:443?sni=a.com#A")
        self.assertEqual(ep.protocol, Protocol.ANYTLS)
        self.assertEqual(ep.tls.security, Security.TLS)

    def test_socks_without_credentials(self):
        ep = parse_link("socks://1.2.3.4:1080#S")
        self.assertEqual(ep.protocol, Protocol.SOCKS)
        self.assertIsNone(ep.uuid)
        self.assertEqual(ep.port, 1080)

    def test_socks_with_credentials(self):
        ep = parse_link("socks5://user:pw@1.2.3.4:1080#S")
        self.assertEqual(ep.uuid, "user")
        self.assertEqual(ep.password, "pw")


class RejectionTest(unittest.TestCase):
    def test_not_a_link(self):
        with self.assertRaises(LinkParseError):
            parse_link("just some text")

    def test_unsupported_scheme(self):
        with self.assertRaises(UnsupportedProtocolError):
            parse_link("wireguard://key@h.io:51820")

    def test_http_is_not_treated_as_proxy(self):
        with self.assertRaises(UnsupportedProtocolError):
            parse_link("https://sub.example.com/link/abc")

    def test_missing_port(self):
        with self.assertRaises(LinkParseError):
            parse_link("vless://u@h.io#x")

    def test_port_out_of_range(self):
        with self.assertRaises(LinkParseError):
            parse_link("vless://u@h.io:70000#x")

    def test_unclosed_ipv6_bracket(self):
        with self.assertRaises(LinkParseError):
            parse_link("vless://u@[2001:db8::1:443#x")


if __name__ == "__main__":
    unittest.main()
