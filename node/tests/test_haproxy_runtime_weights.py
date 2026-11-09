"""Tests for runtime HAProxy weights set by the panel.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Веса меняются на лету без reload. Две вещи нельзя проморгать: бэкенд, где
не осталось ни одного живого сервера с весом, отрезает всех новых клиентов, а
веса замолчавшей панели не должны жить вечно.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.haproxy_manager import HAProxyManager, StatsSocketUnavailableError  # noqa: E402
from app.services.haproxy_runtime_weights import (  # noqa: E402
    RuntimeWeights,
    SocketPermissionError,
    WeightItem,
)

BACKEND = "backend_tcp_lb"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def proxy(*servers) -> list[dict]:
    return [{"name": BACKEND, "servers": [
        {"name": name, "status": status, "weight": weight, "backup": False} for name, status, weight in servers
    ]}]


class RuntimeWeightsTestCase(unittest.TestCase):
    def setUp(self):
        self.commands: list[str] = []
        self.responses: dict[str, str] = {}
        self.proxies: list[dict] = []
        self.clock = FakeClock()

        def run_command(command: str) -> str:
            self.commands.append(command)
            return self.responses.get(command, "\n")

        self.rw = RuntimeWeights(run_command, lambda: self.proxies, clock=self.clock)


class ApplyTests(RuntimeWeightsTestCase):
    def test_sends_one_command_per_item(self):
        self.rw.apply([WeightItem(BACKEND, "srv1", 6), WeightItem(BACKEND, "srv2", 100)], 180)
        self.assertEqual(self.commands, [f"set server {BACKEND}/srv1 weight 6", f"set server {BACKEND}/srv2 weight 100"])

    def test_results_report_each_item(self):
        result = self.rw.apply([WeightItem(BACKEND, "srv1", 6)], 180)
        self.assertEqual([(r.server, r.ok, r.error) for r in result.results], [("srv1", True, None)])

    def test_unknown_server_is_item_error(self):
        self.responses[f"set server {BACKEND}/srv9 weight 5"] = "No such server.\n"
        result = self.rw.apply([WeightItem(BACKEND, "srv9", 5)], 180)
        self.assertEqual((result.results[0].ok, result.results[0].error), (False, "No such server."))

    def test_permission_denied_aborts(self):
        self.responses[f"set server {BACKEND}/srv1 weight 5"] = "Permission denied\n"
        with self.assertRaises(SocketPermissionError):
            self.rw.apply([WeightItem(BACKEND, "srv1", 5)], 180)

    def test_guard_restores_when_no_live_weighted_server(self):
        self.proxies[:] = proxy(("srv1", "UP", 0), ("srv2", "DOWN", 100), ("srv3", "UP 1/3", 0))
        result = self.rw.apply([WeightItem(BACKEND, "srv1", 0)], 180)
        self.assertEqual(result.guarded_backends, [BACKEND])
        self.assertIn(f"set server {BACKEND}/srv1 weight 100%", self.commands)
        self.assertIn(f"set server {BACKEND}/srv3 weight 100%", self.commands)
        self.assertNotIn(f"set server {BACKEND}/srv2 weight 100%", self.commands)

    def test_guard_checks_before_zeroing_the_last_live_server(self):
        self.proxies[:] = proxy(("srv1", "UP", 100), ("srv2", "DOWN", 100))
        result = self.rw.apply([WeightItem(BACKEND, "srv1", 0)], 180)
        self.assertEqual(result.guarded_backends, [BACKEND])
        self.assertNotIn(f"set server {BACKEND}/srv1 weight 0", self.commands)
        self.assertFalse(result.results[0].ok)

    def test_zero_weight_server_getting_weight_counts_as_live(self):
        self.proxies[:] = proxy(("srv1", "UP", 0), ("srv2", "DOWN", 100))
        result = self.rw.apply([WeightItem(BACKEND, "srv1", 50)], 180)
        self.assertEqual(result.guarded_backends, [])
        self.assertIn(f"set server {BACKEND}/srv1 weight 50", self.commands)

    def test_admin_drained_server_is_not_live(self):
        # DRAIN — ручной слив (set server state drain): новых клиентов сервер не берёт при любом весе
        self.proxies[:] = proxy(("srv1", "UP", 100), ("srv2", "DRAIN", 0))
        result = self.rw.apply([WeightItem(BACKEND, "srv1", 0), WeightItem(BACKEND, "srv2", 100)], 180)
        self.assertEqual(result.guarded_backends, [BACKEND])
        self.assertIn(f"set server {BACKEND}/srv1 weight 100%", self.commands)
        self.assertNotIn(f"set server {BACKEND}/srv2 weight 100%", self.commands)

    def test_guard_quiet_with_live_weighted_server(self):
        self.proxies[:] = proxy(("srv1", "UP", 0), ("srv2", "no check", 100))
        self.assertEqual(self.rw.apply([WeightItem(BACKEND, "srv1", 0)], 180).guarded_backends, [])

    def test_guard_ignores_backup_servers(self):
        self.proxies[:] = [{"name": BACKEND, "servers": [
            {"name": "srv1", "status": "UP", "weight": 0, "backup": False},
            {"name": "bk", "status": "UP", "weight": 100, "backup": True},
        ]}]
        self.assertEqual(self.rw.apply([WeightItem(BACKEND, "srv1", 0)], 180).guarded_backends, [BACKEND])


class LeaseTests(RuntimeWeightsTestCase):
    def test_expiry_restores_config_weight(self):
        self.rw.apply([WeightItem(BACKEND, "srv1", 6)], 180)
        self.clock.now += 179
        self.assertEqual(self.rw.expire_due(), [])
        self.clock.now += 2
        self.assertEqual(self.rw.expire_due(), [(BACKEND, "srv1")])
        self.assertEqual(self.commands[-1], f"set server {BACKEND}/srv1 weight 100%")

    def test_reapply_extends_lease(self):
        self.rw.apply([WeightItem(BACKEND, "srv1", 6)], 180)
        self.clock.now += 170
        self.rw.apply([WeightItem(BACKEND, "srv1", 6)], 180)
        self.clock.now += 20
        self.assertEqual(self.rw.expire_due(), [])

    def test_failed_item_gets_no_lease(self):
        self.responses[f"set server {BACKEND}/srv9 weight 5"] = "No such server.\n"
        self.rw.apply([WeightItem(BACKEND, "srv9", 5)], 10)
        self.clock.now += 11
        self.assertEqual(self.rw.expire_due(), [])

    def test_vanished_server_lease_is_dropped(self):
        self.rw.apply([WeightItem(BACKEND, "srv1", 6)], 10)
        self.responses[f"set server {BACKEND}/srv1 weight 100%"] = "No such server.\n"
        self.clock.now += 11
        self.assertEqual(self.rw.expire_due(), [(BACKEND, "srv1")])
        self.clock.now += 11
        self.assertEqual(self.rw.expire_due(), [])

    def test_hung_socket_stops_the_round(self):
        self.rw.apply([WeightItem(BACKEND, "srv1", 6), WeightItem(BACKEND, "srv2", 6)], 10)
        self.rw._run_command = mock.Mock(side_effect=TimeoutError("socket timed out"))
        self.clock.now += 11
        self.assertEqual(self.rw.expire_due(), [])
        self.assertEqual(self.rw._run_command.call_count, 1)

    def test_unavailable_socket_keeps_lease(self):
        self.rw.apply([WeightItem(BACKEND, "srv1", 6)], 10)
        self.rw._run_command = mock.Mock(side_effect=StatsSocketUnavailableError("gone"))
        self.clock.now += 11
        self.assertEqual(self.rw.expire_due(), [])
        self.rw._run_command = lambda command: "\n"
        self.assertEqual(self.rw.expire_due(), [(BACKEND, "srv1")])


class PeriodicGuardTests(RuntimeWeightsTestCase):
    def test_restores_when_the_live_server_goes_down_later(self):
        self.proxies[:] = proxy(("srv1", "UP", 100), ("srv2", "UP", 100))
        self.rw.apply([WeightItem(BACKEND, "srv1", 0)], 180)
        self.proxies[:] = proxy(("srv1", "UP", 0), ("srv2", "DOWN", 100))
        self.assertEqual(self.rw.check_guard(), [BACKEND])
        self.assertEqual(self.commands[-1], f"set server {BACKEND}/srv1 weight 100%")

    def test_quiet_while_a_live_server_has_weight(self):
        self.proxies[:] = proxy(("srv1", "UP", 100), ("srv2", "UP", 100))
        self.rw.apply([WeightItem(BACKEND, "srv1", 0)], 180)
        self.proxies[:] = proxy(("srv1", "UP", 0), ("srv2", "UP", 100))
        self.assertEqual(self.rw.check_guard(), [])

    def test_no_stat_read_without_panel_weights(self):
        reads = mock.Mock(return_value=[])
        self.rw._read_proxies = reads
        self.assertEqual(self.rw.check_guard(), [])
        reads.assert_not_called()


SHOW_STAT = (
    "# pxname,svname,scur,status,weight,bck,mode,\n"
    f"{BACKEND},srv1,1,UP,6,0,tcp,\n"
    f"{BACKEND},BACKEND,1,UP,6,0,tcp,\n"
)


class ManagerRuntimeCommandTests(unittest.TestCase):
    def manager(self, config: str) -> HAProxyManager:
        manager = HAProxyManager.__new__(HAProxyManager)
        manager._read_config = lambda: config
        return manager

    def test_no_stats_socket_raises(self):
        with self.assertRaises(StatsSocketUnavailableError):
            self.manager("global\n    no log\n").runtime_command("show stat")

    def test_falls_through_missing_candidates(self):
        manager = self.manager("global\n    stats socket /var/run/haproxy.sock mode 660 level admin\n")
        replies = [FileNotFoundError(), "\n"]

        def query(command, path):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with mock.patch.object(HAProxyManager, "_query_stats_socket", side_effect=query):
            self.assertEqual(manager.runtime_command("set server b/s weight 5"), "\n")

    def test_read_live_proxies_parses_fresh_stat(self):
        manager = self.manager("global\n    stats socket /run/haproxy.sock level admin\n")
        with mock.patch.object(HAProxyManager, "_query_stats_socket", return_value=SHOW_STAT):
            [proxy] = manager.read_live_proxies()
        self.assertEqual([(s["name"], s["status"], s["weight"]) for s in proxy["servers"]], [("srv1", "UP", 6)])


if __name__ == "__main__":
    unittest.main()
