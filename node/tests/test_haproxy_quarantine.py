"""Tests for taking backend addresses out of HAProxy balancing (services/haproxy_quarantine).

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Панель присылает адреса, до которых релей не доходит, нода переводит их серверы
в обслуживание. Цена ошибки двойная: правило, оставшееся без серверов, отрезает
всех клиентов, а сервер, который забыли вернуть, простаивает навсегда.
"""

import asyncio
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import haproxy_quarantine as quarantine_module  # noqa: E402
from app.services.haproxy_quarantine import Quarantine, SkipReason  # noqa: E402
from app.services.haproxy_runtime_weights import SocketPermissionError  # noqa: E402

LB = "backend_tcp_lb"
OTHER = "backend_tcp_other"
A = ("10.0.0.1", 443)
B = ("10.0.0.2", 443)
C = ("10.0.0.3", 443)
TTL = 180


def server(name: str, target: tuple[str, int], status: str = "UP", weight: int = 1,
           backup: bool = False, scur: int = 0) -> dict:
    return {"name": name, "status": status, "weight": weight, "backup": backup,
            "addr": f"{target[0]}:{target[1]}", "scur": scur}


class FakeClock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeHAProxy:
    """Stats socket с памятью: команды меняют статус строк, как это делает HAProxy."""

    def __init__(self, *proxies: tuple[str, list[dict]]):
        self.proxies = [{"name": name, "servers": servers} for name, servers in proxies]
        self.commands: list[str] = []
        self.replies: dict[str, str] = {}
        self.broken: bool = False
        self.reads = 0

    def read(self) -> list[dict]:
        self.reads += 1
        if self.broken:
            raise OSError("stats socket is gone")
        return [{**proxy, "servers": [dict(row) for row in proxy["servers"]]} for proxy in self.proxies]

    def run(self, command: str) -> str:
        if self.broken:
            raise OSError("stats socket is gone")
        self.commands.append(command)
        if command in self.replies:
            return self.replies[command]
        state = re.fullmatch(r"set server (\S+)/(\S+) state (maint|ready)", command)
        if state:
            self.row(state[1], state[2])["status"] = "MAINT" if state[3] == "maint" else "UP"
        dropped = re.fullmatch(r"shutdown sessions server (\S+)/(\S+)", command)
        if dropped:
            self.row(dropped[1], dropped[2])["scur"] = 0
        return "\n"

    def row(self, backend: str, name: str) -> dict:
        proxy = next(p for p in self.proxies if p["name"] == backend)
        return next(s for s in proxy["servers"] if s["name"] == name)

    def reload(self) -> None:
        """Новый воркер: всё, что переводили командами, снова в работе."""
        for proxy in self.proxies:
            for row in proxy["servers"]:
                if row["status"] == "MAINT" and not row.get("disabled_in_config"):
                    row["status"] = "UP"


class QuarantineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "haproxy_quarantine.json"
        self.clock = FakeClock()
        self.maint_rows: list[tuple[str, str]] = []
        self.released: list[tuple[str, int]] = []
        self.haproxy = self.make_haproxy()
        self.quarantine = self.make_quarantine()

    def make_haproxy(self) -> FakeHAProxy:
        return FakeHAProxy(
            (LB, [server("srv1", A, scur=7), server("srv2", B, scur=5), server("srv3", C, scur=4)]),
        )

    def make_quarantine(self) -> Quarantine:
        return Quarantine(
            self.haproxy.run, self.haproxy.read, self.state_path, clock=self.clock,
            on_maint=self.maint_rows.append, on_release=self.released.append,
        )

    def apply(self, *targets, share: int = 50, drop: bool = True, dead=()):
        return self.quarantine.apply(list(targets), TTL, share, drop, frozenset(dead))

    def status(self, name: str, backend: str = LB) -> str:
        return self.haproxy.row(backend, name)["status"]


