"""Тесты области бана блокировщика торрентов (app/services/torrent_ban_scope.py).

Голый unittest, без PostgreSQL и без сети: режимы «кроме выбранных» / «только
выбранные», бан по портам, порты папок и свои настройки сервера, разбор
сохранённых списков, гейт по версии ноды.

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
        ServerList,
        ServerMode,
        ServerRule,
        dump_folder_rules,
        dump_server_rules,
        node_supports_port_ban,
        normalize_port_specs,
        parse_folder_rules,
        parse_server_rules,
    )
except ImportError as e:  # рантайм панели (sqlalchemy, httpx) не установлен
    raise unittest.SkipTest(f"torrent_ban_scope requires the panel runtime: {e}")

BRIDGES = "Мосты"


def server_list(servers=None, folders=None) -> ServerList:
    """{id: порты | ServerRule} → ServerList; кортеж портов — без отказа от правила папки."""
    rules = {
        server_id: rule if isinstance(rule, ServerRule) else ServerRule(rule)
        for server_id, rule in (servers or {}).items()
    }
    return ServerList(rules, folders or {})


def scope(mode: ServerMode, excluded=None, included=None) -> BanScope:
    return BanScope(mode=mode, excluded=excluded or ServerList(), included=included or ServerList())


class ExcludeModeTest(unittest.TestCase):
    def test_unlisted_server_gets_whole_ip_ban(self):
        ban = scope(ServerMode.EXCLUDE).ban_for(1, None)
        self.assertIsNotNone(ban)
        self.assertTrue(ban.whole_ip)

    def test_listed_server_without_ports_is_skipped(self):
        self.assertIsNone(scope(ServerMode.EXCLUDE, excluded=server_list({1: ()})).ban_for(1, None))

    def test_listed_server_with_ports_gets_port_ban(self):
        ban = scope(ServerMode.EXCLUDE, excluded=server_list({1: ("8443",)})).ban_for(1, None)
        self.assertEqual(ban.ports, ("8443",))
        self.assertFalse(ban.whole_ip)

    def test_include_list_is_ignored(self):
        ban_scope = scope(ServerMode.EXCLUDE, included=server_list({1: ("8443",)}))
        self.assertTrue(ban_scope.ban_for(1, None).whole_ip)


class IncludeModeTest(unittest.TestCase):
    def test_unlisted_server_is_skipped(self):
        self.assertIsNone(scope(ServerMode.INCLUDE, included=server_list({2: ()})).ban_for(1, None))

    def test_listed_server_without_ports_gets_whole_ip_ban(self):
        ban_scope = scope(ServerMode.INCLUDE, included=server_list({1: ()}))
        self.assertTrue(ban_scope.ban_for(1, None).whole_ip)

    def test_listed_server_with_ports_gets_port_ban(self):
        ban = scope(ServerMode.INCLUDE, included=server_list({1: ("2053", "8443")})).ban_for(1, None)
        self.assertEqual(ban.ports, ("2053", "8443"))

    def test_exclude_list_is_ignored(self):
        ban_scope = scope(ServerMode.INCLUDE, excluded=server_list({1: ("8443",)}))
        self.assertIsNone(ban_scope.ban_for(1, None))


class FolderRulesTest(unittest.TestCase):
    def test_server_without_own_rule_takes_folder_ports(self):
        included = server_list({1: ()}, {BRIDGES: ("8443",)})
        self.assertEqual(scope(ServerMode.INCLUDE, included=included).ban_for(1, BRIDGES).ports, ("8443",))

    def test_exclude_mode_takes_folder_ports_too(self):
        excluded = server_list({1: ()}, {BRIDGES: ("8443",)})
        self.assertEqual(scope(ServerMode.EXCLUDE, excluded=excluded).ban_for(1, BRIDGES).ports, ("8443",))

    def test_folder_ports_apply_only_to_listed_servers(self):
        listed = server_list({1: ()}, {BRIDGES: ("8443",)})
        self.assertIsNone(scope(ServerMode.INCLUDE, included=listed).ban_for(2, BRIDGES))
        self.assertTrue(scope(ServerMode.EXCLUDE, excluded=listed).ban_for(2, BRIDGES).whole_ip)

    def test_own_ports_beat_folder_ports(self):
        included = server_list({1: ("2053",)}, {BRIDGES: ("8443",)})
        self.assertEqual(scope(ServerMode.INCLUDE, included=included).ban_for(1, BRIDGES).ports, ("2053",))

    def test_override_restores_mode_default_inside_folder(self):
        listed = server_list({1: ServerRule((), override=True)}, {BRIDGES: ("8443",)})
        self.assertTrue(scope(ServerMode.INCLUDE, included=listed).ban_for(1, BRIDGES).whole_ip)
        self.assertIsNone(scope(ServerMode.EXCLUDE, excluded=listed).ban_for(1, BRIDGES))

    def test_other_folder_rule_does_not_apply(self):
        included = server_list({1: ()}, {"Финляндия": ("8443",)})
        ban_scope = scope(ServerMode.INCLUDE, included=included)
        self.assertTrue(ban_scope.ban_for(1, BRIDGES).whole_ip)
        self.assertTrue(ban_scope.ban_for(1, None).whole_ip)


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
    def test_server_round_trip(self):
        rules = {5: ServerRule(), 6: ServerRule((), override=True), 7: ServerRule(("2053", "8443"))}
        self.assertEqual(json.loads(dump_server_rules(rules)), [
            {"id": 5, "ports": [], "override": False},
            {"id": 6, "ports": [], "override": True},
            {"id": 7, "ports": ["2053", "8443"], "override": True},
        ])
        parsed = parse_server_rules(dump_server_rules(rules))
        self.assertEqual({k: (v.ports, v.own) for k, v in parsed.items()}, {
            5: ((), False), 6: ((), True), 7: (("2053", "8443"), True),
        })

    def test_entries_without_override_keep_their_meaning(self):
        raw = json.dumps([{"id": 5, "ports": []}, {"id": 7, "ports": ["8443"]}])
        excluded = ServerList(parse_server_rules(raw), {BRIDGES: ("2053",)})
        self.assertEqual(excluded.ports_for(5, BRIDGES), ("2053",))
        self.assertEqual(excluded.ports_for(7, BRIDGES), ("8443",))

    def test_folder_round_trip_drops_folders_without_ports(self):
        raw = json.dumps([{"name": BRIDGES, "ports": ["8443"]}, {"name": "Пусто", "ports": []}])
        self.assertEqual(parse_folder_rules(raw), {BRIDGES: ("8443",)})
        self.assertEqual(parse_folder_rules(dump_folder_rules({BRIDGES: ("8443",)})), {BRIDGES: ("8443",)})

    def test_empty_and_broken_values_are_empty(self):
        for raw in (None, "", "not json", '{"id": 1}', '[{"ports": []}]'):
            with self.subTest(raw=raw):
                self.assertEqual(parse_server_rules(raw), {})
                self.assertEqual(parse_folder_rules(raw), {})

    def test_broken_entry_does_not_drop_the_rest(self):
        raw = json.dumps([{"id": 1, "ports": ["abc"]}, {"id": 2, "ports": ["8443"]}])
        self.assertEqual(parse_server_rules(raw), {2: ServerRule(("8443",))})
        raw = json.dumps([
            {"name": 5, "ports": ["8443"]},
            {"name": "A", "ports": ["x"]},
            {"name": "B", "ports": ["53"]},
        ])
        self.assertEqual(parse_folder_rules(raw), {"B": ("53",)})


class PlanBansTest(unittest.TestCase):
    """TorrentBlockerService._plan_bans: живость, права ipset, гейт версии для портов, папки."""

    def setUp(self):
        try:
            from app.services.torrent_blocker import TorrentBlockerService
        except ImportError as e:
            self.skipTest(f"torrent_blocker requires the panel runtime: {e}")
        self.plan = TorrentBlockerService._plan_bans
        self.now = datetime.now(timezone.utc)

    def server(self, server_id: int, version: str = "10.35.0", seen_ago: int = 10, caps=None, folder=None):
        return SimpleNamespace(
            id=server_id, name=f"node-{server_id}", node_version=version, folder=folder,
            last_seen=self.now - timedelta(seconds=seen_ago), node_capabilities=caps,
        )

    def planned(self, servers, ban_scope):
        return {s.id: ban.ports for s, ban in self.plan(servers, ban_scope, self.now - timedelta(seconds=300))}

    def test_mixes_whole_ip_and_port_bans(self):
        ban_scope = scope(ServerMode.EXCLUDE, excluded=server_list({2: ("8443",), 3: ()}))
        servers = [self.server(1), self.server(2), self.server(3)]
        self.assertEqual(self.planned(servers, ban_scope), {1: (), 2: ("8443",)})

    def test_folder_ports_reach_the_plan(self):
        ban_scope = scope(ServerMode.INCLUDE, included=server_list({1: (), 2: ()}, {BRIDGES: ("8443",)}))
        servers = [self.server(1, folder=BRIDGES), self.server(2)]
        self.assertEqual(self.planned(servers, ban_scope), {1: ("8443",), 2: ()})

    def test_offline_node_is_skipped(self):
        servers = [self.server(1, seen_ago=3600)]
        self.assertEqual(self.planned(servers, scope(ServerMode.EXCLUDE)), {})

    def test_node_with_ipset_closed_is_skipped(self):
        servers = [self.server(1, caps='{"ipset": "ro"}')]
        self.assertEqual(self.planned(servers, scope(ServerMode.EXCLUDE)), {})

    def test_old_node_never_falls_back_to_whole_ip(self):
        ban_scope = scope(ServerMode.INCLUDE, included=server_list({1: ("8443",), 2: ()}))
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
