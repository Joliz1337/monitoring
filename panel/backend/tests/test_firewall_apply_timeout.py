"""Таймаут nginx ноды на /api/firewall/ не короче ожидания панели.

Apply профиля на ноде идёт по одному ufw-правилу и сериализуется локом; общий
location / (30 с) обрывал второй запрос подряд, хотя нода доводила его до конца.
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firewall_profile_sync import APPLY_TIMEOUT_SECONDS  # noqa: E402

NODE_NGINX_TEMPLATE = (
    Path(__file__).resolve().parents[3] / "node" / "nginx" / "templates" / "api.conf.template"
)
FIREWALL_LOCATION = "/api/firewall/"


def location_block(config: str, path: str) -> str | None:
    header = re.search(rf"location\s+{re.escape(path)}\s*\{{", config)
    if header is None:
        return None
    depth, start = 1, header.end()
    for index in range(start, len(config)):
        depth += {"{": 1, "}": -1}.get(config[index], 0)
        if depth == 0:
            return config[start:index]
    return None


def timeout_seconds(block: str, directive: str) -> float | None:
    match = re.search(rf"{directive}\s+(\d+)(s|m)?;", block)
    if match is None:
        return None
    value, unit = int(match.group(1)), match.group(2)
    return value * 60 if unit == "m" else value


class FirewallApplyTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.block = location_block(NODE_NGINX_TEMPLATE.read_text(encoding="utf-8"), FIREWALL_LOCATION)

    def test_firewall_has_own_location(self):
        self.assertIsNotNone(self.block, f"в шаблоне nginx ноды нет location {FIREWALL_LOCATION}")

    def test_read_timeout_covers_panel_apply_timeout(self):
        self.assertIsNotNone(self.block)
        read = timeout_seconds(self.block, "proxy_read_timeout")
        self.assertIsNotNone(read, "у location /api/firewall/ нет proxy_read_timeout")
        self.assertGreaterEqual(read, APPLY_TIMEOUT_SECONDS)

    def test_send_timeout_covers_panel_apply_timeout(self):
        self.assertIsNotNone(self.block)
        send = timeout_seconds(self.block, "proxy_send_timeout")
        self.assertIsNotNone(send, "у location /api/firewall/ нет proxy_send_timeout")
        self.assertGreaterEqual(send, APPLY_TIMEOUT_SECONDS)


class LocationParserTests(unittest.TestCase):
    CONFIG = (
        "location / { proxy_read_timeout 30s; }\n"
        "location /api/firewall/ {\n    if ($x) { return 418; }\n    proxy_read_timeout 2m;\n}\n"
    )

    def test_nested_braces_do_not_end_block_early(self):
        block = location_block(self.CONFIG, FIREWALL_LOCATION)
        self.assertIn("proxy_read_timeout 2m;", block)

    def test_minutes_are_converted(self):
        block = location_block(self.CONFIG, FIREWALL_LOCATION)
        self.assertEqual(timeout_seconds(block, "proxy_read_timeout"), 120)

    def test_missing_location_is_none(self):
        self.assertIsNone(location_block(self.CONFIG, "/api/ssh/"))


if __name__ == "__main__":
    unittest.main()
