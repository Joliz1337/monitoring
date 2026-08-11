"""Tests for NODE_CAPABILITIES parsing and enforcement.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Три вещи, ради которых тест существует. Первая — форма публикуемой карты:
панель считает отсутствующий ключ разрешением, поэтому неполная карта тихо
обнулила бы весь механизм. Вторая — always-allowed: закрытый /api/metrics или
/health превращает живую ноду в «мёртвую» для панели, а /api/system/update — в
неремонтируемую удалённо. Третья — разбор пути: /api/system/execute-stream
обязан попадать в exec, а не в system.
"""

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.capabilities import (  # noqa: E402
    ALWAYS_ALLOWED,
    DENIAL_ERROR,
    DOMAIN_PREFIXES,
    READ_ONLY_WRITES,
    Access,
    CapabilityMiddleware,
    DenyLog,
    Domain,
    domain_for_path,
    normalize_path,
    parse_capabilities,
)


class GrammarTests(unittest.TestCase):
    def test_empty_value_leaves_node_open(self):
        for raw in ["", "   ", None, '""', "''", ' " " ']:
            with self.subTest(raw=raw):
                policy = parse_capabilities(raw)
                self.assertTrue(policy.unrestricted)
                self.assertIsNone(policy.published())
                self.assertIsNone(policy.check("POST", "/api/system/execute"))

    def test_quotes_around_value_are_stripped(self):
        policy = parse_capabilities('"readonly,haproxy"')
        self.assertFalse(policy.unrestricted)
        self.assertEqual(policy.grants[Domain.HAPROXY], Access.WRITE)
        self.assertEqual(policy.grants[Domain.SSH], Access.READ)

    def test_presets(self):
        full = parse_capabilities("full")
        self.assertFalse(full.unrestricted)
        self.assertTrue(all(v == Access.WRITE for v in full.published().values()))

        readonly = parse_capabilities("readonly")
        self.assertTrue(all(v == Access.READ for v in readonly.published().values()))

        monitoring = parse_capabilities("monitoring")
        self.assertEqual(monitoring.published()[Domain.TRAFFIC.value], "ro")
        self.assertEqual(monitoring.published()[Domain.SYSTEM.value], "ro")
        self.assertEqual(monitoring.published()[Domain.HAPROXY.value], "no")

    def test_preset_combines_with_domain(self):
        policy = parse_capabilities("readonly,haproxy")
        self.assertEqual(policy.grants[Domain.HAPROXY], Access.WRITE)
        self.assertEqual(policy.grants[Domain.FIREWALL], Access.READ)

    def test_separators_and_case_do_not_matter(self):
        expected = parse_capabilities("traffic,haproxy:ro").published()
        for raw in ["TRAFFIC, HAPROXY:RO", "traffic haproxy:ro", "traffic,,  haproxy:ro ",
                    "traffic\thaproxy:ro"]:
            with self.subTest(raw=raw):
                self.assertEqual(parse_capabilities(raw).published(), expected)

    def test_access_only_rises_regardless_of_order(self):
        # Иначе смысл строки зависел бы от порядка слов в ней
        direct = parse_capabilities("haproxy,haproxy:ro")
        reversed_order = parse_capabilities("haproxy:ro,haproxy")
        self.assertEqual(direct.grants[Domain.HAPROXY], Access.WRITE)
        self.assertEqual(reversed_order.grants[Domain.HAPROXY], Access.WRITE)
        self.assertEqual(parse_capabilities("full,ssh:ro").grants[Domain.SSH], Access.WRITE)


class PublishedShapeTests(unittest.TestCase):
    """Панель читает отсутствующий ключ как разрешение — карта обязана быть полной."""

    def test_map_always_lists_every_domain(self):
        for raw in ["monitoring", "readonly", "haproxy", "redonly", "ssh:ro"]:
            with self.subTest(raw=raw):
                published = parse_capabilities(raw).published()
                self.assertEqual(set(published), {d.value for d in Domain})
                self.assertTrue(set(published.values()) <= {"no", "ro", "rw"})

    def test_metrics_never_appears_in_the_map(self):
        self.assertNotIn("metrics", parse_capabilities("metrics").published())
        self.assertNotIn("metrics", parse_capabilities("full").published())

    def test_garbage_closes_everything_but_still_publishes(self):
        policy = parse_capabilities("redonly")
        self.assertIsNotNone(policy.published())
        self.assertEqual(set(policy.published().values()), {"no"})


