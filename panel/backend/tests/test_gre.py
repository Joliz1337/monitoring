"""GRE-туннели DDoS-Guard, панельная сторона: схема туннеля, номера, конфиг для
ноды, учёт входящих пакетов, представление, тексты уведомлений.

Голый unittest, без PostgreSQL и без сети: только чистые функции.
Запуск из panel/backend:  python -m unittest tests.test_gre

Закреплённые инварианты: проверки туннеля и интерфейс совпадают с нодой (иначе
нода отвергла бы сохранённый туннель); PE по умолчанию — второй узел /30, а не
слепо CE − 1; у существующего туннеля номер не меняется; «замолчал» сообщается
только про туннель, по которому пакеты уже шли, и один раз на переход; гейт версии
не выше `node/VERSION`; таймаут запросов к ноде короче `location /` в nginx ноды.
"""

import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from pydantic import ValidationError  # noqa: E402

    from app.services.gre import node_client  # noqa: E402
    from app.services.gre.activity import (  # noqa: E402
        SILENT_AFTER_SECONDS,
        Activity,
        ActivityEvent,
        activity_status,
        update_activity,
    )
    from app.services.gre.alerts import alert_text  # noqa: E402
    from app.services.gre.render import (  # noqa: E402
        DEFAULT_HEALTH_NET,
        DEFAULT_REMOTE,
        INTERFACE_PREFIX,
        MAX_TUNNELS,
        GreTunnel,
        assign_slots,
        build_node_config,
        check_tunnel_set,
        config_hash,
        dump_tunnels,
        load_tunnels,
        ptp_peer,
        whitelist_entries,
    )
    from app.services.gre.service import needs_cycle  # noqa: E402
    from app.services.gre.views import (  # noqa: E402
        STATUS_ACTIVE,
        STATUS_DRIFT,
        STATUS_OFF,
        STATUS_PENDING,
        STATUS_UNSUPPORTED,
        install_status,
        node_view,
    )
except ImportError as e:  # рантайм панели (sqlalchemy, httpx) не установлен
    raise unittest.SkipTest(f"gre requires the panel runtime: {e}")

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_MODEL = REPO_ROOT / "node" / "app" / "models" / "gre.py"
NODE_NGINX_TEMPLATE = REPO_ROOT / "node" / "nginx" / "templates" / "api.conf.template"

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
TUNNEL = {"slot": 1, "local": "178.214.103.5", "ce": "91.215.40.174"}


