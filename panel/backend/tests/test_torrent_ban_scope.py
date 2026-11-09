"""Тесты области бана блокировщика торрентов (app/services/torrent_ban_scope.py).

Голый unittest, без PostgreSQL и без сети: режимы «кроме выбранных» / «только
выбранные», бан по портам, разбор сохранённых списков, гейт по версии ноды.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services.torrent_ban_scope import (  # noqa: E402
        MAX_PORT_BAN_PORTS,
        BanScope,
        InvalidPortSpecError,
        ServerMode,
        dump_rules,
        node_supports_port_ban,
        normalize_port_specs,
        parse_rules,
    )
except ImportError as e:  # рантайм панели (sqlalchemy, httpx) не установлен
    raise unittest.SkipTest(f"torrent_ban_scope requires the panel runtime: {e}")


def scope(mode: ServerMode, excluded=None, included=None) -> BanScope:
    return BanScope(mode=mode, excluded=excluded or {}, included=included or {})


class ExcludeModeTest(unittest.TestCase):
    def test_unlisted_server_gets_whole_ip_ban(self):
        ban = scope(ServerMode.EXCLUDE).ban_for(1)
        self.assertIsNotNone(ban)
        self.assertTrue(ban.whole_ip)

    def test_listed_server_without_ports_is_skipped(self):
        self.assertIsNone(scope(ServerMode.EXCLUDE, excluded={1: ()}).ban_for(1))

    def test_listed_server_with_ports_gets_port_ban(self):
        ban = scope(ServerMode.EXCLUDE, excluded={1: ("8443",)}).ban_for(1)
        self.assertEqual(ban.ports, ("8443",))
        self.assertFalse(ban.whole_ip)

    def test_include_list_is_ignored(self):
        self.assertTrue(scope(ServerMode.EXCLUDE, included={1: ("8443",)}).ban_for(1).whole_ip)


class IncludeModeTest(unittest.TestCase):
    def test_unlisted_server_is_skipped(self):
        self.assertIsNone(scope(ServerMode.INCLUDE, included={2: ()}).ban_for(1))

    def test_listed_server_without_ports_gets_whole_ip_ban(self):
        self.assertTrue(scope(ServerMode.INCLUDE, included={1: ()}).ban_for(1).whole_ip)

    def test_listed_server_with_ports_gets_port_ban(self):
        ban = scope(ServerMode.INCLUDE, included={1: ("2053", "8443")}).ban_for(1)
        self.assertEqual(ban.ports, ("2053", "8443"))

    def test_exclude_list_is_ignored(self):
        self.assertIsNone(scope(ServerMode.INCLUDE, excluded={1: ("8443",)}).ban_for(1))


class NormalizePortSpecsTest(unittest.TestCase):
    def test_sorts_and_dedupes(self):
        self.assertEqual(
            normalize_port_specs(["8443", "2053", "8443", "10000-10010", "80-80"]),
            ["80", "2053", "8443", "10000-10010"],
        )

    def test_rejects_garbage(self):
        for bad in (["0"], ["65536"], ["x"], ["20-10"], ["1-2-3"], [" "]):
            with self.subTest(bad=bad), self.assertRaises(InvalidPortSpecError):
                normalize_port_specs(bad)

    def test_limit_counts_expanded_ports(self):
        normalize_port_specs([f"1-{MAX_PORT_BAN_PORTS}"])
        with self.assertRaises(InvalidPortSpecError):
            normalize_port_specs([f"1-{MAX_PORT_BAN_PORTS + 1}"])


class StoredRulesTest(unittest.TestCase):
    def test_round_trip(self):
        rules = {5: (), 7: ("2053", "8443")}
        self.assertEqual(parse_rules(dump_rules(rules)), rules)
        self.assertEqual(
            json.loads(dump_rules(rules)),
            [{"id": 5, "ports": []}, {"id": 7, "ports": ["2053", "8443"]}],
        )

    def test_empty_and_broken_values_are_empty(self):
        for raw in (None, "", "not json", '{"id": 1}', '[{"ports": []}]'):
            with self.subTest(raw=raw):
                self.assertEqual(parse_rules(raw), {})

    def test_broken_entry_does_not_drop_the_rest(self):
        raw = json.dumps([{"id": 1, "ports": ["abc"]}, {"id": 2, "ports": ["8443"]}])
        self.assertEqual(parse_rules(raw), {2: ("8443",)})


class PlanBansTest(unittest.TestCase):
    """TorrentBlockerService._plan_bans: живость, права ipset, гейт версии для портов."""

    def setUp(self):
        try:
            from app.services.torrent_blocker import TorrentBlockerService
        except ImportError as e:
            self.skipTest(f"torrent_blocker requires the panel runtime: {e}")
        self.plan = TorrentBlockerService._plan_bans
        self.now = datetime.now(timezone.utc)

    def server(self, server_id: int, version: str = "10.35.0", seen_ago: int = 10, caps=None):
        return SimpleNamespace(
            id=server_id, name=f"node-{server_id}", node_version=version,
            last_seen=self.now - timedelta(seconds=seen_ago), node_capabilities=caps,
        )

    def planned(self, servers, ban_scope):
        return {s.id: ban.ports for s, ban in self.plan(servers, ban_scope, self.now - timedelta(seconds=300))}

    def test_mixes_whole_ip_and_port_bans(self):
        ban_scope = scope(ServerMode.EXCLUDE, excluded={2: ("8443",), 3: ()})
        servers = [self.server(1), self.server(2), self.server(3)]
        self.assertEqual(self.planned(servers, ban_scope), {1: (), 2: ("8443",)})

    def test_offline_node_is_skipped(self):
        servers = [self.server(1, seen_ago=3600)]
        self.assertEqual(self.planned(servers, scope(ServerMode.EXCLUDE)), {})

    def test_node_with_ipset_closed_is_skipped(self):
        servers = [self.server(1, caps='{"ipset": "ro"}')]
        self.assertEqual(self.planned(servers, scope(ServerMode.EXCLUDE)), {})

    def test_old_node_never_falls_back_to_whole_ip(self):
        ban_scope = scope(ServerMode.INCLUDE, included={1: ("8443",), 2: ()})
        servers = [self.server(1, version="10.34.0"), self.server(2, version="10.34.0")]
        self.assertEqual(self.planned(servers, ban_scope), {2: ()})


class NodeVersionGateTest(unittest.TestCase):
    def test_gate(self):
        self.assertTrue(node_supports_port_ban("10.35.0"))
        self.assertTrue(node_supports_port_ban("10.40.1"))
        self.assertFalse(node_supports_port_ban("10.34.9"))
        self.assertFalse(node_supports_port_ban(None))


if __name__ == "__main__":
    unittest.main()
