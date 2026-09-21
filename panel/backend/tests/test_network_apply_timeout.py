"""Таймаут nginx ноды на /api/system/network/ не короче ожидания панели.

Транзакция доп. IP на ноде включает `netplan apply` и проверку DAD; общий
location / (30 с) обрывал бы запрос, хотя нода доводила его до конца.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_firewall_apply_timeout import NODE_NGINX_TEMPLATE, location_block, timeout_seconds  # noqa: E402

try:
    from app.services.network_transactions import APPLY_TIMEOUT_SECONDS
except ImportError as e:  # pragma: no cover
    raise unittest.SkipTest(f"network_transactions requires the panel runtime: {e}")

NETWORK_LOCATION = "/api/system/network/"


class NetworkApplyTimeoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.block = location_block(NODE_NGINX_TEMPLATE.read_text(encoding="utf-8"), NETWORK_LOCATION)

    def test_location_exists(self):
        self.assertIsNotNone(self.block, f"location {NETWORK_LOCATION} is missing in the node nginx template")

    def test_read_timeout_covers_panel_wait(self):
        self.assertGreaterEqual(timeout_seconds(self.block, "proxy_read_timeout"), APPLY_TIMEOUT_SECONDS)

    def test_send_timeout_covers_panel_wait(self):
        self.assertGreaterEqual(timeout_seconds(self.block, "proxy_send_timeout"), APPLY_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
