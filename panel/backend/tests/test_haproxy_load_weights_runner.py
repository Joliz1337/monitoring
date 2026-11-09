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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402

from app.models import Server  # noqa: E402
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


def relay(node_version: str = "10.35.0") -> Server:
    return Server(id=RELAY_ID, name="relay", url="https://relay.example:9100", api_key="key",
                  node_version=node_version, last_seen=datetime.now(timezone.utc),
                  node_capabilities=None, pki_enabled=False, proxy_url=None,
                  active_haproxy_profile_id=PROFILE_ID, haproxy_listen_ips=None, haproxy_source_ips=None)


def cycle_input(node_version: str = "10.35.0") -> CycleInput:
    return CycleInput(relays=[RelayTarget(relay(node_version), PROFILE)], profiles={PROFILE_ID: PROFILE},
                      owners=OWNERS, capacities=CAPS)


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

    async def test_profile_view_for_rule_form(self):
        await self.runner.push_cycle(cycle_input())
        view = self.runner.profile_view(PROFILE_ID)
        self.assertEqual([line["weight"] for line in view["rules"]["lb"]], [30, 100, 100])
        self.assertIsNotNone(view["updated_at"])
        self.assertEqual(self.runner.profile_view(999), {"rules": {}, "updated_at": view["updated_at"]})


if __name__ == "__main__":
    unittest.main()
