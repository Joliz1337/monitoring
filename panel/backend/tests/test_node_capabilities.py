"""Тесты прав ноды на стороне панели (app/services/node_capabilities.py).

Голый unittest, без PostgreSQL и без сети.

Главный здесь — MirrorTest: карта путей продублирована на ноде и в панели, и
разъехаться она может молча. Панель тогда либо перестанет пускать рабочую
функцию, либо начнёт ходить в закрытое — то есть ровно то, ради чего всё
делалось. Второй по важности — CallSiteCoverageTest: новый файл, который ходит
к ноде мимо реестра, ломает сборку.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import importlib.util
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.services.node_capabilities import (  # noqa: E402
        ACCESS_NONE,
        ACCESS_READ,
        ACCESS_WRITE,
        ALWAYS_ALLOWED,
        DENIAL_ERROR,
        DOMAIN_PREFIXES,
        READ_ONLY_WRITES,
        Capability,
        allowed_capabilities,
        allows,
        capabilities_from_denial,
        capabilities_from_version,
        denial_headers,
        is_restricted,
        normalize_map,
        parse,
        resolve_route,
    )
except ImportError as e:  # рантайм панели не установлен
    raise unittest.SkipTest(f"node_capabilities requires the panel runtime: {e}")


def caps_json(**overrides: str) -> str:
    """Полная карта: закрыто всё, кроме перечисленного — так её шлёт нода."""
    full = {c.value: ACCESS_NONE for c in Capability}
    full.update(overrides)
    return json.dumps(full, sort_keys=True, separators=(",", ":"))


class ParseTest(unittest.TestCase):
    def test_absent_map_means_no_limits(self):
        for raw in [None, "", "null", "[]", "{}", "не json"]:
            with self.subTest(raw=raw):
                self.assertIsNone(parse(raw))

    def test_valid_map(self):
        parsed = parse(caps_json(haproxy=ACCESS_WRITE))
        self.assertEqual(parsed["haproxy"], ACCESS_WRITE)
        self.assertEqual(parsed["ssh"], ACCESS_NONE)


class AllowsTest(unittest.TestCase):
    def test_no_map_allows_everything(self):
        for capability in Capability:
            for write in (True, False):
                with self.subTest(capability=capability, write=write):
                    self.assertTrue(allows(None, capability, write=write))

    def test_levels(self):
        raw = caps_json(haproxy=ACCESS_WRITE, traffic=ACCESS_READ)
        self.assertTrue(allows(raw, Capability.HAPROXY, write=True))
        self.assertTrue(allows(raw, Capability.TRAFFIC, write=False))
        self.assertFalse(allows(raw, Capability.TRAFFIC, write=True))
        self.assertFalse(allows(raw, Capability.SSH, write=False))

    def test_unknown_entries_fail_open(self):
        # Нода старее панели: домена в карте нет — решает нода, не мы.
        partial = json.dumps({"haproxy": ACCESS_NONE})
        self.assertTrue(allows(partial, Capability.EXEC, write=True))
        weird = json.dumps({"ssh": "maybe"})
        self.assertTrue(allows(weird, Capability.SSH, write=True))

    def test_is_restricted(self):
        self.assertFalse(is_restricted(None))
        self.assertFalse(is_restricted(caps_json(**{c.value: ACCESS_WRITE for c in Capability})))
        self.assertTrue(is_restricted(caps_json(haproxy=ACCESS_WRITE)))

    def test_allowed_capabilities_lists_only_reachable(self):
        raw = caps_json(haproxy=ACCESS_WRITE, traffic=ACCESS_READ)
        self.assertEqual(allowed_capabilities(raw), ["traffic", "haproxy"])
        self.assertEqual(len(allowed_capabilities(None)), len(Capability))


class ResolveRouteTest(unittest.TestCase):
    def test_always_allowed_paths_are_never_gated(self):
        for path in ALWAYS_ALLOWED:
            with self.subTest(path=path):
                self.assertEqual(resolve_route(path, "POST"), (None, False))

    def test_firewall_lives_under_two_prefixes(self):
        self.assertEqual(resolve_route("/api/haproxy/firewall/rules")[0], Capability.FIREWALL)
        self.assertEqual(resolve_route("/api/firewall/profile/apply")[0], Capability.FIREWALL)
        self.assertEqual(resolve_route("/api/haproxy/config")[0], Capability.HAPROXY)

    def test_execute_stream_is_exec_not_system(self):
        # startswith("/api/system/execute/") здесь ошибался
        self.assertEqual(resolve_route("/api/system/execute-stream", "POST")[0], Capability.EXEC)
        self.assertEqual(resolve_route("/api/system/execute", "POST")[0], Capability.EXEC)
        self.assertEqual(resolve_route("/api/system/time-sync", "POST")[0], Capability.SYSTEM)

    def test_exact_match_does_not_leak_into_neighbours(self):
        self.assertEqual(resolve_route("/api/system/updateXYZ", "POST")[0], Capability.SYSTEM)

    def test_write_flag(self):
        self.assertFalse(resolve_route("/api/traffic/legacy/export", "GET")[1])
        self.assertTrue(resolve_route("/api/traffic/legacy/purge", "POST")[1])
        self.assertTrue(resolve_route("/api/ipset/remove", "DELETE")[1])
        self.assertTrue(resolve_route("/api/ssh/keys", "DELETE")[1])
        for path in READ_ONLY_WRITES:
            with self.subTest(path=path):
                self.assertFalse(resolve_route(path, "POST")[1])

    def test_query_string_is_ignored(self):
        # recovery_reconciler ходит с ?path=/opt/remnanode
        self.assertEqual(
            resolve_route("/api/remnawave/nginx/config?path=/opt/remnanode")[0],
            Capability.REMNAWAVE,
        )

    def test_unmapped_path_is_not_gated(self):
        self.assertEqual(resolve_route("/api/whatever-new"), (None, False))


class DenialLearningTest(unittest.TestCase):
    def test_denial_body_yields_canonical_map(self):
        body = {
            "detail": "…",
            "error": DENIAL_ERROR,
            "capabilities": {c.value: ACCESS_NONE for c in Capability},
        }
        self.assertEqual(capabilities_from_denial(body), caps_json())

    def test_foreign_403_is_ignored(self):
        # 403 от чужого прокси перед нодой не должен переписывать права
        for body in [{}, {"detail": "Forbidden"}, {"error": "other", "capabilities": {}},
                     {"error": DENIAL_ERROR, "capabilities": "nope"}, "текст"]:
            with self.subTest(body=body):
                self.assertIsNone(capabilities_from_denial(body))

    def test_version_reply_distinguishes_absent_from_null(self):
        self.assertEqual(capabilities_from_version({"version": "10.20.0"}), (False, None))
        self.assertEqual(capabilities_from_version({"capabilities": None}), (True, None))
        found, value = capabilities_from_version(
            {"capabilities": {c.value: ACCESS_READ for c in Capability}}
        )
        self.assertTrue(found)
        self.assertEqual(value, caps_json(**{c.value: ACCESS_READ for c in Capability}))

    def test_normalize_drops_unknown_entries(self):
        self.assertIsNone(normalize_map({"metrics": "no"}))
        self.assertEqual(normalize_map({"ssh": "ro", "bogus": "ro"}), '{"ssh":"ro"}')

    def test_denial_headers(self):
        self.assertEqual(denial_headers(Capability.HAPROXY, True), {"X-Capability-Denied": "haproxy:rw"})
        self.assertEqual(denial_headers(Capability.HAPROXY, False), {"X-Capability-Denied": "haproxy:ro"})


class MirrorTest(unittest.TestCase):
    """Карта путей ноды и панели обязана совпадать до символа."""

    @classmethod
    def setUpClass(cls):
        node_module = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "node", "app", "capabilities.py"
        )
        if not os.path.exists(node_module):
            raise unittest.SkipTest("node/app/capabilities.py недоступен (контейнер панели)")
        spec = importlib.util.spec_from_file_location("_node_capabilities", node_module)
        cls.node = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.node)

    def test_domain_names_match(self):
        self.assertEqual(
            {d.value for d in self.node.Domain},
            {c.value for c in Capability},
        )

    def test_prefix_map_matches(self):
        self.assertEqual(
            {path: domain.value for path, domain in self.node.DOMAIN_PREFIXES.items()},
            {path: capability.value for path, capability in DOMAIN_PREFIXES.items()},
        )

    def test_exempt_and_read_only_sets_match(self):
        self.assertEqual(self.node.ALWAYS_ALLOWED, ALWAYS_ALLOWED)
        self.assertEqual(self.node.READ_ONLY_WRITES, READ_ONLY_WRITES)

    def test_access_values_match(self):
        self.assertEqual(self.node.Access.NONE.value, ACCESS_NONE)
        self.assertEqual(self.node.Access.READ.value, ACCESS_READ)
        self.assertEqual(self.node.Access.WRITE.value, ACCESS_WRITE)
        self.assertEqual(self.node.DENIAL_ERROR, DENIAL_ERROR)

    def test_both_sides_route_paths_identically(self):
        for path in list(DOMAIN_PREFIXES) + [
            "/api/haproxy/firewall/rule/3",
            "/api/system/execute-stream",
            "/api/system/optimizations/apply",
            "/api/ssh/config/test",
            "/api/nothing",
        ]:
            with self.subTest(path=path):
                mine = resolve_route(path, "POST")[0]
                theirs = self.node.domain_for_path(self.node.normalize_path(path))
                if path in ALWAYS_ALLOWED:
                    continue
                self.assertEqual(
                    mine.value if mine else None,
                    theirs.value if theirs else None,
                )

    def test_node_published_map_is_readable_here(self):
        policy = self.node.parse_capabilities("readonly,haproxy")
        raw = json.dumps(policy.published())
        self.assertTrue(allows(raw, Capability.HAPROXY, write=True))
        self.assertTrue(allows(raw, Capability.SSH, write=False))
        self.assertFalse(allows(raw, Capability.SSH, write=True))


class CallSiteCoverageTest(unittest.TestCase):
    """Каждый файл, ходящий к ноде, должен быть осознанно рассмотрен.

    Реестр ниже — не список «где стоит проверка», а список «что мы разобрали».
    Новый файл с клиентом к ноде обязан попасть в один из двух списков.
    """

    GATED = {
        "routers/bulk_actions.py",
        # фоновый опрос порта sshd: гейт внутри proxy_to_node (раздел ssh), отказ = пропуск ноды
        "routers/firewall_profiles.py",
        "routers/proxy.py",
        "routers/ssh_security.py",
        "services/antiddos_manager.py",
        "services/blocklist_manager.py",
        "services/cpu_affinity_sync.py",
        "services/dnat_profile_sync.py",
        # гейт ensure_node_ready(SYSTEM, write) внутри каждого запроса к ноде
        "services/exit_proxy/node_client.py",
        "services/firewall_profile_sync.py",
        "services/haproxy_profile_sync.py",
        "services/metrics_collector.py",
        # гейт в роутере proxy: require_capability(SYSTEM) до старта задачи
        "services/network_transactions.py",
        "services/recovery_reconciler.py",
        "services/remnawave_nginx_sync.py",
        # гейт в роутере remnawave_install: require_capability(EXEC) до старта job
        "services/remnawave_node_install.py",
        "services/reserved_ports_sync.py",
        "services/ssh_manager.py",
        "services/time_sync.py",
        "services/torrent_blocker.py",
        "services/traffic_import.py",
        "services/wildcard_ssl.py",
        # гейт в роутере xray_test: require_capability(EXEC) до старта прогона
        "services/xray_test/node_runner.py",
    }

    # Ходят только по always-allowed адресам либо обязаны работать всегда:
    # без них ограниченную ноду нельзя ни установить, ни обновить, ни перевести
    # на общий сертификат, ни увидеть в списке живой.
    UNGATED = {
        "routers/servers.py",          # /api/version, кнопка «Тест»
        "routers/system.py",           # /api/system/versions
        "services/deploy_job_manager.py",  # установка новой ноды
        "services/http_client.py",     # сами клиенты
        "services/migration.py",       # /api/system/replace-node-cert
        "services/server_alerter.py",  # проба /api/metrics
    }

    def test_every_caller_is_accounted_for(self):
        app_dir = os.path.join(os.path.dirname(__file__), "..", "app")
        pattern = re.compile(r"get_node_client\(|get_node_apply_client\(|proxy_to_node\(")
        callers = set()
        for root, _, files in os.walk(app_dir):
            for name in files:
                if not name.endswith(".py"):
                    continue
                full = os.path.join(root, name)
                try:
                    with open(full, encoding="utf-8") as handle:
                        source = handle.read()
                except OSError:
                    continue
                if pattern.search(source):
                    callers.add(os.path.relpath(full, app_dir).replace(os.sep, "/"))

        unaccounted = callers - self.GATED - self.UNGATED
        self.assertEqual(
            unaccounted, set(),
            "новый файл ходит к ноде и не рассмотрен на предмет прав: "
            f"{sorted(unaccounted)}",
        )


if __name__ == "__main__":
    unittest.main()