class ApplyTests(QuarantineTestCase):
    def make_haproxy(self) -> FakeHAProxy:
        return FakeHAProxy(
            (LB, [server("srv1", A, scur=7), server("srv1_o2", A, scur=3),
                  server("srv2", B), server("srv2_o2", B), server("srv3", C), server("srv3_o2", C)]),
            (OTHER, [server("srv1", A, scur=2), server("srv9", ("10.0.0.9", 443))]),
        )

    def test_every_row_of_the_address_goes_to_maintenance_and_loses_its_sessions(self):
        [result] = self.apply(A)
        self.assertEqual(self.haproxy.commands, [
            f"set server {LB}/srv1 state maint", f"shutdown sessions server {LB}/srv1",
            f"set server {LB}/srv1_o2 state maint", f"shutdown sessions server {LB}/srv1_o2",
            f"set server {OTHER}/srv1 state maint", f"shutdown sessions server {OTHER}/srv1",
        ])
        self.assertTrue(result.found)
        self.assertEqual((result.ip, result.port), A)
        self.assertEqual(
            [(row.backend, row.server, row.sessions) for row in result.applied],
            [(LB, "srv1", 7), (LB, "srv1_o2", 3), (OTHER, "srv1", 2)],
        )
        self.assertEqual(result.skipped, [])

    def test_sessions_stay_when_drop_is_off(self):
        self.apply(A, drop=False)
        self.assertEqual(self.haproxy.commands, [
            f"set server {LB}/srv1 state maint", f"set server {LB}/srv1_o2 state maint",
            f"set server {OTHER}/srv1 state maint",
        ])

    def test_repeat_sends_no_commands(self):
        self.apply(A)
        sent = len(self.haproxy.commands)
        [result] = self.apply(A)
        self.assertEqual(len(self.haproxy.commands), sent)
        # Строки уже отключены: адрес применён, но переводить в этот раз некого
        self.assertEqual([row.sessions for row in result.applied], [0, 0, 0])

    def test_address_dropped_from_the_list_returns(self):
        self.apply(A)
        self.haproxy.commands.clear()
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.haproxy.commands, [
            f"set server {LB}/srv1 state ready", f"set server {LB}/srv1_o2 state ready",
            f"set server {OTHER}/srv1 state ready",
        ])
        self.assertEqual(self.released, [A])
        self.assertEqual(self.quarantine.quarantined_rows(), frozenset())

    def test_address_without_servers_is_not_found(self):
        [result] = self.apply(("10.9.9.9", 443))
        self.assertFalse(result.found)
        self.assertEqual((result.applied, result.skipped, self.haproxy.commands), ([], [], []))

    def test_duplicate_addresses_are_one(self):
        results = self.apply(A, A)
        self.assertEqual(len(results), 1)

    def test_callback_names_every_row_switched(self):
        self.apply(A)
        self.assertEqual(self.maint_rows, [(LB, "srv1"), (LB, "srv1_o2"), (OTHER, "srv1")])

    def test_quarantined_rows_are_only_ours(self):
        self.haproxy.row(LB, "srv3")["status"] = "MAINT"
        self.apply(A)
        self.assertEqual(
            self.quarantine.quarantined_rows(), frozenset({(LB, "srv1"), (LB, "srv1_o2"), (OTHER, "srv1")}),
        )

    def test_permission_denied_aborts(self):
        self.haproxy.replies[f"set server {LB}/srv1 state maint"] = "Permission denied\n"
        with self.assertRaises(SocketPermissionError):
            self.apply(A)

    def test_haproxy_refusal_is_reported_for_that_backend(self):
        self.haproxy.replies[f"set server {OTHER}/srv1 state maint"] = "No such server.\n"
        [result] = self.apply(A)
        self.assertEqual([row.backend for row in result.applied], [LB, LB])
        self.assertEqual([(s.backend, s.reason) for s in result.skipped], [(OTHER, SkipReason.ERROR)])
        self.assertNotIn((OTHER, "srv1"), self.quarantine.quarantined_rows())

    def test_dead_socket_changes_nothing(self):
        self.haproxy.broken = True
        with self.assertRaises(OSError):
            self.apply(A)
        self.haproxy.broken = False
        self.quarantine.reconcile()
        self.assertEqual(self.haproxy.commands, [])


