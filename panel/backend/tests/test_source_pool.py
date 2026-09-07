"""Пул исходящих адресов, панельная сторона: конфиг для ноды, сниппет Xray, представления.

Голый unittest, без PostgreSQL и без сети: только чистые функции.
Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"

Закреплённые инварианты: номера и число меток в сниппете совпадают с нодой
(иначе Xray пометит соединения метками, для которых на ноде нет правил, и весь
трафик уйдёт с одного адреса); правило балансировщика ловит только TCP; таймаут
запросов к ноде короче `location /` в nginx ноды.
"""

import json
import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services.source_pool import node_client  # noqa: E402
    from app.services.source_pool.render import (  # noqa: E402
        BALANCER_TAG,
        MARK_BASE,
        MARK_COUNT,
        OUTBOUND_TAG_PREFIX,
        build_node_config,
        config_hash,
        xray_outbounds,
        xray_routing,
        xray_snippet,
    )
    from app.services.source_pool.views import (  # noqa: E402
        STATUS_ACTIVE,
        STATUS_DRIFT,
        STATUS_FAILED,
        STATUS_OFF,
        STATUS_PENDING,
        STATUS_UNSUPPORTED,
        install_status,
        marks_per_address,
        node_view,
    )
except ImportError as e:  # рантайм панели (sqlalchemy, httpx) не установлен
    raise unittest.SkipTest(f"source_pool requires the panel runtime: {e}")

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_MODEL = REPO_ROOT / "node" / "app" / "models" / "source_pool.py"
NODE_NGINX_TEMPLATE = REPO_ROOT / "node" / "nginx" / "templates" / "api.conf.template"


