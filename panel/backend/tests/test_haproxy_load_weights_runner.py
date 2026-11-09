"""Tests for the background push of load-based weights to HAProxy relays.

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Исполнитель ходит на ноды каждые 10 секунд: лишняя отправка — нагрузка на весь
парк, пропущенная — релей с весами часовой давности. Нода без эндпоинта не
должна получать запрос каждый цикл, а устаревшая нагрузка — выводить сервер.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

from app.models import Server  # noqa: E402
from app.services import haproxy_load_weights_runner as runner_module  # noqa: E402
from app.services.haproxy_config import (  # noqa: E402
    BackendServer,
    BalancerOptions,
    HAProxyRule,
    ProfileOptions,
    get_config_generator,
)
from app.services.haproxy_load_weights import NodeLoad  # noqa: E402
from app.services.haproxy_load_weights_runner import (  # noqa: E402
    CycleInput,
    LoadWeightsRunner,
    RelayTarget,
    relay_select,
)

MBIT = 1_000_000 / 8
RELAY_ID = 10
PROFILE_ID = 1
OWNERS = {"10.0.0.1": 1, "10.0.0.2": 2, "10.0.0.3": 3}
CAPS = {1: 3000, 2: 10000, 3: 10000}


def profile_config() -> str:
    servers = [BackendServer(f"srv{i}", f"10.0.0.{i}", 443, weight=100) for i in (1, 2, 3)]
    rule = HAProxyRule(name="lb", rule_type="tcp", listen_port=443, target_ip="", target_port=0,
                       is_balancer=True, servers=servers,
                       balancer_options=BalancerOptions(algorithm="leastconn", load_weights=True))
    return get_config_generator().generate_full_config([rule], ProfileOptions())


PROFILE = profile_config()


def relay(node_version: str = "10.35.0", server_id: int = RELAY_ID, source_ips: list[str] | None = None,
          sync_status: str = "synced") -> Server:
    return Server(id=server_id, name=f"relay{server_id}", url=f"https://relay{server_id}.example:9100", api_key="key",
                  node_version=node_version, last_seen=datetime.now(timezone.utc),
                  node_capabilities=None, pki_enabled=False, proxy_url=None,
                  active_haproxy_profile_id=PROFILE_ID, haproxy_listen_ips=None,
                  haproxy_source_ips=json.dumps(source_ips) if source_ips else None,
                  haproxy_sync_status=sync_status)


def cycle_input(node_version: str = "10.35.0", relays: list[Server] | None = None) -> CycleInput:
    targets = [RelayTarget(server, PROFILE) for server in (relays or [relay(node_version)])]
    return CycleInput(relays=targets, profiles={PROFILE_ID: PROFILE}, owners=OWNERS, capacities=CAPS)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.captured: list[httpx.Request] = []
        self.status = 200
        self.clock = FakeClock()

        def handler(request: httpx.Request) -> httpx.Response:
            self.captured.append(request)
            return httpx.Response(self.status, json={"results": [], "guarded_backends": []})

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.runner = self.make_runner()

    async def asyncTearDown(self):
        await self.client.aclose()

    def make_runner(self, **kwargs) -> LoadWeightsRunner:
        return LoadWeightsRunner(client_factory=lambda server: self.client, clock=self.clock, **kwargs)

    def sent_weights(self) -> list[dict]:
        return [item for request in self.captured for item in json.loads(request.content)["weights"]]

    async def test_push_sends_put_with_ttl(self):
        await self.runner.push_cycle(cycle_input())
        [request] = self.captured
        self.assertEqual((request.method, request.url.path), ("PUT", "/api/haproxy/runtime/weights"))
        body = json.loads(request.content)
        self.assertEqual(body["ttl_seconds"], 180)
        self.assertIn({"backend": "backend_tcp_lb", "server": "srv1", "weight": 30}, body["weights"])

    async def test_unchanged_second_cycle_is_quiet(self):
        await self.runner.push_cycle(cycle_input())
        await self.runner.push_cycle(cycle_input())
        self.assertEqual(len(self.captured), 1)

    async def test_request_push_forces_resend(self):
        await self.runner.push_cycle(cycle_input())
        self.runner.request_push(RELAY_ID)
        await self.runner.push_cycle(cycle_input())
        self.assertEqual(len(self.captured), 2)

    async def test_404_marks_version_unsupported(self):
        self.status = 404
        await self.runner.push_cycle(cycle_input())
        await self.runner.push_cycle(cycle_input())
        self.assertEqual(len(self.captured), 1)

    async def test_unsupported_mark_expires(self):
        # На dev-канале обновлённая нода сохраняет номер версии — пометка не должна быть вечной
        self.status = 404
        await self.runner.push_cycle(cycle_input())
        self.status = 200
        self.clock.now += 601
        await self.runner.push_cycle(cycle_input())
        self.assertEqual(len(self.captured), 2)

    async def test_failed_push_is_retried(self):
        self.status = 500
        await self.runner.push_cycle(cycle_input())
        self.status = 200
        await self.runner.push_cycle(cycle_input())
        self.assertEqual(len(self.captured), 2)

    async def test_old_node_is_skipped(self):
        await self.runner.push_cycle(cycle_input(node_version="10.34.0"))
        self.assertEqual(self.captured, [])

    async def test_batches_over_limit(self):
        runner = self.make_runner(max_items_per_request=2)
        await runner.push_cycle(cycle_input())
        self.assertEqual(len(self.captured), 2)
        self.assertEqual(len(self.sent_weights()), 3)

    async def test_stale_loads_ignored(self):
        self.runner.observe({1: NodeLoad(0, 2900 * MBIT, 0)}, 10)
        self.clock.now = 31
        await self.runner.push_cycle(cycle_input())
        self.assertIn({"backend": "backend_tcp_lb", "server": "srv1", "weight": 30}, self.sent_weights())

    async def test_fresh_loads_drain_saturated_server(self):
        self.runner.observe({1: NodeLoad(0, 2900 * MBIT, 0)}, 10)
        self.clock.now = 29
        await self.runner.push_cycle(cycle_input())
        self.assertIn({"backend": "backend_tcp_lb", "server": "srv1", "weight": 0}, self.sent_weights())

    async def test_unsynced_relay_is_skipped(self):
        # На релее ещё старый конфиг: веса для нового попали бы не туда
        await self.runner.push_cycle(cycle_input(relays=[relay(sync_status="failed")]))
        self.assertEqual(self.captured, [])

    async def test_one_broken_relay_does_not_stop_the_others(self):
        def factory(server):
            if server.id == 1:
                raise RuntimeError("no mTLS context")
            return self.client
        runner = LoadWeightsRunner(client_factory=factory, clock=self.clock)
        await runner.push_cycle(cycle_input(relays=[relay(server_id=1), relay(server_id=2)]))
        self.assertEqual([r.url.host for r in self.captured], ["relay2.example"])

    async def test_relays_with_same_source_count_share_one_render(self):
        relays = [relay(server_id=1, source_ips=["192.0.2.10", "192.0.2.11"]),
                  relay(server_id=2, source_ips=["198.51.100.5", "198.51.100.6"]),
                  relay(server_id=3)]
        with mock.patch.object(runner_module, "render_for_server", wraps=runner_module.render_for_server) as render:
            await self.runner.push_cycle(cycle_input(relays=relays))
        self.assertEqual(render.call_count, 2)
        by_host = {r.url.host: {w["server"] for w in json.loads(r.content)["weights"]} for r in self.captured}
        self.assertIn("srv1_o2", by_host["relay1.example"])
        self.assertIn("srv1_o2", by_host["relay2.example"])
        self.assertNotIn("srv1_o2", by_host["relay3.example"])

    def test_relay_query_skips_heavy_metrics(self):
        sql = str(relay_select().compile(dialect=postgresql.dialect()))
        self.assertNotIn("last_metrics", sql)
        self.assertIn("node_version", sql)

    async def test_profile_view_for_rule_form(self):
        await self.runner.push_cycle(cycle_input())
        view = self.runner.profile_view(PROFILE_ID)
        self.assertEqual([line["weight"] for line in view["rules"]["lb"]], [30, 100, 100])
        self.assertIsNotNone(view["updated_at"])
        self.assertEqual(self.runner.profile_view(999), {"rules": {}, "updated_at": view["updated_at"]})


if __name__ == "__main__":
    unittest.main()