class WideRuleTestCase(QuarantineTestCase):
    def wide(self, count: int) -> list[tuple[str, int]]:
        targets = [(f"10.0.1.{i}", 443) for i in range(1, count + 1)]
        self.haproxy = FakeHAProxy((LB, [server(f"srv{i}", t) for i, t in enumerate(targets, 1)]))
        self.quarantine = self.make_quarantine()
        return targets

    def applied(self, results) -> list[bool]:
        return [bool(r.applied) for r in results]


class ShareLimitTests(WideRuleTestCase):
    def test_half_of_ten(self):
        targets = self.wide(10)
        results = self.apply(*targets[:6])
        self.assertEqual(self.applied(results), [True] * 5 + [False])
        self.assertEqual([(s.backend, s.reason) for s in results[5].skipped], [(LB, SkipReason.SHARE_LIMIT)])
        self.assertEqual(self.status("srv6"), "UP")

    def test_one_of_three(self):
        targets = self.wide(3)
        self.assertEqual(self.applied(self.apply(*targets[:2])), [True, False])

    def test_one_of_two(self):
        targets = self.wide(2)
        self.assertEqual(self.applied(self.apply(targets[0])), [True])

    def test_single_server_rule_is_never_touched(self):
        [target] = self.wide(1)
        [result] = self.apply(target)
        self.assertEqual([s.reason for s in result.skipped], [SkipReason.SHARE_LIMIT])
        self.assertEqual(self.haproxy.commands, [])

    def test_share_is_what_the_panel_sent(self):
        targets = self.wide(10)
        self.assertEqual(self.applied(self.apply(*targets[:4], share=30)), [True, True, True, False])

    def test_copies_of_one_address_count_once(self):
        self.haproxy = FakeHAProxy((LB, [
            server("srv1", A), server("srv1_o2", A), server("srv2", B), server("srv2_o2", B),
        ]))
        self.quarantine = self.make_quarantine()
        [result] = self.apply(A)
        self.assertEqual([row.server for row in result.applied], ["srv1", "srv1_o2"])

    def test_limit_is_per_rule(self):
        self.haproxy = FakeHAProxy(
            (LB, [server("srv1", A), server("srv2", B)]),
            (OTHER, [server("srv1", A)]),
        )
        self.quarantine = self.make_quarantine()
        [result] = self.apply(A)
        self.assertEqual([row.backend for row in result.applied], [LB])
        self.assertEqual([(s.backend, s.reason) for s in result.skipped], [(OTHER, SkipReason.SHARE_LIMIT)])

    def test_earlier_address_wins_the_slot(self):
        self.apply(B, A)
        self.assertEqual((self.status("srv1"), self.status("srv2")), ("UP", "MAINT"))

    def test_slot_freed_by_a_returned_address_goes_to_the_next(self):
        self.apply(A, B)
        self.assertEqual((self.status("srv1"), self.status("srv2")), ("MAINT", "UP"))
        self.apply(B)
        self.assertEqual((self.status("srv1"), self.status("srv2")), ("UP", "MAINT"))