class UnknownTokenTests(unittest.TestCase):
    def test_typo_is_reported_and_ignored(self):
        policy = parse_capabilities("readonly,haproxi")
        self.assertEqual(policy.unknown_tokens, ("haproxi",))
        self.assertEqual(policy.published(), parse_capabilities("readonly").published())

    def test_metrics_token_is_accepted_silently(self):
        for raw in ["metrics", "metrics:ro"]:
            with self.subTest(raw=raw):
                self.assertEqual(parse_capabilities(raw).unknown_tokens, ())

    def test_bad_suffixes(self):
        for raw in ["ssh:rw", "ssh:write", "ssh:", "readonly:ro", "full:ro"]:
            with self.subTest(raw=raw):
                self.assertEqual(parse_capabilities(raw).unknown_tokens, (raw.lower(),))


class PathRoutingTests(unittest.TestCase):
    def test_firewall_wins_over_haproxy_prefix(self):
        self.assertEqual(domain_for_path("/api/haproxy/firewall/rules"), Domain.FIREWALL)
        self.assertEqual(domain_for_path("/api/haproxy/firewall/rule/3"), Domain.FIREWALL)
        self.assertEqual(domain_for_path("/api/firewall/profile/apply"), Domain.FIREWALL)
        self.assertEqual(domain_for_path("/api/haproxy/rules/my-rule"), Domain.HAPROXY)

    def test_exec_is_separate_from_system(self):
        self.assertEqual(domain_for_path("/api/system/execute"), Domain.EXEC)
        self.assertEqual(domain_for_path("/api/system/execute-stream"), Domain.EXEC)
        self.assertEqual(domain_for_path("/api/system/time-sync"), Domain.SYSTEM)
        self.assertEqual(domain_for_path("/api/system/optimizations/apply"), Domain.SYSTEM)

    def test_unmapped_paths_are_left_to_the_router(self):
        for path in ["/api/systemfoo", "/docs", "/", "/api"]:
            with self.subTest(path=path):
                self.assertIsNone(domain_for_path(path))

    def test_normalization(self):
        self.assertEqual(normalize_path("/api/ssh/"), "/api/ssh")
        self.assertEqual(normalize_path("/api/ssh?x=1"), "/api/ssh")
        self.assertEqual(normalize_path("/"), "/")


class AlwaysAllowedTests(unittest.TestCase):
    def setUp(self):
        self.policy = parse_capabilities("redonly")  # мусор: закрыто всё, что можно

    def test_every_always_allowed_path_survives(self):
        for path in ALWAYS_ALLOWED:
            with self.subTest(path=path):
                self.assertIsNone(self.policy.check("GET", path))
                self.assertIsNone(self.policy.check("POST", path))

    def test_trailing_slash_does_not_bypass_the_exemption(self):
        self.assertIsNone(self.policy.check("POST", "/api/system/update/"))

    def test_neighbours_of_exempt_paths_are_still_closed(self):
        self.assertIsNotNone(self.policy.check("POST", "/api/system/optimizations/apply"))
        self.assertIsNotNone(self.policy.check("POST", "/api/system/updateXYZ"))


class AccessLevelTests(unittest.TestCase):
    def test_read_only_domain(self):
        policy = parse_capabilities("ssh:ro")
        self.assertIsNone(policy.check("GET", "/api/ssh/config"))
        denial = policy.check("POST", "/api/ssh/config")
        self.assertIsNotNone(denial)
        self.assertEqual(denial.domain, Domain.SSH)
        self.assertEqual(denial.required, Access.WRITE)

    def test_closed_domain_denies_reads_too(self):
        denial = parse_capabilities("haproxy").check("GET", "/api/ssh/config")
        self.assertEqual(denial.required, Access.READ)

    def test_read_only_writes_pass_under_ro(self):
        policy = parse_capabilities("readonly")
        for path in READ_ONLY_WRITES:
            with self.subTest(path=path):
                self.assertIsNone(policy.check("POST", path))

    def test_write_methods(self):
        policy = parse_capabilities("ipset:ro")
        for method in ["POST", "PUT", "DELETE", "PATCH"]:
            with self.subTest(method=method):
                self.assertIsNotNone(policy.check(method, "/api/ipset/remove"))
        self.assertIsNone(policy.check("HEAD", "/api/ipset/status"))


def collect_paths(routes, prefix: str = "") -> set:
    """Пути приложения. FastAPI держит подключённые роутеры обёрткой, а не
    списком маршрутов, поэтому дерево обходится рекурсивно."""
    found = set()
    for route in routes:
        path = getattr(route, "path", None)
        if path:
            found.add(prefix + path)
            continue
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            found |= collect_paths(included.routes, prefix + getattr(context, "prefix", ""))
    return found


