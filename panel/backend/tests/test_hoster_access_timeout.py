"""Таймауты nginx (нода и панель) на hoster-access/purge не короче ожидания панели.

Purge гоняет несколько apt purge на ноде; общие location (нода 30 с, панель 60 с)
оборвали бы запрос, хотя нода доводит его до конца.
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_firewall_apply_timeout import location_block, timeout_seconds  # noqa: E402

try:
    from app.routers.proxy import HOSTER_PURGE_TIMEOUT
except ImportError as e:  # pragma: no cover
    raise unittest.SkipTest(f"proxy router requires the panel runtime: {e}")

ROOT = Path(__file__).resolve().parents[3]
NODE_TEMPLATE = ROOT / "node" / "nginx" / "templates" / "api.conf.template"
PANEL_TEMPLATE = ROOT / "panel" / "nginx" / "nginx.conf.template"
NODE_LOCATION = "/api/system/hoster-access/"


def _regex_location_block(config: str, needle: str) -> str | None:
    header = re.search(rf"location\s+~[^\n{{]*{re.escape(needle)}[^\n{{]*\{{", config)
    if header is None:
        return None
    depth, start = 1, header.end()
    for index in range(start, len(config)):
        depth += {"{": 1, "}": -1}.get(config[index], 0)
        if depth == 0:
            return config[start:index]
    return None


class HosterPurgeTimeoutTests(unittest.TestCase):
    def test_node_location_covers_panel_wait(self):
        block = location_block(NODE_TEMPLATE.read_text(encoding="utf-8"), NODE_LOCATION)
        self.assertIsNotNone(block, f"нет location {NODE_LOCATION} в nginx ноды")
        self.assertGreaterEqual(timeout_seconds(block, "proxy_read_timeout"), HOSTER_PURGE_TIMEOUT)
        self.assertGreaterEqual(timeout_seconds(block, "proxy_send_timeout"), HOSTER_PURGE_TIMEOUT)

    def test_panel_location_covers_panel_wait(self):
        block = _regex_location_block(PANEL_TEMPLATE.read_text(encoding="utf-8"), "hoster-access/purge")
        self.assertIsNotNone(block, "нет location hoster-access/purge в nginx панели")
        self.assertGreaterEqual(timeout_seconds(block, "proxy_read_timeout"), HOSTER_PURGE_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