class DeadAddressTests(WideRuleTestCase):
    """Адрес, не отвечающий на проверку «как клиент», не работает наверняка: держать
    его в балансировке ради доли живых незачем. Потери и падения бывают у рабочего
    сервера — для них предел доли остаётся."""

    def test_dead_addresses_go_out_beyond_the_share(self):
        targets = self.wide(10)
        results = self.apply(*targets[:8], dead=targets[:8])
        self.assertEqual(self.applied(results), [True] * 8)
        self.assertEqual([self.status(f"srv{i}") for i in (8, 9)], ["MAINT", "UP"])

    def test_rule_with_every_server_dead_is_emptied(self):
        # Правило без серверов сразу отказывает клиенту. С мёртвым сервером в работе
        # клиент получил бы принятое подключение и ждал ответа до своего таймаута
        targets = self.wide(3)
        results = self.apply(*targets, dead=targets)
        self.assertEqual(self.applied(results), [True, True, True])
        self.assertEqual([self.status(f"srv{i}") for i in (1, 2, 3)], ["MAINT"] * 3)

    def test_single_dead_server_goes_out_too(self):
        [target] = self.wide(1)
        [result] = self.apply(target, dead=[target])
        self.assertEqual((bool(result.applied), result.skipped), (True, []))
        self.assertEqual(self.status("srv1"), "MAINT")

    def test_doubtful_server_is_still_never_the_last_to_go(self):
        # Сервер с потерями может оказаться рабочим: при мёртвых соседях он остаётся
        lossy, dead_one, dead_two = self.wide(3)
        results = self.apply(dead_one, dead_two, lossy, share=100, dead=[dead_one, dead_two])
        self.assertEqual(self.applied(results), [True, True, False])
        self.assertEqual([s.reason for s in results[2].skipped], [SkipReason.LAST_SERVER])
        self.assertEqual(self.status("srv1"), "UP")

    def test_dead_server_stays_out_when_its_neighbours_fall(self):
        # Обычный отключённый сервер при упавших соседях возвращается: он лучше, чем
        # ничего. Мёртвый не возвращается — клиентам от него толку нет
        targets = self.wide(3)
        self.apply(targets[0], dead=[targets[0]])
        for name in ("srv2", "srv3"):
            self.haproxy.row(LB, name)["status"] = "DOWN"
        self.haproxy.commands.clear()
        self.quarantine.reconcile()
        self.assertEqual((self.haproxy.commands, self.status("srv1")), ([], "MAINT"))

    def test_dead_addresses_use_up_the_share_of_the_doubtful(self):
        # Пять мёртвых из десяти уже заняли всю долю: адреса с потерями остаются в работе
        targets = self.wide(10)
        results = self.apply(*targets[:7], dead=targets[:5])
        self.assertEqual(self.applied(results), [True] * 5 + [False, False])
        self.assertEqual([s.reason for s in results[5].skipped], [SkipReason.SHARE_LIMIT])

    def test_doubtful_address_takes_the_share_the_dead_left(self):
        targets = self.wide(10)
        results = self.apply(*targets[:6], dead=targets[:3])
        self.assertEqual(self.applied(results), [True] * 5 + [False])

    def test_dead_addresses_take_their_slots_wherever_they_stand_in_the_list(self):
        # Адрес с потерями впереди списка не должен оставить в работе мёртвый вместо себя
        lossy, dead_one, dead_two = self.wide(3)
        results = self.apply(lossy, dead_one, dead_two, dead=[dead_one, dead_two])
        self.assertEqual(self.applied(results), [False, True, True])
        self.assertEqual([s.reason for s in results[0].skipped], [SkipReason.SHARE_LIMIT])
        self.assertEqual([self.status(f"srv{i}") for i in (1, 2, 3)], ["UP", "MAINT", "MAINT"])

    def test_address_that_is_dead_no_more_falls_back_under_the_share(self):
        targets = self.wide(4)
        self.apply(*targets[:3], dead=targets[:3])
        self.assertEqual([self.status(f"srv{i}") for i in (1, 2, 3)], ["MAINT"] * 3)
        self.apply(*targets[:3])
        self.assertEqual([self.status(f"srv{i}") for i in (1, 2, 3)], ["MAINT", "MAINT", "UP"])


class LastServerTests(QuarantineTestCase):
    def pair(self, neighbour: dict) -> None:
        self.haproxy = FakeHAProxy((LB, [server("srv1", A), neighbour]))
        self.quarantine = self.make_quarantine()

    def assert_guarded(self) -> None:
        [result] = self.apply(A)
        self.assertEqual([(s.backend, s.reason) for s in result.skipped], [(LB, SkipReason.LAST_SERVER)])
        self.assertEqual((result.applied, self.haproxy.commands), ([], []))

    def test_down_neighbour_is_no_cover(self):
        self.pair(server("srv2", B, status="DOWN"))
        self.assert_guarded()

    def test_backup_neighbour_is_no_cover(self):
        self.pair(server("srv2", B, backup=True))
        self.assert_guarded()

    def test_zero_weight_neighbour_is_no_cover(self):
        self.pair(server("srv2", B, weight=0))
        self.assert_guarded()

    def test_neighbour_in_manual_maintenance_is_no_cover(self):
        self.pair(server("srv2", B, status="MAINT"))
        self.assert_guarded()

    def test_unchecked_neighbour_is_cover(self):
        self.pair(server("srv2", B, status="no check"))
        self.assertEqual(self.status("srv1"), "UP")
        self.apply(A)
        self.assertEqual(self.status("srv1"), "MAINT")

    def test_neighbour_failing_later_brings_the_server_back(self):
        self.apply(A)
        for name in ("srv2", "srv3"):
            self.haproxy.row(LB, name)["status"] = "DOWN"
        self.haproxy.commands.clear()
        self.quarantine.reconcile()
        self.assertEqual(self.haproxy.commands, [f"set server {LB}/srv1 state ready"])
        # Адрес остаётся в списке панели: поднимется сосед — отключится снова
        self.haproxy.row(LB, "srv2")["status"] = "UP"
        self.quarantine.reconcile()
        self.assertEqual(self.status("srv1"), "MAINT")