class RouteCoverageTests(unittest.TestCase):
    """Новый эндпоинт с незнакомым префиксом — незакрываемая дыра. Ловим здесь."""

    @classmethod
    def setUpClass(cls):
        try:
            from app.main import app
        except Exception as exc:  # рантайм агента не установлен
            raise unittest.SkipTest(f"app.main is not importable: {exc}")
        cls.paths = sorted(
            path for path in collect_paths(app.routes)
            if path.startswith(("/api/", "/health"))
        )
        # Пустой список означал бы, что обход сломался и тест ничего не проверяет
        if len(cls.paths) < 50:
            raise AssertionError(f"обход маршрутов вернул только {len(cls.paths)} путей")

    def test_every_route_is_either_exempt_or_mapped(self):
        for path in self.paths:
            with self.subTest(path=path):
                normalized = normalize_path(path)
                self.assertTrue(
                    normalized in ALWAYS_ALLOWED or domain_for_path(normalized) is not None,
                    f"{path} не попадает ни в always-allowed, ни в домен",
                )

    def test_exempt_paths_and_read_only_writes_still_exist(self):
        existing = {normalize_path(p) for p in self.paths}
        # /api/metrics регистрируется как /api/metrics с пустым path у роутера
        for path in ALWAYS_ALLOWED | READ_ONLY_WRITES:
            with self.subTest(path=path):
                self.assertIn(path, existing)


class MiddlewareTests(unittest.TestCase):
    def _call(self, policy, method, path):
        sent = []
        called = {"inner": False}

        async def inner(scope, receive, send):
            called["inner"] = True
            await send({"type": "http.response.start", "status": 200, "headers": []})

        async def send(message):
            sent.append(message)

        middleware = CapabilityMiddleware(inner, policy)
        scope = {"type": "http", "method": method, "path": path}
        asyncio.run(middleware(scope, None, send))
        return called["inner"], sent

    def test_allowed_request_reaches_the_app(self):
        inner_called, _ = self._call(parse_capabilities("ssh"), "POST", "/api/ssh/config")
        self.assertTrue(inner_called)

    def test_denied_request_never_reaches_the_app(self):
        inner_called, sent = self._call(parse_capabilities("monitoring"), "POST", "/api/ssh/config")
        self.assertFalse(inner_called)
        self.assertEqual(sent[0]["status"], 403)
        headers = {name.decode(): value.decode() for name, value in sent[0]["headers"]}
        self.assertEqual(headers["x-capability-denied"], "ssh:rw")

    def test_denial_body_shape(self):
        _, sent = self._call(parse_capabilities("monitoring"), "POST", "/api/ssh/config")
        body = json.loads(sent[1]["body"])
        # detail строкой: и панель, и фронт печатают его как текст ошибки
        self.assertIsInstance(body["detail"], str)
        self.assertEqual(body["error"], DENIAL_ERROR)
        self.assertEqual(body["domain"], "ssh")
        self.assertEqual(body["access"], "rw")
        self.assertEqual(set(body["capabilities"]), {d.value for d in Domain})

    def test_non_http_scope_passes_through(self):
        received = []

        async def inner(scope, receive, send):
            received.append(scope["type"])

        middleware = CapabilityMiddleware(inner, parse_capabilities("monitoring"))
        asyncio.run(middleware({"type": "lifespan"}, None, None))
        self.assertEqual(received, ["lifespan"])


class DenyLogTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.log = DenyLog(clock=lambda: self.now)
        self.denial = parse_capabilities("monitoring").check("POST", "/api/ssh/config")

    def test_repeated_denials_log_once_per_interval(self):
        with self.assertLogs("app.capabilities", level="WARNING") as captured:
            for _ in range(5):
                self.log.record(self.denial)
        self.assertEqual(len(captured.output), 1)

        self.now += DenyLog.INTERVAL_SECONDS + 1
        with self.assertLogs("app.capabilities", level="WARNING") as captured:
            self.log.record(self.denial)
        self.assertIn("suppressed=4", captured.output[0])

    def test_domains_are_throttled_independently(self):
        other = parse_capabilities("monitoring").check("POST", "/api/ipset/add")
        with self.assertLogs("app.capabilities", level="WARNING") as captured:
            self.log.record(self.denial)
            self.log.record(other)
        self.assertEqual(len(captured.output), 2)


class MapConsistencyTests(unittest.TestCase):
    def test_every_domain_has_at_least_one_prefix(self):
        mapped = set(DOMAIN_PREFIXES.values())
        self.assertEqual(mapped, set(Domain))

    def test_exempt_and_domain_maps_do_not_overlap(self):
        for path in ALWAYS_ALLOWED:
            with self.subTest(path=path):
                self.assertNotIn(path, DOMAIN_PREFIXES)


if __name__ == "__main__":
    unittest.main()