def row(**overrides) -> SimpleNamespace:
    base = dict(
        tunnels=dump_tunnels([GreTunnel(**TUNNEL)]), notify=True, activity=None, node_state=None,
        config_hash=None, sync_status="synced", sync_error=None, last_sync_at=None, last_state_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def server(**overrides) -> SimpleNamespace:
    base = dict(id=1, name="node-1", url="https://178.214.103.5:9100", node_version="10.35.0", node_capabilities=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def node_tunnel(**overrides) -> dict:
    base = dict(slot=1, name="gre-ddg1", local="178.214.103.5", remote=DEFAULT_REMOTE, pe="91.215.40.173",
                ce="91.215.40.174", health_net=DEFAULT_HEALTH_NET, link="ok", mtu=1476, conflict_with=None,
                rx_bytes=900, rx_packets=12, tx_bytes=700, tx_packets=10)
    base.update(overrides)
    return base


class TunnelSchemaTest(unittest.TestCase):
    def test_defaults_are_ddos_guard(self):
        tunnel = GreTunnel(local="178.214.103.5", ce="91.215.40.174")
        self.assertEqual(tunnel.remote, DEFAULT_REMOTE)
        self.assertEqual(tunnel.health_net, DEFAULT_HEALTH_NET)
        self.assertEqual(tunnel.pe, "91.215.40.173")

    def test_pe_is_the_other_host_of_the_ptp_network(self):
        self.assertEqual(ptp_peer("91.215.40.174"), "91.215.40.173")
        # CE на первом узле: CE − 1 был бы адресом сети
        self.assertEqual(ptp_peer("91.215.40.173"), "91.215.40.174")
        self.assertIsNone(ptp_peer("91.215.40.172"))

    def test_rejects_what_the_node_rejects(self):
        for changes in ({"ce": "91.215.40.172"}, {"ce": "91.215.40.175"}, {"pe": "91.215.40.177"},
                        {"local": "91.215.40.173"}, {"remote": "91.215.40.174"}, {"ce": "2001:db8::1"},
                        {"health_net": "0.0.0.0/0"}, {"slot": MAX_TUNNELS + 1}):
            with self.assertRaises(ValidationError, msg=str(changes)):
                GreTunnel(**{**TUNNEL, **changes})

    def test_node_limits_and_interface_match(self):
        node_model = NODE_MODEL.read_text(encoding="utf-8")
        self.assertIn(f"MAX_TUNNELS = {MAX_TUNNELS}", node_model)
        self.assertIn(f'INTERFACE_PREFIX = "{INTERFACE_PREFIX}"', node_model)
        self.assertIn("PTP_PREFIX = 30", node_model)

    def test_node_accepts_what_the_panel_sends(self):
        config = build_node_config(row())
        self.assertEqual(config["tunnels"][0], {
            "slot": 1, "local": "178.214.103.5", "remote": DEFAULT_REMOTE, "pe": "91.215.40.173",
            "ce": "91.215.40.174", "health_net": DEFAULT_HEALTH_NET,
        })


class TunnelSetTest(unittest.TestCase):
    def second(self, **changes) -> GreTunnel:
        return GreTunnel(**{"local": "178.214.103.6", "ce": "91.215.40.178", **changes})

    def test_new_tunnels_get_free_slots_and_old_ones_keep_theirs(self):
        tunnels = assign_slots([GreTunnel(**{**TUNNEL, "slot": 2}), self.second(), self.second(local="10.0.0.1", ce="91.215.40.182")])
        self.assertEqual([t.slot for t in tunnels], [1, 2, 3])
        self.assertEqual(tunnels[1].ce, "91.215.40.174")

    def test_set_rules(self):
        check_tunnel_set([GreTunnel(**TUNNEL), self.second()])
        for clash in (self.second(slot=1), self.second(ce="91.215.40.174"), self.second(local="178.214.103.5")):
            with self.assertRaises(ValueError):
                check_tunnel_set([GreTunnel(**TUNNEL), clash])

    def test_load_drops_broken_records(self):
        stored = json.dumps([{**TUNNEL, "pe": "91.215.40.173", "remote": DEFAULT_REMOTE, "health_net": DEFAULT_HEALTH_NET},
                             {"slot": 2, "local": "x", "ce": "y"}, "junk"])
        self.assertEqual([t.slot for t in load_tunnels(row(tunnels=stored))], [1])
        self.assertEqual(load_tunnels(row(tunnels="not json")), [])
        self.assertEqual(load_tunnels(None), [])

    def test_hash_follows_the_config(self):
        first = config_hash(build_node_config(row()))
        self.assertEqual(first, config_hash(build_node_config(row())))
        changed = row(tunnels=dump_tunnels([GreTunnel(**{**TUNNEL, "local": "178.214.103.6"})]))
        self.assertNotEqual(first, config_hash(build_node_config(changed)))
        self.assertEqual(build_node_config(row(tunnels=None)), {"tunnels": []})

    def test_whitelist_entries(self):
        self.assertEqual(whitelist_entries([row(), row(tunnels=None)]), {DEFAULT_REMOTE, DEFAULT_HEALTH_NET})

    def test_cycle_follows_tunnels_and_pending_removal(self):
        self.assertTrue(needs_cycle(row()))
        self.assertTrue(needs_cycle(row(tunnels="[]", sync_status="pending")))
        self.assertFalse(needs_cycle(row(tunnels="[]", sync_status="synced")))


class ActivityTest(unittest.TestCase):
    def test_first_sight_waits(self):
        activity, changes = update_activity({}, [node_tunnel()], NOW)
        self.assertIsNone(activity["gre-ddg1"]["changed_at"])
        self.assertEqual(changes, [])
        self.assertEqual(activity_status(activity["gre-ddg1"], NOW)[0], Activity.WAITING)

    def test_growing_counter_is_alive(self):
        activity, _ = update_activity({}, [node_tunnel(rx_packets=12)], NOW)
        later = NOW + timedelta(minutes=1)
        activity, changes = update_activity(activity, [node_tunnel(rx_packets=20)], later)
        self.assertEqual(changes, [])
        status, last_rx_at = activity_status(activity["gre-ddg1"], later)
        self.assertEqual(status, Activity.ALIVE)
        self.assertEqual(last_rx_at, later.isoformat())

    def test_silence_after_traffic_is_reported_once_and_recovery_too(self):
        activity, _ = update_activity({}, [node_tunnel(rx_packets=12)], NOW)
        activity, _ = update_activity(activity, [node_tunnel(rx_packets=20)], NOW + timedelta(minutes=1))
        quiet = NOW + timedelta(minutes=1, seconds=SILENT_AFTER_SECONDS)
        activity, changes = update_activity(activity, [node_tunnel(rx_packets=20)], quiet)
        self.assertEqual([c.event for c in changes], [ActivityEvent.SILENT])
        self.assertEqual(changes[0].ce, "91.215.40.174")
        self.assertEqual(activity_status(activity["gre-ddg1"], quiet)[0], Activity.SILENT)
        activity, changes = update_activity(activity, [node_tunnel(rx_packets=20)], quiet + timedelta(minutes=1))
        self.assertEqual(changes, [])
        # Интерфейс пересоздан — счётчик меньше прежнего, но пакеты пошли
        activity, changes = update_activity(activity, [node_tunnel(rx_packets=3)], quiet + timedelta(minutes=2))
        self.assertEqual([c.event for c in changes], [ActivityEvent.RECOVERED])
        self.assertFalse(activity["gre-ddg1"]["silent"])

    def test_tunnel_that_never_got_packets_is_silent_without_alerts(self):
        activity, _ = update_activity({}, [node_tunnel(rx_packets=0)], NOW)
        later = NOW + timedelta(seconds=SILENT_AFTER_SECONDS + 60)
        activity, changes = update_activity(activity, [node_tunnel(rx_packets=0)], later)
        self.assertEqual(changes, [])
        self.assertEqual(activity_status(activity["gre-ddg1"], later), (Activity.SILENT, None))

    def test_removed_tunnel_is_forgotten(self):
        activity, _ = update_activity({}, [node_tunnel()], NOW)
        activity, _ = update_activity(activity, [], NOW + timedelta(minutes=1))
        self.assertEqual(activity, {})


class ViewsTest(unittest.TestCase):
    def test_install_status_ladder(self):
        self.assertEqual(install_status(None, False, {}), STATUS_OFF)
        self.assertEqual(install_status(row(tunnels="[]"), False, {}), STATUS_OFF)
        self.assertEqual(install_status(row(sync_status="pending"), True, {}), STATUS_PENDING)
        self.assertEqual(install_status(row(), True, {"supported": True, "in_sync": True}), STATUS_ACTIVE)
        self.assertEqual(install_status(row(), True, {"supported": True, "in_sync": False}), STATUS_DRIFT)
        # Снятие ещё не доехало — не «выключено»
        self.assertEqual(install_status(row(tunnels="[]", sync_status="pending"), False, {}), STATUS_PENDING)
        # Агент без GRE и туннелей нет — снимать нечего
        self.assertEqual(install_status(row(tunnels="[]", sync_status="unsupported"), False, {}), STATUS_OFF)

    def test_view_merges_node_state_and_activity(self):
        state = {"supported": True, "in_sync": True, "tunnels": [node_tunnel()]}
        activity = {"gre-ddg1": {"rx_packets": 12, "changed_at": (NOW - timedelta(minutes=1)).isoformat(),
                                 "first_seen_at": NOW.isoformat(), "silent": False}}
        view = node_view(server(), row(node_state=json.dumps(state), activity=json.dumps(activity)), True, NOW)
        self.assertEqual(view["install_status"], STATUS_ACTIVE)
        tunnel = view["tunnels"][0]
        self.assertEqual((tunnel["name"], tunnel["link"], tunnel["mtu"], tunnel["rx_bytes"]), ("gre-ddg1", "ok", 1476, 900))
        self.assertEqual(tunnel["activity"], Activity.ALIVE.value)
        self.assertEqual(view["default_remote"], DEFAULT_REMOTE)

    def test_state_of_a_previous_variant_is_not_shown(self):
        state = {"supported": True, "in_sync": True, "tunnels": [node_tunnel(ce="91.215.40.178", pe="91.215.40.177")]}
        tunnel = node_view(server(), row(node_state=json.dumps(state)), True, NOW)["tunnels"][0]
        self.assertIsNone(tunnel["link"])
        self.assertEqual(tunnel["rx_bytes"], 0)

    def test_old_agent_is_unsupported(self):
        view = node_view(server(node_version="10.34.0"), row(), True, NOW)
        self.assertEqual(view["install_status"], STATUS_UNSUPPORTED)
        self.assertFalse(view["supported_by_node"])


class AlertTextTest(unittest.TestCase):
    def test_texts_name_the_tunnel_and_escape(self):
        from app.services.gre.activity import ActivityChange
        silent = ActivityChange("gre-ddg1", "91.215.40.174", ActivityEvent.SILENT)
        text = alert_text("ru", "<node>", silent)
        self.assertIn("&lt;node&gt;", text)
        self.assertIn("gre-ddg1", text)
        self.assertIn(str(SILENT_AFTER_SECONDS // 60), text)
        self.assertIn("silent", alert_text("en", "node", silent))
        self.assertIn("снова", alert_text("ru", "node", ActivityChange("gre-ddg1", "91.215.40.174", ActivityEvent.RECOVERED)))


class NodeClientTest(unittest.TestCase):
    def test_version_gate_matches_node_version(self):
        self.assertTrue(node_client.node_supports_gre(node_client.MIN_NODE_VERSION_GRE))
        self.assertFalse(node_client.node_supports_gre("10.34.9"))
        self.assertFalse(node_client.node_supports_gre(None))
        node_version = (REPO_ROOT / "node" / "VERSION").read_text(encoding="utf-8").strip()
        self.assertTrue(node_client.node_supports_gre(node_version))

    def test_timeout_below_node_nginx_default(self):
        """У /api/system/gre/* нет своего location — действует общий proxy_read_timeout."""
        template = NODE_NGINX_TEMPLATE.read_text(encoding="utf-8")
        location = re.search(r"location\s+/\s*\{(.*?)\n\s*\}", template, re.S)
        self.assertIsNotNone(location, "в шаблоне nginx ноды нет location /")
        read_timeout = re.search(r"proxy_read_timeout\s+(\d+)s;", location.group(1))
        self.assertIsNotNone(read_timeout)
        self.assertLess(node_client.NODE_TIMEOUT_SEC, int(read_timeout.group(1)))


if __name__ == "__main__":
    unittest.main()