class ManualMaintenanceTests(QuarantineTestCase):
    def test_server_disabled_by_hand_is_left_alone(self):
        self.haproxy.row(LB, "srv1")["status"] = "MAINT"
        [result] = self.apply(A)
        self.assertEqual([(s.backend, s.reason) for s in result.skipped], [(LB, SkipReason.MANUAL_MAINT)])
        self.apply()
        self.assertEqual(self.haproxy.commands, [])
        self.assertEqual(self.status("srv1"), "MAINT")


class LeaseTests(QuarantineTestCase):
    def test_silent_panel_means_the_server_returns(self):
        self.apply(A)
        self.clock.now += TTL - 1
        self.quarantine.reconcile()
        self.assertEqual(self.status("srv1"), "MAINT")
        self.clock.now += 1
        self.quarantine.reconcile()
        self.assertEqual(self.status("srv1"), "UP")
        self.assertEqual(self.released, [A])

    def test_repeat_extends_the_lease(self):
        self.apply(A)
        self.clock.now += 100
        self.apply(A)
        self.clock.now += 100
        self.quarantine.reconcile()
        self.assertEqual(self.status("srv1"), "MAINT")

    def test_return_is_retried_after_a_dead_socket(self):
        self.apply(A)
        self.clock.now += TTL
        self.haproxy.broken = True
        with self.assertRaises(OSError):
            self.quarantine.reconcile()
        self.haproxy.broken = False
        self.quarantine.reconcile()
        self.assertEqual(self.status("srv1"), "UP")

    def test_idle_manager_does_not_touch_the_socket(self):
        self.quarantine.reconcile()
        self.assertEqual(self.haproxy.reads, 0)

    def test_row_removed_from_config_is_forgotten(self):
        self.apply(A)
        self.haproxy.proxies[0]["servers"] = [s for s in self.haproxy.proxies[0]["servers"] if s["name"] != "srv1"]
        self.haproxy.commands.clear()
        self.apply()
        self.assertEqual(self.haproxy.commands, [])
        self.assertEqual(self.quarantine.quarantined_rows(), frozenset())


class ReloadTests(QuarantineTestCase):
    def test_new_worker_gets_the_maintenance_again(self):
        self.apply(A)
        self.haproxy.reload()
        self.haproxy.row(LB, "srv1")["scur"] = 2
        self.haproxy.commands.clear()
        self.quarantine.reconcile()
        self.assertEqual(self.haproxy.commands, [
            f"set server {LB}/srv1 state maint", f"shutdown sessions server {LB}/srv1",
        ])

    def test_server_disabled_in_config_survives_our_release(self):
        row = self.haproxy.row(LB, "srv3")
        row["status"], row["disabled_in_config"] = "MAINT", True
        self.apply(A)
        self.haproxy.reload()
        self.quarantine.reconcile()
        self.apply()
        self.assertEqual(self.status("srv3"), "MAINT")
        self.assertNotIn(f"set server {LB}/srv3 state ready", self.haproxy.commands)


