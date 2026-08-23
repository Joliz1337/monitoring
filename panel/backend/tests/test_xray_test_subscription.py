"""Тесты подписок: определение формата, частичный разбор и защита от SSRF.

Голый unittest, без сети и БД — резолвер подменяется моком.

Главный здесь — SafeTargetTest. URL подписки вводит оператор, а запрос делает
сервер панели, у которого есть доступ к базе, сокету Docker и всем нодам:
пропущенный внутренний адрес превращает удобную кнопку в инструмент разведки
внутренней сети.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import base64
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.xray_test.errors import (  # noqa: E402
    UnknownSubscriptionFormatError,
    UnsafeTargetError,
)
from app.services.xray_test.subscription import (  # noqa: E402
    SubscriptionFormat,
    assert_public_target,
    detect_format,
    parse_links_text,
    parse_subscription,
)

UUID = "11111111-2222-3333-4444-555555555555"
LINKS = "\n".join([
    f"vless://{UUID}@a.example:443?security=tls&sni=a.example#one",
    "trojan://pw@b.example:443#two",
    "hysteria2://pw@c.example:443#three",
])


def _b64(text: str, *, urlsafe: bool = False, strip_padding: bool = False) -> str:
    encoder = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    encoded = encoder(text.encode()).decode()
    return encoded.rstrip("=") if strip_padding else encoded


class FormatDetectionTest(unittest.TestCase):
    def test_plain_links(self):
        self.assertIs(detect_format(LINKS), SubscriptionFormat.PLAIN)

    def test_plain_with_crlf_and_comment(self):
        text = "# my subscription\r\n" + LINKS.replace("\n", "\r\n")
        self.assertIs(detect_format(text), SubscriptionFormat.PLAIN)

    def test_base64_standard_alphabet(self):
        self.assertIs(detect_format(_b64(LINKS)), SubscriptionFormat.BASE64)

    def test_base64_urlsafe_without_padding(self):
        encoded = _b64(LINKS, urlsafe=True, strip_padding=True)
        self.assertIs(detect_format(encoded), SubscriptionFormat.BASE64)

    def test_base64_with_line_breaks(self):
        encoded = _b64(LINKS)
        wrapped = "\n".join(encoded[i:i + 40] for i in range(0, len(encoded), 40))
        self.assertIs(detect_format(wrapped), SubscriptionFormat.BASE64)

    def test_xray_json(self):
        raw = json.dumps({"outbounds": [{"protocol": "vless", "settings": {}}]})
        self.assertIs(detect_format(raw), SubscriptionFormat.XRAY_JSON)

    def test_singbox_json(self):
        raw = json.dumps({"outbounds": [{"type": "vless", "server": "h.io"}], "route": {}})
        self.assertIs(detect_format(raw), SubscriptionFormat.SINGBOX_JSON)

    def test_garbage_rejected(self):
        with self.assertRaises(UnknownSubscriptionFormatError):
            detect_format("совершенно не подписка, просто текст")

    def test_empty_rejected(self):
        with self.assertRaises(UnknownSubscriptionFormatError):
            detect_format("   ")


class ParseSubscriptionTest(unittest.TestCase):
    def test_plain_parsed(self):
        content = parse_subscription(LINKS)
        self.assertEqual(len(content.endpoints), 3)
        self.assertEqual(content.errors, [])
        self.assertEqual(content.links[0].split("#")[-1], "one")

    def test_base64_parsed(self):
        content = parse_subscription(_b64(LINKS))
        self.assertIs(content.format, SubscriptionFormat.BASE64)
        self.assertEqual(len(content.endpoints), 3)

    def test_broken_line_does_not_kill_import(self):
        text = LINKS + "\nvless://broken-without-host\nvless://%s@d.example:443#four" % UUID
        content = parse_subscription(text)

        self.assertEqual(len(content.endpoints), 4)
        self.assertEqual(len(content.errors), 1)
        self.assertEqual(content.errors[0].line, 4)

    def test_error_preview_masks_secrets(self):
        text = f"vless://{UUID}@e.example\n" + LINKS
        content = parse_subscription(text)
        self.assertTrue(content.errors)
        self.assertNotIn(UUID, content.errors[0].preview)

    def test_json_subscription_reports_dropped_sections(self):
        raw = json.dumps({
            "inbounds": [{"listen": "0.0.0.0", "port": 1080, "protocol": "socks"}],
            "outbounds": [{
                "protocol": "vless", "tag": "p",
                "settings": {"vnext": [{"address": "h.io", "port": 443,
                                        "users": [{"id": UUID, "encryption": "none"}]}]},
                "streamSettings": {"network": "tcp", "security": "tls",
                                   "tlsSettings": {"serverName": "h.io"}},
            }],
        })
        content = parse_subscription(raw)
        self.assertEqual(len(content.endpoints), 1)
        self.assertIn("inbounds", content.dropped_sections)

    def test_all_lines_broken_rejected(self):
        with self.assertRaises(UnknownSubscriptionFormatError):
            parse_subscription("vless://\nvless://\n")


class ParseLinksTextTest(unittest.TestCase):
    def test_blank_and_comment_lines_skipped(self):
        endpoints, links, errors = parse_links_text(f"\n\n# note\n{LINKS}\n\n")
        self.assertEqual(len(endpoints), 3)
        self.assertEqual(len(links), 3)
        self.assertEqual(errors, [])


class SafeTargetTest(unittest.IsolatedAsyncioTestCase):
    async def _assert_blocked(self, url: str, addresses=None):
        with mock.patch(
            "app.services.xray_test.subscription._resolve_all",
            new=mock.AsyncMock(return_value=addresses or ["10.0.0.5"]),
        ), mock.patch(
            "app.services.xray_test.subscription.resolve_panel_ip",
            new=mock.AsyncMock(return_value=None),
        ):
            with self.assertRaises(UnsafeTargetError):
                await assert_public_target(url)

    async def test_loopback_blocked(self):
        await self._assert_blocked("http://127.0.0.1/sub", ["127.0.0.1"])

    async def test_cloud_metadata_blocked(self):
        await self._assert_blocked("http://169.254.169.254/latest/meta-data", ["169.254.169.254"])

    async def test_private_range_blocked(self):
        await self._assert_blocked("http://10.1.2.3/sub", ["10.1.2.3"])

    async def test_ipv6_loopback_blocked(self):
        await self._assert_blocked("http://[::1]/sub", ["::1"])

    async def test_ipv6_private_blocked(self):
        await self._assert_blocked("http://sub.example/x", ["fd00::1"])

    async def test_domain_resolving_to_private_blocked(self):
        await self._assert_blocked("https://evil.example/sub", ["10.0.0.7"])

    async def test_mixed_answer_blocked_when_any_private(self):
        """Один приватный адрес среди публичных всё равно закрывает запрос."""
        await self._assert_blocked("https://mixed.example/sub", ["93.184.216.34", "192.168.1.5"])

    async def test_non_http_scheme_blocked(self):
        with self.assertRaises(UnsafeTargetError):
            await assert_public_target("file:///etc/passwd")

    async def test_panel_own_address_blocked(self):
        with mock.patch(
            "app.services.xray_test.subscription._resolve_all",
            new=mock.AsyncMock(return_value=["203.0.113.10"]),
        ), mock.patch(
            "app.services.xray_test.subscription.resolve_panel_ip",
            new=mock.AsyncMock(return_value="203.0.113.10"),
        ):
            with self.assertRaises(UnsafeTargetError):
                await assert_public_target("https://panel.example/sub")

    async def test_public_target_allowed(self):
        with mock.patch(
            "app.services.xray_test.subscription._resolve_all",
            new=mock.AsyncMock(return_value=["93.184.216.34"]),
        ), mock.patch(
            "app.services.xray_test.subscription.resolve_panel_ip",
            new=mock.AsyncMock(return_value=None),
        ):
            await assert_public_target("https://sub.example/link")


if __name__ == "__main__":
    unittest.main()