def row(**overrides) -> SimpleNamespace:
    base = dict(
        enabled=True, excluded=None, node_state=None, config_hash=None,
        sync_status="synced", sync_error=None, last_sync_at=None, last_state_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def server(**overrides) -> SimpleNamespace:
    base = dict(id=1, name="node-1", node_version="10.29.0", node_capabilities=None)
    base.update(overrides)
    return SimpleNamespace(**base)


class NodeConfigTest(unittest.TestCase):
    def test_missing_row_means_disabled(self):
        self.assertEqual(build_node_config(None), {"enabled": False, "excluded": []})

    def test_excluded_sorted_and_deduplicated(self):
        config = build_node_config(row(excluded=json.dumps(["1.2.3.5", "1.2.3.4", "1.2.3.5"])))
        self.assertEqual(config["excluded"], ["1.2.3.4", "1.2.3.5"])

    def test_hash_ignores_key_order(self):
        a = config_hash({"enabled": True, "excluded": ["1.2.3.4"]})
        b = config_hash({"excluded": ["1.2.3.4"], "enabled": True})
        self.assertEqual(a, b)


class SnippetTest(unittest.TestCase):
    def test_marks_match_node_constants(self):
        """Панель и нода договариваются о метках только через эти константы."""
        text = NODE_MODEL.read_text(encoding="utf-8")
        node_count = int(re.search(r"^MARK_COUNT = (\d+)", text, re.MULTILINE).group(1))
        node_base = int(re.search(r"^MARK_BASE = (\d+)", text, re.MULTILINE).group(1))
        self.assertEqual((MARK_COUNT, MARK_BASE), (node_count, node_base))

    def test_every_outbound_has_its_own_mark(self):
        outbounds = xray_outbounds()
        self.assertEqual(len(outbounds), MARK_COUNT)
        marks = [o["streamSettings"]["sockopt"]["mark"] for o in outbounds]
        self.assertEqual(marks, list(range(MARK_BASE, MARK_BASE + MARK_COUNT)))
        for outbound in outbounds:
            self.assertTrue(outbound["tag"].startswith(OUTBOUND_TAG_PREFIX))
            self.assertEqual(outbound["protocol"], "freedom")

    def test_balancer_selects_by_prefix_and_rule_is_tcp_only(self):
        routing = xray_routing()
        balancer = routing["balancers"][0]
        self.assertEqual(balancer["tag"], BALANCER_TAG)
        self.assertEqual(balancer["selector"], [OUTBOUND_TAG_PREFIX])
        self.assertEqual(balancer["strategy"], {"type": "roundRobin"})
        self.assertEqual(routing["rule"], {"type": "field", "network": "tcp", "balancerTag": BALANCER_TAG})

    def test_snippet_is_valid_json_and_mentions_order(self):
        snippet = xray_snippet()
        self.assertEqual(len(json.loads(snippet["outbounds_json"])), MARK_COUNT)
        json.loads(snippet["routing_json"])
        self.assertIn("ПОСЛЕДНИМ", snippet["text"])


class ViewsTest(unittest.TestCase):
    def test_install_status_ladder(self):
        self.assertEqual(install_status(None, {}), STATUS_OFF)
        self.assertEqual(install_status(row(enabled=False), {}), STATUS_OFF)
        self.assertEqual(install_status(row(sync_status="pending"), {}), STATUS_PENDING)
        self.assertEqual(install_status(row(), {}), STATUS_PENDING)
        self.assertEqual(install_status(row(sync_status="failed"), {}), STATUS_FAILED)
        self.assertEqual(install_status(row(), {"supported": True, "in_sync": True}), STATUS_ACTIVE)
        self.assertEqual(install_status(row(), {"supported": True, "in_sync": False}), STATUS_DRIFT)
        self.assertEqual(install_status(row(), {"supported": False}), STATUS_FAILED)

    def test_marks_counted_per_address(self):
        state = {"bindings": [{"address": "a"}, {"address": "b"}, {"address": "a"}]}
        self.assertEqual(marks_per_address(state), {"a": 2, "b": 1})

    def test_node_view_marks_excluded_and_old_agent(self):
        state = {
            "supported": True, "in_sync": True, "interface": "bond0",
            "addresses": ["1.2.3.4", "1.2.3.5"], "active_addresses": ["1.2.3.4"],
            "bindings": [{"address": "1.2.3.4"}] * 3, "mark_count": 30, "mark_base": 101,
        }
        view = node_view(server(), row(excluded=json.dumps(["1.2.3.5"]), node_state=json.dumps(state)), True)
        self.assertEqual(view["install_status"], STATUS_ACTIVE)
        self.assertEqual(view["addresses"], [
            {"address": "1.2.3.4", "excluded": False, "marks": 3},
            {"address": "1.2.3.5", "excluded": True, "marks": 0},
        ])
        old = node_view(server(node_version="10.28.0"), row(node_state=json.dumps(state)), True)
        self.assertEqual(old["install_status"], STATUS_UNSUPPORTED)
        self.assertFalse(old["supported_by_node"])


class NodeClientTest(unittest.TestCase):
    def test_version_gate(self):
        self.assertTrue(node_client.node_supports_source_pool("10.29.0"))
        self.assertTrue(node_client.node_supports_source_pool("10.30.1"))
        self.assertFalse(node_client.node_supports_source_pool("10.28.9"))
        self.assertFalse(node_client.node_supports_source_pool(None))

    def test_timeout_below_node_nginx_default(self):
        """У /api/system/source-pool/* нет своего location — действует общий proxy_read_timeout."""
        template = NODE_NGINX_TEMPLATE.read_text(encoding="utf-8")
        location = re.search(r"location\s+/\s*\{(.*?)\n\s*\}", template, re.S)
        self.assertIsNotNone(location, "в шаблоне nginx ноды нет location /")
        read_timeout = re.search(r"proxy_read_timeout\s+(\d+)s;", location.group(1))
        self.assertIsNotNone(read_timeout)
        self.assertLess(node_client.NODE_TIMEOUT_SEC, int(read_timeout.group(1)))


if __name__ == "__main__":
    unittest.main()
