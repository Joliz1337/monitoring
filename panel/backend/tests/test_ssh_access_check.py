"""Проверка SSH-доступа: панель заранее пробует войти на серверы.

Без неё о неверном пароле узнают, когда нода уже не обновилась и запасной путь
не сработал. Проверка только входит и выходит: команд на сервере не выполняет,
неверный пароль не повторяет (на нодах стоит fail2ban), чужой ключ хоста
отличает от неверного пароля. Идёт фоном с потолком одновременных подключений,
её можно остановить.
"""

import asyncio
import os
import socket
import sys
import unittest
from unittest import mock

import asyncssh
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ssh_fakes import FakeDb, LiveSshCase, Socks5Proxy, public_text  # noqa: E402

from app.models import Server, SshAccessProfile  # noqa: E402
from app.routers import ssh_access as api  # noqa: E402
from app.services import ssh_access, ssh_access_check  # noqa: E402
from app.services.ssh_access import AccessScope  # noqa: E402
from app.services.ssh_access_check import (  # noqa: E402
    CheckAlreadyRunning,
    CheckOutcome,
    CheckStatus,
    SshAccessCheckManager,
    check_target,
)
from app.services.ssh_target import AccessLevel, SSHTarget, SSHTargetError  # noqa: E402


def dead_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def finished(manager: SshAccessCheckManager) -> dict:
    while manager.state()["status"] == "running":
        await asyncio.sleep(0.01)
    return manager.state()


class CheckTargetTests(LiveSshCase):
    async def test_working_login_is_ok(self):
        self.assertEqual(await check_target(self.target()), CheckOutcome(CheckStatus.OK))

    async def test_wrong_password_is_one_failed_attempt(self):
        outcome = await check_target(self.target(password="wrong"))

        self.assertEqual(outcome.status, CheckStatus.AUTH_FAILED)
        self.assertEqual(self.seen.count("password"), 1)

    async def test_changed_host_key_stops_before_auth(self):
        stranger = public_text(asyncssh.generate_private_key("ssh-ed25519"))

        outcome = await check_target(self.target(host_key=stranger))

        self.assertEqual(outcome.status, CheckStatus.HOST_KEY_CHANGED)
        self.assertNotIn("auth", self.seen)

    async def test_dead_port_is_unreachable_with_the_reason(self):
        outcome = await check_target(self.target(port=dead_port()))

        self.assertEqual(outcome.status, CheckStatus.UNREACHABLE)
        self.assertTrue(outcome.detail)

    async def test_silent_server_is_given_the_short_check_timeout(self):
        with mock.patch.object(asyncssh, "connect", mock.AsyncMock(side_effect=asyncio.TimeoutError())):
            outcome = await check_target(SSHTarget(host="10.0.0.5", port=2222, password="pw"))

        self.assertEqual(outcome, CheckOutcome(CheckStatus.UNREACHABLE, "10.0.0.5:2222 не ответил за 15 с"))

    async def test_broken_key_is_reported_not_raised(self):
        outcome = await check_target(self.target(password=None, private_key="not a key"))

        self.assertEqual(outcome.status, CheckStatus.BAD_KEY)
        self.assertEqual(self.seen, [])

    async def test_nothing_is_executed_on_the_server(self):
        await check_target(self.target())

        self.assertIn("password", self.seen)
        self.assertEqual(self.commands, [])

    async def test_first_successful_check_pins_the_host_key(self):
        await check_target(self.target(server_id=7))

        self.remember.assert_awaited_once_with(7, public_text(self.host_key))

    async def test_proxy_used_by_design_is_not_counted_as_fallback(self):
        # Режим «всегда» или прокси из карточки сервера: запасной путь не понадобился
        proxy = Socks5Proxy()
        address = f"127.0.0.1:{await proxy.start()}"
        self.addAsyncCleanup(proxy.stop)

        outcome = await check_target(self.target(routes=(address,)))

        self.assertEqual(outcome, CheckOutcome(CheckStatus.OK))
        self.assertEqual(proxy.destinations, [("127.0.0.1", self.port)])

    async def test_login_over_the_fallback_proxy_is_marked(self):
        proxy = Socks5Proxy(redirect_to=self.port)
        address = f"127.0.0.1:{await proxy.start()}"
        self.addAsyncCleanup(proxy.stop)

        outcome = await check_target(self.target(port=dead_port(), routes=(None, address)))

        self.assertEqual(outcome, CheckOutcome(CheckStatus.OK, via_proxy=True))


