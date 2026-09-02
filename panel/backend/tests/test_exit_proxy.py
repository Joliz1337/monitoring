"""Exit-прокси, панельная сторона: конфиг для ноды, представления, события, алерты, команды.

Голый unittest, без PostgreSQL и без сети: только чистые функции.
Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"

Закреплённые инварианты: хэш конфига не зависит от порядка ключей; первый
сбор статуса не шлёт алерты за старые события ноды; таймаут запросов к ноде
короче `location /` в nginx ноды (своего location у exit-прокси нет).
"""

import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services.deploy_service import build_warp_install_command  # noqa: E402
    from app.services.exit_proxy import node_client  # noqa: E402
    from app.services.exit_proxy.alerts import ALERT_KINDS, alert_text, candidate_label  # noqa: E402
    from app.services.exit_proxy.render import (  # noqa: E402
        NodePrefs,
        build_node_config,
        config_hash,
        remnawave_rules,
        remnawave_snippet,
    )
    from app.services.exit_proxy.settings import (  # noqa: E402
        DEFAULT_CUSTOM_CHECKS,
        RESERVED_SERVICE_PORTS,
        SettingsSnapshot,
    )
    from app.services.exit_proxy.views import (  # noqa: E402
        STATUS_ACTIVE,
        STATUS_FAILED,
        STATUS_OFF,
        STATUS_PENDING,
        STATUS_UNSUPPORTED,
        install_status,
        new_node_events,
        node_view,
    )
    from app.services.reserved_ports_sync import merged_entries  # noqa: E402
except ImportError as e:  # рантайм панели (sqlalchemy, httpx) не установлен
    raise unittest.SkipTest(f"exit_proxy requires the panel runtime: {e}")

NODE_NGINX_TEMPLATE = Path(__file__).resolve().parents[3] / "node" / "nginx" / "templates" / "api.conf.template"


def settings(**overrides) -> SettingsSnapshot:
    base = dict(
        enabled=True, port=7590, check_interval_minutes=30, blocked_countries=["RU"],
        builtin_checks={"google_country": True, "google_captcha": True, "gemini": True},
        custom_checks=list(DEFAULT_CUSTOM_CHECKS), telegram_enabled=True, alert_cooldown_seconds=1800,
    )
    base.update(overrides)
    return SettingsSnapshot(**base)