class HungSocketTests(QuarantineTestCase):
    def test_stats_page_does_not_wait_for_a_hung_socket(self):
        # quarantined_rows() зовётся из цикла событий агента при каждом запросе статистики
        self.apply(A)
        entered, release = threading.Event(), threading.Event()

        def slow_read() -> list[dict]:
            entered.set()
            release.wait(5)
            return self.haproxy.read()

        hung = Quarantine(self.haproxy.run, slow_read, self.state_path, clock=self.clock)
        worker = threading.Thread(target=hung.reconcile)
        worker.start()
        entered.wait(5)
        started = time.monotonic()
        rows = hung.quarantined_rows()
        waited = time.monotonic() - started
        release.set()
        worker.join(5)
        self.assertEqual(rows, frozenset({(LB, "srv1")}))
        self.assertLess(waited, 1.0)


class BackgroundLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_error_does_not_stop_the_rounds(self):
        # Умерший круг — это серверы, которые никто не вернёт по сроку
        with tempfile.TemporaryDirectory() as folder:
            quarantine = Quarantine(lambda command: "\n", lambda: [], Path(folder) / "state.json")
            rounds = []

            def reconcile() -> None:
                rounds.append(len(rounds))
                if len(rounds) == 1:
                    raise ValueError("unexpected")

            quarantine.reconcile = reconcile
            with mock.patch.object(quarantine_module, "RECONCILE_SECONDS", 0.01),                  self.assertLogs(quarantine_module.logger, level="ERROR"):
                await quarantine.start()
                await asyncio.sleep(0.3)
                await quarantine.stop()
        self.assertGreater(len(rounds), 1)


class RestartTests(QuarantineTestCase):
    def test_restarted_agent_still_returns_the_server(self):
        self.apply(A)
        self.quarantine = self.make_quarantine()
        self.assertEqual(self.quarantine.quarantined_rows(), frozenset({(LB, "srv1")}))
        self.clock.now += TTL
        self.quarantine.reconcile()
        self.assertEqual(self.status("srv1"), "UP")

    def test_restarted_agent_keeps_the_panel_settings(self):
        self.apply(A, drop=False)
        self.quarantine = self.make_quarantine()
        self.haproxy.reload()
        self.haproxy.commands.clear()
        self.quarantine.reconcile()
        self.assertEqual(self.haproxy.commands, [f"set server {LB}/srv1 state maint"])

    def test_restarted_agent_remembers_which_addresses_are_dead(self):
        # Из трёх адресов по доле выводится один; оба мёртвых должны остаться вне балансировки
        self.apply(A, B, dead=[A, B])
        self.quarantine = self.make_quarantine()
        self.haproxy.reload()
        self.quarantine.reconcile()
        self.assertEqual((self.status("srv1"), self.status("srv2")), ("MAINT", "MAINT"))

    def test_state_file_of_an_agent_without_dead_marks_is_read(self):
        self.apply(A)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        for lease in state["leases"]:
            lease.pop("dead", None)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.quarantine = self.make_quarantine()
        self.assertEqual(self.quarantine.quarantined_rows(), frozenset({(LB, "srv1")}))
        self.clock.now += TTL
        self.quarantine.reconcile()
        self.assertEqual(self.status("srv1"), "UP")

    def test_restart_does_not_adopt_a_manual_maintenance(self):
        self.haproxy.row(LB, "srv2")["status"] = "MAINT"
        self.apply(A)
        self.quarantine = self.make_quarantine()
        self.assertNotIn((LB, "srv2"), self.quarantine.quarantined_rows())

    def test_broken_state_file_is_an_empty_state(self):
        self.state_path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("app.services.haproxy_quarantine", level="ERROR"):
            quarantine = self.make_quarantine()
        self.assertEqual(quarantine.quarantined_rows(), frozenset())
        quarantine.reconcile()
        self.assertEqual(self.haproxy.reads, 0)

    def test_state_file_of_another_shape_is_an_empty_state(self):
        self.state_path.write_text('{"leases": [{"ip": "10.0.0.1"}], "owned": "x"}', encoding="utf-8")
        with self.assertLogs("app.services.haproxy_quarantine", level="ERROR"):
            quarantine = self.make_quarantine()
        self.assertEqual(quarantine.quarantined_rows(), frozenset())


if __name__ == "__main__":
    unittest.main()