class BlockedChecks:
    """Подмена check_target: проверки стоят, пока тест их не отпустит."""

    def __init__(self):
        self.release = asyncio.Event()
        self.in_flight = 0
        self.peak = 0

    async def __call__(self, _target: SSHTarget) -> CheckOutcome:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await self.release.wait()
        finally:
            self.in_flight -= 1
        return CheckOutcome(CheckStatus.OK)


def subject(server_id: int, **target_fields):
    return server_id, f"node-{server_id}", SSHTarget(host=f"10.0.0.{server_id}", password="pw", **target_fields)


def refused(server_id: int, reason: str = "no_creds"):
    return server_id, f"node-{server_id}", SSHTargetError(reason, "нет доступа")


class CheckManagerTests(LiveSshCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.manager = SshAccessCheckManager()

    def blocked(self) -> BlockedChecks:
        checks = BlockedChecks()
        patcher = mock.patch.object(ssh_access_check, "check_target", checks)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Отпустить в любом случае, иначе упавший тест оставит висящие задачи
        self.addCleanup(checks.release.set)
        return checks

    def test_nothing_was_checked_yet(self):
        self.assertIsNone(self.manager.state())

    async def test_state_counts_every_server_and_lists_the_failed(self):
        subjects = [
            (1, "node-1", self.target()),
            (2, "node-2", self.target(password="wrong", login_level=AccessLevel.GLOBAL)),
            refused(3),
        ]

        job_id = self.manager.start(subjects)
        state = await finished(self.manager)

        self.assertEqual((state["job_id"], state["status"], state["total"], state["done"]), (job_id, "done", 3, 3))
        self.assertEqual(state["counts"], {"ok": 1, "auth_failed": 1, "no_creds": 1})
        self.assertEqual(sorted(state["failed"], key=lambda row: row["server_id"]), [
            {"server_id": 2, "name": "node-2", "status": "auth_failed", "detail": None, "login_level": "global"},
            {"server_id": 3, "name": "node-3", "status": "no_creds", "detail": None, "login_level": None},
        ])
        self.assertIsNotNone(state["finished_at"])

    async def test_refused_target_is_counted_without_connecting(self):
        self.manager.start([refused(3, "not_root")])
        state = await finished(self.manager)

        self.assertEqual(state["counts"], {"not_root": 1})
        self.assertEqual(self.seen, [])

    async def test_empty_scope_finishes_at_once(self):
        self.manager.start([])

        state = self.manager.state()
        self.assertEqual((state["status"], state["total"], state["done"]), ("done", 0, 0))

    async def test_logins_that_needed_the_proxy_are_counted(self):
        outcomes = iter([CheckOutcome(CheckStatus.OK, via_proxy=True), CheckOutcome(CheckStatus.OK)])

        async def scripted(_target: SSHTarget) -> CheckOutcome:
            return next(outcomes)

        with mock.patch.object(ssh_access_check, "check_target", scripted):
            self.manager.start([subject(1), subject(2)])
            state = await finished(self.manager)

        self.assertEqual((state["counts"], state["ok_via_proxy"]), ({"ok": 2}, 1))

    async def test_second_start_while_running_is_refused(self):
        self.blocked()
        self.manager.start([subject(1)])

        with self.assertRaises(CheckAlreadyRunning):
            self.manager.start([subject(2)])

    async def test_new_check_replaces_a_finished_one(self):
        self.manager.start([refused(1)])
        await finished(self.manager)

        self.manager.start([refused(2), refused(3)])
        state = await finished(self.manager)

        self.assertEqual((state["total"], [row["server_id"] for row in state["failed"]]), (2, [2, 3]))

    async def test_cancel_stops_the_rest(self):
        checks = self.blocked()
        with mock.patch.object(ssh_access_check, "CHECK_CONCURRENCY", 2):
            self.manager.start([subject(sid) for sid in range(1, 6)])
            await asyncio.sleep(0.05)

            self.assertTrue(self.manager.cancel())
            checks.release.set()
            await asyncio.sleep(0.05)

        state = self.manager.state()
        self.assertEqual((state["status"], state["done"], state["total"]), ("cancelled", 0, 5))
        self.assertEqual(checks.in_flight, 0)

    def test_nothing_to_cancel(self):
        self.assertFalse(self.manager.cancel())

    async def test_connections_are_bounded(self):
        checks = self.blocked()
        with mock.patch.object(ssh_access_check, "CHECK_CONCURRENCY", 2):
            self.manager.start([subject(sid) for sid in range(1, 7)])
            await asyncio.sleep(0.05)
            checks.release.set()
            state = await finished(self.manager)

        self.assertEqual((checks.peak, state["done"]), (2, 6))

    async def test_failed_list_is_capped_but_counts_are_full(self):
        with mock.patch.object(ssh_access_check, "FAILED_RESULTS_LIMIT", 2):
            self.manager.start([refused(sid) for sid in range(1, 6)])
            state = await finished(self.manager)

        self.assertEqual((len(state["failed"]), state["counts"]), (2, {"no_creds": 5}))

    async def test_unexpected_error_of_one_server_does_not_stop_the_check(self):
        async def broken(target: SSHTarget) -> CheckOutcome:
            if target.host == "10.0.0.1":
                raise RuntimeError("bug")
            return CheckOutcome(CheckStatus.OK)

        with mock.patch.object(ssh_access_check, "check_target", broken), self.assertLogs(ssh_access_check.logger):
            self.manager.start([subject(1), subject(2)])
            state = await finished(self.manager)

        self.assertEqual((state["status"], state["counts"]), ("done", {"unreachable": 1, "ok": 1}))

    async def test_old_result_is_forgotten(self):
        self.manager.start([])

        with mock.patch.object(ssh_access_check, "CHECK_RESULT_TTL_SECONDS", -1):
            self.assertIsNone(self.manager.state())


def node(sid: int, **fields) -> Server:
    return Server(**{
        "id": sid, "name": f"node-{sid}", "url": f"https://10.0.0.{sid}:9100", "is_active": True, **fields,
    })


class CheckSubjectsTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_server_gets_a_target_or_the_reason_of_refusal(self):
        db = FakeDb(servers=[node(1, folder="RU"), node(2)], profiles=[SshAccessProfile(folder="RU", ssh_password="f")])

        subjects = await ssh_access.check_subjects(db, AccessScope.GLOBAL, [], [])

        (first_id, first_name, target), (second_id, _second_name, refusal) = subjects
        self.assertEqual((first_id, first_name, target.password, target.login_level), (1, "node-1", "f", AccessLevel.FOLDER))
        self.assertEqual((second_id, refusal.reason), (2, "no_creds"))

    async def servers_query(self, scope: AccessScope, folders=(), server_ids=()) -> str:
        db = FakeDb()
        await ssh_access.check_subjects(db, scope, list(folders), list(server_ids))
        return str(db.queries[0].compile(compile_kwargs={"literal_binds": True}))

    async def test_only_active_servers_are_checked(self):
        sql = await self.servers_query(AccessScope.GLOBAL)

        self.assertIn("servers.is_active = true", sql)
        self.assertNotIn("servers.folder IN", sql)
        self.assertNotIn("servers.id IN", sql)

    async def test_scope_narrows_the_servers(self):
        by_folders = await self.servers_query(AccessScope.FOLDERS, folders=[" RU ", "DE"])
        by_ids = await self.servers_query(AccessScope.SERVERS, server_ids=[3, 7])

        self.assertIn("servers.folder IN ('RU', 'DE')", by_folders)
        self.assertIn("servers.id IN (3, 7)", by_ids)
        self.assertIn("servers.is_active = true", by_ids)


class CheckApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = SshAccessCheckManager()
        patcher = mock.patch.object(api, "get_ssh_access_check_manager", return_value=self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_check_of_a_scope_without_servers_is_done_at_once(self):
        started = await api.start_check(api.ScopeRequest(scope="folders", folders=["GONE"]), FakeDb())
        state = (await api.check_state())["check"]

        self.assertEqual((started, state["status"], state["total"]), ({"job_id": state["job_id"]}, "done", 0))

    async def test_nothing_checked_yet(self):
        self.assertEqual(await api.check_state(), {"check": None})
        self.assertEqual(await api.cancel_check(), {"cancelled": False})

    async def test_second_check_while_one_runs_is_a_conflict(self):
        release = asyncio.Event()
        self.addCleanup(release.set)

        async def waiting(_target: SSHTarget) -> CheckOutcome:
            await release.wait()
            return CheckOutcome(CheckStatus.OK)

        db = FakeDb(servers=[node(1, ssh_password="pw")])
        with mock.patch.object(ssh_access_check, "check_target", waiting):
            await api.start_check(api.ScopeRequest(scope="global"), db)
            with self.assertRaises(HTTPException) as caught:
                await api.start_check(api.ScopeRequest(scope="global"), db)

            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(await api.cancel_check(), {"cancelled": True})


if __name__ == "__main__":
    unittest.main()