def server(**overrides) -> SimpleNamespace:
    base = dict(id=7, name="nl-1", folder="EU", node_version="10.29.0", node_capabilities=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def node_row(**overrides) -> SimpleNamespace:
    base = dict(
        enabled=True, select_mode="auto", pinned_candidate=None, candidates_order=None,
        candidates_disabled=None, node_status=None, sync_status="synced", sync_error=None, last_status_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class RenderTest(unittest.TestCase):
    def test_node_config_carries_global_settings_and_node_prefs(self):
        prefs = NodePrefs(select_mode="manual", pinned_candidate="warp", candidates_order=["warp", "ip:1.2.3.4"])
        config = build_node_config(settings(port=7600), prefs)
        self.assertEqual(config["port"], 7600)
        self.assertEqual(config["select_mode"], "manual")
        self.assertEqual(config["candidates_order"], ["warp", "ip:1.2.3.4"])
        self.assertEqual(config["custom_checks"][0]["id"], "claude")
        self.assertTrue(config["enabled"])

    def test_hash_is_stable_across_key_order(self):
        first = config_hash({"a": 1, "b": [1, 2]})
        second = config_hash({"b": [1, 2], "a": 1})
        self.assertEqual(first, second)
        self.assertNotEqual(first, config_hash({"a": 2, "b": [1, 2]}))

    def test_node_prefs_from_row_tolerates_broken_json(self):
        prefs = NodePrefs.from_row(node_row(candidates_order="not json", candidates_disabled=None))
        self.assertEqual(prefs.candidates_order, [])
        self.assertEqual(prefs.candidates_disabled, [])

    def test_snippet_points_to_local_socks_and_blocks_quic(self):
        snippet = remnawave_snippet(7590)
        self.assertIn('"port": 7590', snippet["outbound_json"])
        self.assertIn('"127.0.0.1"', snippet["outbound_json"])
        rules = remnawave_rules()
        self.assertEqual(rules[0]["network"], "udp")
        self.assertEqual(rules[0]["port"], 443)
        self.assertIn("geosite:google", rules[1]["domain"])
        self.assertIn("geosite:google-gemini", rules[1]["domain"])


class ViewsTest(unittest.TestCase):
    def test_install_status_progression(self):
        self.assertEqual(install_status(None, {}), STATUS_OFF)
        self.assertEqual(install_status(node_row(enabled=False), {}), STATUS_OFF)
        self.assertEqual(install_status(node_row(sync_status="pending"), {}), STATUS_PENDING)
        self.assertEqual(install_status(node_row(), {}), STATUS_PENDING)
        self.assertEqual(install_status(node_row(), {"listening": True}), STATUS_ACTIVE)
        self.assertEqual(install_status(node_row(), {"listening": False}), STATUS_FAILED)
        self.assertEqual(install_status(node_row(sync_status="denied"), {"listening": True}), "denied")

    def test_node_view_reads_node_status_json(self):
        status = {
            "listening": True, "current": "ip:5.255.127.33", "warp_present": True,
            "check_in_progress": False, "last_check_at": "2026-09-02T10:00:00+00:00",
            "self_test": {"ok": True, "ip": "5.255.127.33", "expected": "5.255.127.33", "at": "t", "error": None},
            "stats": {"active_connections": 3, "total_connections": 10, "failed_connections": 0},
            "candidates": [
                {"id": "ip:5.255.127.33", "kind": "ip", "address": "5.255.127.33", "primary": True, "enabled": True,
                 "priority": 0, "healthy": False,
                 "last_check": {"ip": "5.255.127.33", "country": "RU", "captcha": True, "gemini": "ok",
                                "checks": [{"name": "Claude", "ok": True, "status": 200, "detail": "status 200"}],
                                "checked_at": "t"}},
                {"id": "warp", "kind": "warp", "address": "127.0.0.1:9091", "enabled": True, "priority": 1, "healthy": None},
            ],
        }
        import json
        view = node_view(server(), node_row(node_status=json.dumps(status)), online=True)
        self.assertEqual(view["install_status"], STATUS_ACTIVE)
        self.assertEqual(view["current_exit"]["label"], "5.255.127.33")
        self.assertEqual(view["current_exit"]["country"], "RU")
        self.assertEqual(view["candidates"][1]["label"], "WARP")
        self.assertIsNone(view["candidates"][1]["ip"])
        claude = view["candidates"][0]["checks"]["Claude"]
        self.assertEqual((claude["ok"], claude["status"]), (True, 200))
        self.assertTrue(view["candidates"][0]["captcha"])
        self.assertTrue(view["self_test"]["ok"])
        self.assertEqual(view["stats"]["active_connections"], 3)
        self.assertTrue(view["warp"]["present"])

    def test_old_node_is_reported_unsupported(self):
        view = node_view(server(node_version="10.28.0"), node_row(), online=True)
        self.assertEqual(view["install_status"], STATUS_UNSUPPORTED)

    def test_disabled_node_is_off_regardless_of_status(self):
        view = node_view(server(), node_row(enabled=False, node_status='{"listening": true}'), online=False)
        self.assertEqual(view["install_status"], STATUS_OFF)
        self.assertFalse(view["online"])

    def test_new_node_events_are_chronological_and_after_watermark(self):
        events = [
            {"at": "2026-09-02T10:05:00+00:00", "kind": "switched"},
            {"at": "2026-09-02T10:00:00+00:00", "kind": "started"},
            {"at": "2026-09-02T09:00:00+00:00", "kind": "old"},
            {"kind": "broken"},
        ]
        fresh = new_node_events(events, "2026-09-02T09:30:00+00:00")
        self.assertEqual([event["kind"] for event in fresh], ["started", "switched"])
        self.assertEqual(len(new_node_events(events, None)), 3)


class AlertsTest(unittest.TestCase):
    def test_labels_and_html_escaping(self):
        self.assertEqual(candidate_label("ip:5.255.127.33"), "5.255.127.33")
        self.assertEqual(candidate_label("warp"), "WARP")
        self.assertEqual(candidate_label(None), "—")
        text = alert_text("switched", "<nl>", "ip:1.1.1.1", "warp", "switched")
        self.assertIn("&lt;nl&gt;", text)
        self.assertIn("1.1.1.1 → <b>WARP</b>", text)
        self.assertIn("не прошёл проверки", text)

    def test_only_meaningful_kinds_reach_telegram(self):
        self.assertIn("switched", ALERT_KINDS)
        self.assertIn("no_healthy", ALERT_KINDS)
        self.assertNotIn("manual_switch", ALERT_KINDS)
        self.assertNotIn("started", ALERT_KINDS)


class NodeClientTest(unittest.TestCase):
    def test_version_gate(self):
        self.assertTrue(node_client.node_supports_exit_proxy("10.29.0"))
        self.assertTrue(node_client.node_supports_exit_proxy("10.30.1"))
        self.assertFalse(node_client.node_supports_exit_proxy("10.28.9"))
        self.assertFalse(node_client.node_supports_exit_proxy(None))

    def test_unsupported_and_denied_raise_before_network(self):
        with self.assertRaises(node_client.ExitProxyNodeUnsupported):
            node_client.ensure_node_ready(server(node_version="10.20.0"))
        with self.assertRaises(node_client.ExitProxyNodeDenied):
            node_client.ensure_node_ready(server(node_capabilities='{"system": "ro"}'))
        node_client.ensure_node_ready(server())

    def test_timeout_fits_default_nginx_location(self):
        config = NODE_NGINX_TEMPLATE.read_text(encoding="utf-8")
        match = re.search(r"location\s+/\s*\{(.*?)\n\s*\}", config, re.S)
        self.assertIsNotNone(match, "в шаблоне nginx ноды нет location /")
        read_timeout = re.search(r"proxy_read_timeout\s+(\d+)s;", match.group(1))
        self.assertIsNotNone(read_timeout)
        self.assertLess(node_client.NODE_TIMEOUT_SEC, int(read_timeout.group(1)))


class IntegrationPointsTest(unittest.TestCase):
    def test_reserved_ports_include_service_entries(self):
        self.assertEqual(merged_entries("5201", "8443", ["7590"]), ["5201", "7590", "8443"])
        self.assertEqual(merged_entries(None, None, []), [])

    def test_default_port_is_not_a_service_port(self):
        self.assertNotIn(7590, RESERVED_SERVICE_PORTS)
        self.assertIn(7500, RESERVED_SERVICE_PORTS)
        self.assertIn(7564, RESERVED_SERVICE_PORTS)
        self.assertIn(9091, RESERVED_SERVICE_PORTS)

    def test_warp_install_command(self):
        command = build_warp_install_command()
        self.assertIn("MON_INSTALL_WARP=1", command)
        self.assertIn("--unattended", command)
        self.assertNotIn("MON_INSTALL_REMNAWAVE", command)
        self.assertNotIn("MON_INSTALL_NODE", command)


if __name__ == "__main__":
    unittest.main()
