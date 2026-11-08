"""Коммит ветки с GitHub для сверки клона через зеркало и как он доезжает до апдейтеров.

Панель спрашивает SHA у самого GitHub (api.github.com) и отдаёт его апдейтеру
ноды полем expected_commit, а своему апдейтеру — в текст скрипта. Нет SHA —
апдейтеры не идут на зеркало, но обновление с github.com не отменяется.

Голый unittest, без PostgreSQL и без сети.
Запуск из panel/backend:  python -m unittest tests.test_upstream_commit
"""

import asyncio
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.routers import proxy as proxy_router
    from app.routers import system as system_router
    from app.services import update_source, upstream_commit
except ImportError as e:  # рантайм панели не установлен
    raise unittest.SkipTest(f"upstream_commit requires the panel runtime: {e}")

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"


class FakeGitHub:
    def __init__(self, status=200, text=SHA, error=None, delay=0.0):
        self.status = status
        self.text = text
        self.error = error
        self.delay = delay
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.status, text=self.text)


class ResolveTestCase(unittest.TestCase):
    def setUp(self):
        upstream_commit._cache.clear()
        self.github = FakeGitHub()
        self.clock = 1000.0
        patches = (
            patch.object(upstream_commit, "get_external_client", lambda: self.github),
            patch.object(upstream_commit.time, "monotonic", lambda: self.clock),
            # каждый asyncio.run — свой цикл, а занятый лок привязывается к циклу
            patch.object(upstream_commit, "_lock", asyncio.Lock()),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(upstream_commit._cache.clear)

    def resolve(self, ref="dev"):
        return asyncio.run(upstream_commit.resolve(ref))


class FetchTests(ResolveTestCase):

    def test_returns_sha_from_github_api(self):
        self.assertEqual(self.resolve("dev"), SHA)
        url, kwargs = self.github.calls[0]
        self.assertEqual(url, "https://api.github.com/repos/Joliz1337/monitoring/commits/dev")
        self.assertEqual(kwargs["headers"]["Accept"], "application/vnd.github.sha")
        self.assertIn("timeout", kwargs)

    def test_body_that_is_not_a_full_sha_is_rejected(self):
        for body in ("<html>rate limited</html>", SHA.upper(), SHA[:12], SHA + "0", ""):
            with self.subTest(body=body):
                upstream_commit._cache.clear()
                self.github.text = body
                self.assertIsNone(self.resolve())

    def test_trailing_newline_is_tolerated(self):
        self.github.text = SHA + "\n"
        self.assertEqual(self.resolve(), SHA)

    def test_error_status_gives_no_commit(self):
        for status in (403, 404, 500):
            with self.subTest(status=status):
                upstream_commit._cache.clear()
                self.github.status = status
                self.assertIsNone(self.resolve())

    def test_network_error_gives_no_commit(self):
        self.github.error = httpx.ConnectError("unreachable")
        self.assertIsNone(self.resolve())

    def test_unsafe_ref_is_not_sent_to_github(self):
        for ref in ("", "../../users/x", "main;id", "-x", "a b", None, 123):
            with self.subTest(ref=ref):
                self.assertIsNone(self.resolve(ref))
        self.assertEqual(self.github.calls, [])


class DeadlineTests(unittest.TestCase):
    """Своя установка без подмены часов: на замороженных часах asyncio таймер не сработает."""

    def test_hung_github_gives_no_commit_within_total_deadline(self):
        # Таймаут httpx — на каждую фазу запроса: GitHub, отдающий ответ по байту,
        # держал бы общий лок, а за ним и всю волну обновлений
        github = FakeGitHub(delay=30)
        upstream_commit._cache.clear()
        self.addCleanup(upstream_commit._cache.clear)
        with (
            patch.object(upstream_commit, "get_external_client", lambda: github),
            patch.object(upstream_commit, "_lock", asyncio.Lock()),
            patch.object(upstream_commit, "GITHUB_TOTAL_TIMEOUT_SEC", 0.05),
        ):
            started = time.perf_counter()
            commit = asyncio.run(upstream_commit.resolve("dev"))
        self.assertIsNone(commit)
        self.assertLess(time.perf_counter() - started, 5)


class CacheTests(ResolveTestCase):

    def test_bulk_update_asks_github_once(self):
        async def burst():
            return await asyncio.gather(*(upstream_commit.resolve("dev") for _ in range(50)))

        self.assertEqual(asyncio.run(burst()), [SHA] * 50)
        self.assertEqual(len(self.github.calls), 1)

    def test_refetches_after_ttl(self):
        self.resolve()
        self.github.text = OTHER_SHA
        self.clock += upstream_commit.COMMIT_TTL_SEC + 1
        self.assertEqual(self.resolve(), OTHER_SHA)
        self.assertEqual(len(self.github.calls), 2)

    def test_failure_is_remembered_briefly(self):
        self.github.status = 403
        self.assertIsNone(self.resolve())
        self.assertIsNone(self.resolve())
        self.assertEqual(len(self.github.calls), 1)
        self.github.status = 200
        self.clock += upstream_commit.COMMIT_FAILED_TTL_SEC + 1
        self.assertEqual(self.resolve(), SHA)

    def test_refs_are_cached_separately(self):
        self.resolve("main")
        self.resolve("dev")
        self.assertEqual(len(self.github.calls), 2)


class NodeUpdateRequestTests(unittest.IsolatedAsyncioTestCase):

    async def trigger(self, data, commit):
        server = SimpleNamespace(id=1, node_version="10.35.0")
        node_call = AsyncMock(return_value={"success": True, "attempt_id": "a1"})
        with patch.object(proxy_router, "get_server_by_id", AsyncMock(return_value=server)), \
                patch.object(proxy_router, "proxy_request", node_call), \
                patch.object(proxy_router.node_update_watcher, "track", AsyncMock()), \
                patch.object(proxy_router.upstream_commit, "resolve", AsyncMock(return_value=commit)) as resolve:
            await proxy_router.trigger_node_update(1, data, db=MagicMock(), _={})
        return node_call.call_args.kwargs["json_data"], resolve

    async def test_resolved_commit_is_sent_to_node(self):
        sent, resolve = await self.trigger({"target_version": "dev"}, SHA)
        self.assertEqual(sent["expected_commit"], SHA)
        resolve.assert_awaited_once_with("dev")

    async def test_without_commit_node_gets_no_field(self):
        sent, _ = await self.trigger({"target_version": "dev"}, None)
        self.assertNotIn("expected_commit", sent)

    async def test_commit_from_the_browser_is_not_trusted(self):
        sent, _ = await self.trigger({"target_version": "dev", "expected_commit": OTHER_SHA}, None)
        self.assertNotIn("expected_commit", sent)
        sent, _ = await self.trigger({"target_version": "dev", "expected_commit": OTHER_SHA}, SHA)
        self.assertEqual(sent["expected_commit"], SHA)


class PanelUpdaterScriptTests(unittest.TestCase):

    def test_script_verifies_mirror_clone_against_commit(self):
        script = system_router._panel_updater_script("dev", SHA)
        self.assertIn(update_source.MIRROR_URL, script)
        self.assertIn(SHA, script)

    def test_script_without_commit_does_not_use_mirror(self):
        self.assertNotIn("ghfast", system_router._panel_updater_script("dev", None))

    def test_compose_comes_from_updater_image(self):
        # Плагин compose уже в образе docker:cli (сверен по sha256 при сборке образа) —
        # своя загрузка бинарника подменила бы его неподписанным
        script = system_router._panel_updater_script("dev", SHA)
        self.assertNotIn("compose/releases", script)
        self.assertNotIn("cli-plugins", script)
        self.assertIn("docker compose version", script)

    def test_update_runs_with_commit_from_github(self):
        client = MagicMock()
        client.containers.run.return_value.wait.return_value = {"StatusCode": 0}
        client.containers.run.return_value.logs.return_value = b""
        with patch.object(system_router, "get_docker_client", return_value=client), \
                patch.object(system_router.upstream_commit, "resolve", AsyncMock(return_value=SHA)) as resolve:
            asyncio.run(system_router.run_panel_update_in_container("dev"))
        resolve.assert_awaited_once_with("dev")
        command = client.containers.run.call_args.kwargs["command"]
        self.assertIn(SHA, command[-1])


if __name__ == "__main__":
    unittest.main()
