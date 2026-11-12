"""Раскатка профиля фоновым заданием (services/profile_rollouts, routers/rollouts).

Голый unittest, без PostgreSQL и без сети: сессия БД и отправка на ноды подменены.

«Синхронизировать всё» держало HTTP-запрос до конца раскатки. Она обходит серверы
по десять и на сотнях нод идёт минутами, а запрос живёт 30 секунд в браузере и 60
на nginx панели: страница показывала ошибку, пока раскатка шла дальше. Теперь
кнопка получает задание сразу, а ход и итог отдаются отдельным запросом.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import os
import sys
import unittest
from contextlib import ExitStack
from dataclasses import dataclass
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import verify_auth  # noqa: E402
from app.database import get_db  # noqa: E402
from app.routers import (  # noqa: E402
    dnat_profiles,
    firewall_profiles,
    haproxy_profiles,
    remnawave_nginx_profiles,
    rollouts,
)
from app.services import (  # noqa: E402
    dnat_profile_sync,
    firewall_profile_sync,
    haproxy_profile_sync,
    remnawave_nginx_sync,
)
from app.services.profile_rollouts import (  # noqa: E402
    FINISHED_TTL_SECONDS,
    RolloutInProgressError,
    RolloutRegistry,
)

PROFILE_ID = 7
ONLINE, SLOW_ONLINE, OFFLINE, DENIED = 1, 2, 3, 4


@dataclass
class NodeResult:
    server_id: int
    server_name: str
    success: bool
    message: str


async def settle(condition, attempts: int = 200) -> None:
    """Дать фоновой задаче поработать, пока условие не выполнится."""
    for _ in range(attempts):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("background rollout did not reach the expected state")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class RegistryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = FakeClock()
        self.registry = RolloutRegistry(clock=self.clock)
        self.release = asyncio.Event()

    async def two_servers(self, progress) -> None:
        progress.expect(2)
        progress.record(NodeResult(1, "node-1", True, "applied"))
        await self.release.wait()
        progress.record(NodeResult(2, "node-2", False, "Connection timeout"))

    async def test_start_answers_at_once_while_the_rollout_goes_on(self):
        rollout = self.registry.start("firewall", PROFILE_ID, self.two_servers)
        await settle(lambda: rollout.to_dict()["done"] == 1)

        state = rollout.to_dict()
        self.assertEqual((state["status"], state["total"], state["done"]), ("running", 2, 1))
        self.assertTrue(state["job_id"])
        self.assertIsNone(state["error"])

    async def test_results_are_handed_out_only_when_the_rollout_is_over(self):
        rollout = self.registry.start("firewall", PROFILE_ID, self.two_servers)
        await settle(lambda: rollout.to_dict()["done"] == 1)
        self.assertNotIn("results", rollout.to_dict())

        self.release.set()
        await rollout.task

        state = rollout.to_dict()
        self.assertEqual((state["status"], state["total"], state["done"]), ("completed", 2, 2))
        self.assertEqual(state["results"], [
            {"server_id": 1, "server_name": "node-1", "success": True, "message": "applied"},
            {"server_id": 2, "server_name": "node-2", "success": False, "message": "Connection timeout"},
        ])

    async def test_second_rollout_of_the_same_profile_is_refused_while_the_first_runs(self):
        self.registry.start("firewall", PROFILE_ID, self.two_servers)
        with self.assertRaises(RolloutInProgressError):
            self.registry.start("firewall", PROFILE_ID, self.two_servers)
        self.release.set()

    async def test_other_profile_and_other_kind_are_not_blocked(self):
        self.registry.start("firewall", PROFILE_ID, self.two_servers)
        other_profile = self.registry.start("firewall", PROFILE_ID + 1, self.two_servers)
        other_kind = self.registry.start("haproxy", PROFILE_ID, self.two_servers)

        self.assertIsNot(other_profile, other_kind)
        self.release.set()
        await asyncio.gather(other_profile.task, other_kind.task)

    async def test_finished_rollout_lets_the_next_one_start(self):
        first = self.registry.start("firewall", PROFILE_ID, self.two_servers)
        self.release.set()
        await first.task

        second = self.registry.start("firewall", PROFILE_ID, self.two_servers)

        self.assertNotEqual(first.id, second.id)
        self.assertIs(self.registry.latest("firewall", PROFILE_ID), second)
        await second.task

    async def test_runner_error_lands_in_the_state_and_frees_the_profile(self):
        async def database_down(progress) -> None:
            progress.expect(3)
            raise RuntimeError("connection refused")

        with self.assertLogs("app.services.profile_rollouts", level="ERROR"):
            rollout = self.registry.start("dnat", PROFILE_ID, database_down)
            await rollout.task

        state = rollout.to_dict()
        self.assertEqual((state["status"], state["error"]), ("failed", "connection refused"))
        self.assertEqual(state["results"], [])
        self.registry.start("dnat", PROFILE_ID, self.two_servers)
        self.release.set()

    async def test_cancelled_rollout_does_not_keep_the_profile_busy(self):
        rollout = self.registry.start("firewall", PROFILE_ID, self.two_servers)
        await settle(lambda: rollout.to_dict()["done"] == 1)

        rollout.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await rollout.task

        self.assertEqual(rollout.to_dict()["status"], "failed")
        self.assertTrue(rollout.to_dict()["error"])
        self.registry.start("firewall", PROFILE_ID, self.two_servers)
        self.release.set()

    async def test_rollout_that_found_no_servers_reports_zero(self):
        async def profile_was_deleted(progress) -> None:
            return None

        rollout = self.registry.start("firewall", PROFILE_ID, profile_was_deleted)
        await rollout.task

        state = rollout.to_dict()
        self.assertEqual((state["status"], state["total"], state["done"]), ("completed", 0, 0))

    async def test_finished_rollout_is_forgotten_after_the_ttl(self):
        rollout = self.registry.start("firewall", PROFILE_ID, self.two_servers)
        self.release.set()
        await rollout.task

        self.clock.now += FINISHED_TTL_SECONDS - 1
        self.assertIs(self.registry.latest("firewall", PROFILE_ID), rollout)
        self.clock.now += 2
        self.assertIsNone(self.registry.latest("firewall", PROFILE_ID))

    async def test_running_rollout_is_kept_however_long_it_takes(self):
        rollout = self.registry.start("firewall", PROFILE_ID, self.two_servers)
        self.clock.now += FINISHED_TTL_SECONDS * 10

        self.assertIs(self.registry.latest("firewall", PROFILE_ID), rollout)
        self.release.set()

    async def test_nothing_rolled_out_means_no_state(self):
        self.assertIsNone(self.registry.latest("firewall", PROFILE_ID))


def linked_server(server_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=server_id, name=f"node-{server_id}", is_active=True,
        haproxy_listen_ips=None, haproxy_source_ips=None, haproxy_config_hash=None,
        remnawave_nginx_config_hash=None, remnawave_nginx_domain=None,
    )


class FakeDb:
    def __init__(self, servers: list[SimpleNamespace]) -> None:
        self._servers = servers
        self.commit = mock.AsyncMock()

    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self._servers)))


class RecordedProgress:
    def __init__(self) -> None:
        self.total = None
        self.server_ids: list[int] = []

    def expect(self, total: int) -> None:
        self.total = total

    def record(self, result) -> None:
        self.server_ids.append(result.server_id)


class ServiceProgressTest(unittest.IsolatedAsyncioTestCase):
    """Все четыре раскатки сообщают число серверов и итог по каждому, как только он известен.

    В профиле четыре сервера: обычный, медленный, офлайн и с закрытым разделом.
    """

    async def asyncSetUp(self) -> None:
        self.servers = [linked_server(i) for i in (ONLINE, SLOW_ONLINE, OFFLINE, DENIED)]
        self.db = FakeDb(self.servers)
        self.progress = RecordedProgress()
        self.slow_node_answers = asyncio.Event()
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)

    def replace(self, module, name: str, replacement) -> None:
        self.patches.enter_context(mock.patch.object(module, name, replacement))

    def fake_nodes(self, module) -> None:
        async def apply(server, *_args, **_kwargs):
            if server.id == SLOW_ONLINE:
                await self.slow_node_answers.wait()
            return module.SyncResult(server.id, server.name, True, "applied")

        self.replace(module, "server_allows", lambda server, *_a, **_k: server.id != DENIED)
        self.replace(module, "is_server_online", lambda server, *_a, **_k: server.id != OFFLINE)
        self.replace(module, "_sync_single_server", apply)

    def queue_offline_in_bulk(self, module) -> None:
        async def queue(servers, *_args):
            return [module.SyncResult(s.id, s.name, False, "queued", queued=True) for s in servers]

        self.replace(module, "_queue_offline_servers", queue)

    def queue_offline_one_by_one(self, module) -> None:
        async def queue(server, *_args):
            return module.SyncResult(server.id, server.name, False, "queued", status="queued")

        self.replace(module, "_queue_offline_server", queue)

    def firewall_rollout(self):
        self.fake_nodes(firewall_profile_sync)
        self.queue_offline_in_bulk(firewall_profile_sync)
        profile = SimpleNamespace(
            id=PROFILE_ID, rules_json="[]", default_incoming="deny", default_outgoing="allow",
        )
        return firewall_profile_sync.sync_profile_to_servers(profile, self.db, progress=self.progress)

    def haproxy_rollout(self):
        self.fake_nodes(haproxy_profile_sync)
        self.queue_offline_one_by_one(haproxy_profile_sync)
        profile = SimpleNamespace(id=PROFILE_ID, config_content="global\n")
        return haproxy_profile_sync.sync_profile_to_servers(profile, self.db, progress=self.progress)

    def dnat_rollout(self):
        self.fake_nodes(dnat_profile_sync)
        self.queue_offline_in_bulk(dnat_profile_sync)
        self.replace(dnat_profile_sync, "ordered_linked_servers", mock.AsyncMock(return_value=self.servers))
        profile = SimpleNamespace(id=PROFILE_ID, rules_json="[]")
        return dnat_profile_sync.sync_profile_to_servers(profile, self.db, progress=self.progress)

    def nginx_rollout(self):
        self.fake_nodes(remnawave_nginx_sync)
        self.queue_offline_one_by_one(remnawave_nginx_sync)
        self.replace(
            remnawave_nginx_sync, "get_remnawave_nginx_path", mock.AsyncMock(return_value="/opt/remnawave"),
        )
        profile = SimpleNamespace(id=PROFILE_ID, config_content="server {}")
        return remnawave_nginx_sync.sync_profile_to_servers(profile, self.db, progress=self.progress)

    async def assert_reports_each_server_as_it_settles(self, rollout, unreported: frozenset = frozenset()) -> None:
        reported = {ONLINE, SLOW_ONLINE, OFFLINE, DENIED} - unreported
        task = asyncio.create_task(rollout)
        await settle(lambda: len(self.progress.server_ids) == len(reported) - 1)

        self.assertEqual(self.progress.total, len(reported))
        self.assertEqual(set(self.progress.server_ids), reported - {SLOW_ONLINE})
        self.assertFalse(task.done())

        self.slow_node_answers.set()
        results = await task

        self.assertEqual(sorted(self.progress.server_ids), sorted(reported))
        self.assertEqual(sorted(result.server_id for result in results), sorted(reported))

    async def test_firewall(self):
        await self.assert_reports_each_server_as_it_settles(self.firewall_rollout())

    async def test_haproxy(self):
        await self.assert_reports_each_server_as_it_settles(self.haproxy_rollout())

    async def test_dnat(self):
        await self.assert_reports_each_server_as_it_settles(self.dnat_rollout())

    async def test_remnawave_nginx(self):
        # Сервер с закрытым разделом эта раскатка в итог не включает — значит, и в счёт
        # он не идёт: иначе ход остановился бы на «3 из 4»
        await self.assert_reports_each_server_as_it_settles(
            self.nginx_rollout(), unreported=frozenset({DENIED}),
        )

    async def test_profile_without_servers_reports_zero(self):
        self.servers.clear()
        for rollout in (self.firewall_rollout, self.haproxy_rollout, self.dnat_rollout, self.nginx_rollout):
            with self.subTest(rollout=rollout.__name__):
                self.progress.total = None
                self.assertEqual(await rollout(), [])
                self.assertEqual(self.progress.total, 0)

    async def test_rollout_nobody_watches_still_works(self):
        self.slow_node_answers.set()
        self.fake_nodes(firewall_profile_sync)
        self.queue_offline_in_bulk(firewall_profile_sync)
        profile = SimpleNamespace(
            id=PROFILE_ID, rules_json="[]", default_incoming="deny", default_outgoing="allow",
        )

        results = await firewall_profile_sync.sync_profile_to_servers(profile, self.db)

        self.assertEqual(len(results), 4)

    async def test_total_never_exceeds_the_results_whoever_is_denied(self):
        for rollout in (self.firewall_rollout, self.haproxy_rollout, self.dnat_rollout, self.nginx_rollout):
            with self.subTest(rollout=rollout.__name__):
                self.slow_node_answers.set()
                self.progress = RecordedProgress()

                results = await rollout()

                self.assertEqual(self.progress.total, len(results))
                self.assertEqual(len(self.progress.server_ids), len(results))


class FakeSession:
    def __init__(self, profile) -> None:
        self._profile = profile

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, _model, _profile_id):
        return self._profile


@dataclass(frozen=True)
class ProfileRouter:
    module: object
    result: type
    result_fields: frozenset


COMMON_FIELDS = {"server_id", "server_name", "success", "message"}
# Поля итога по серверу читает страница профиля: их набор — договор с фронтендом
PROFILE_ROUTERS = (
    ProfileRouter(firewall_profiles, firewall_profile_sync.SyncResult,
                  frozenset(COMMON_FIELDS | {"rolled_back", "queued"})),
    ProfileRouter(haproxy_profiles, haproxy_profile_sync.SyncResult, frozenset(COMMON_FIELDS | {"status"})),
    ProfileRouter(dnat_profiles, dnat_profile_sync.SyncResult, frozenset(COMMON_FIELDS | {"queued"})),
    ProfileRouter(remnawave_nginx_profiles, remnawave_nginx_sync.SyncResult,
                  frozenset(COMMON_FIELDS | {"status"})),
)


class ProfileRouterTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.registry = RolloutRegistry()
        self.node_answers = asyncio.Event()
        self.sync_calls: list[dict] = []
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(rollouts, "get_rollout_registry", lambda: self.registry))
        for case in PROFILE_ROUTERS:
            self.fake_rollout(case)

    def fake_rollout(self, case: ProfileRouter) -> None:
        profile = SimpleNamespace(id=PROFILE_ID)

        async def sync(_profile, _db, **kwargs):
            self.sync_calls.append(kwargs)
            kwargs["progress"].expect(1)
            await self.node_answers.wait()
            kwargs["progress"].record(case.result(1, "node-1", True, "applied"))
            return []

        for name, replacement in (
            ("_get_profile", mock.AsyncMock(return_value=profile)),
            ("async_session_maker", lambda: FakeSession(profile)),
            ("sync_profile_to_servers", sync),
        ):
            self.patches.enter_context(mock.patch.object(case.module, name, replacement))

    async def test_sync_all_answers_with_a_running_job(self):
        for case in PROFILE_ROUTERS:
            with self.subTest(router=case.module.__name__):
                answer = await case.module.sync_all(PROFILE_ID, db=None, _=None)

                self.assertEqual(answer["job"]["status"], "running")
                self.assertEqual(answer["job"]["done"], 0)
        self.node_answers.set()

    async def test_state_follows_the_rollout_to_its_results(self):
        for case in PROFILE_ROUTERS:
            with self.subTest(router=case.module.__name__):
                started = await case.module.sync_all(PROFILE_ID, db=None, _=None)
                kind = case.module.ROLLOUT_KIND
                await settle(lambda: self.registry.latest(kind, PROFILE_ID).total == 1)

                running = await case.module.sync_state(PROFILE_ID, _=None)
                self.assertEqual(running["job"]["job_id"], started["job"]["job_id"])
                self.assertEqual((running["job"]["status"], running["job"]["total"]), ("running", 1))

                self.node_answers.set()
                await self.registry.latest(kind, PROFILE_ID).task

                finished = (await case.module.sync_state(PROFILE_ID, _=None))["job"]
                self.assertEqual((finished["status"], finished["done"]), ("completed", 1))
                self.assertEqual(set(finished["results"][0]), case.result_fields)
                self.node_answers.clear()

    async def test_same_profile_id_of_different_kinds_rolls_out_side_by_side(self):
        for case in PROFILE_ROUTERS:
            await case.module.sync_all(PROFILE_ID, db=None, _=None)

        self.assertEqual(len({case.module.ROLLOUT_KIND for case in PROFILE_ROUTERS}), len(PROFILE_ROUTERS))
        self.node_answers.set()

    async def test_second_click_while_the_rollout_runs_is_answered_409(self):
        for case in PROFILE_ROUTERS:
            with self.subTest(router=case.module.__name__):
                await case.module.sync_all(PROFILE_ID, db=None, _=None)
                with self.assertRaises(HTTPException) as refused:
                    await case.module.sync_all(PROFILE_ID, db=None, _=None)

                self.assertEqual(refused.exception.status_code, 409)
                self.assertEqual(refused.exception.detail, rollouts.ROLLOUT_IN_PROGRESS_MESSAGE)
        self.node_answers.set()

    async def test_state_of_a_profile_nobody_rolled_out_is_empty(self):
        for case in PROFILE_ROUTERS:
            with self.subTest(router=case.module.__name__):
                self.assertEqual(await case.module.sync_state(PROFILE_ID, _=None), {"job": None})

    async def test_firewall_passes_the_selected_servers_and_force_to_the_rollout(self):
        selected = firewall_profiles.SyncRequest(server_ids=[3, 5])

        await firewall_profiles.sync_all(PROFILE_ID, force=True, data=selected, db=None, _=None)
        await settle(lambda: bool(self.sync_calls))

        self.assertEqual(self.sync_calls[0]["server_ids"], [3, 5])
        self.assertTrue(self.sync_calls[0]["force"])
        self.node_answers.set()

    def test_start_is_accepted_and_state_is_served_on_the_same_path(self):
        for case in PROFILE_ROUTERS:
            with self.subTest(router=case.module.__name__):
                by_method = {
                    method: route
                    for route in case.module.router.routes
                    if route.path.endswith("/{profile_id}/sync")
                    for method in route.methods
                }
                self.assertEqual(by_method["POST"].status_code, 202)
                self.assertIn("GET", by_method)


class RolloutOverHttpTest(unittest.TestCase):
    """Тот же путь через настоящий роутер: коды ответов и то, что уходит в JSON."""

    URL = f"/haproxy-profiles/{PROFILE_ID}/sync"

    def setUp(self) -> None:
        registry = RolloutRegistry()
        self.node_answers = asyncio.Event()
        profile = SimpleNamespace(id=PROFILE_ID)

        async def sync(_profile, _db, **kwargs):
            kwargs["progress"].expect(1)
            await self.node_answers.wait()
            kwargs["progress"].record(
                haproxy_profile_sync.SyncResult(1, "node-1", True, "applied", status="success"),
            )
            return []

        for target, name, replacement in (
            (rollouts, "get_rollout_registry", lambda: registry),
            (haproxy_profiles, "_get_profile", mock.AsyncMock(return_value=profile)),
            (haproxy_profiles, "async_session_maker", lambda: FakeSession(profile)),
            (haproxy_profiles, "sync_profile_to_servers", sync),
        ):
            self.enterContext(mock.patch.object(target, name, replacement))

        app = FastAPI()
        app.include_router(haproxy_profiles.router)
        app.dependency_overrides[verify_auth] = lambda: {}
        app.dependency_overrides[get_db] = lambda: None
        self.client = self.enterContext(TestClient(app))

    def job(self) -> dict:
        return self.client.get(self.URL).json()["job"]

    def test_click_gets_an_answer_before_the_rollout_ends(self):
        started = self.client.post(self.URL)

        self.assertEqual(started.status_code, 202)
        self.assertEqual(started.json()["job"]["status"], "running")
        self.client.portal.call(self.node_answers.set)

    def test_progress_and_results_come_from_the_state_request(self):
        job_id = self.client.post(self.URL).json()["job"]["job_id"]

        running = self.job()
        self.assertEqual(
            (running["job_id"], running["status"], running["total"], running["done"]), (job_id, "running", 1, 0),
        )
        self.assertNotIn("results", running)

        self.client.portal.call(self.node_answers.set)
        finished = self.job()
        for _ in range(50):
            if finished["status"] != "running":
                break
            finished = self.job()

        self.assertEqual((finished["status"], finished["done"], finished["error"]), ("completed", 1, None))
        self.assertEqual(finished["results"], [
            {"server_id": 1, "server_name": "node-1", "success": True, "message": "applied", "status": "success"},
        ])

    def test_second_start_while_running_is_409_with_a_reason(self):
        self.client.post(self.URL)

        again = self.client.post(self.URL)

        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json(), {"detail": rollouts.ROLLOUT_IN_PROGRESS_MESSAGE})
        self.client.portal.call(self.node_answers.set)

    def test_profile_never_rolled_out_has_no_job(self):
        self.assertEqual(self.client.get(self.URL).json(), {"job": None})


if __name__ == "__main__":
    unittest.main()
