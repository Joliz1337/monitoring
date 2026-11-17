"""Ключ хоста SSH: доверие при первом подключении, сверка при следующих.

Панель входит на серверы под root с сохранённым паролем или ключом. Без сверки
ключа хоста любой узел на пути мог выдать себя за сервер и получить пароль.
Первое подключение запоминает ключ за сервером, дальше сервер обязан предъявить
тот же — иначе подключение рвётся до аутентификации. Сохранение SSH-доступа
заново сбрасывает запомненный ключ: переустановленный сервер снова доступен.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import asyncssh
from sqlalchemy.exc import OperationalError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ssh_fakes import FakeDb, LiveSshCase, public_text  # noqa: E402

from app.models import Server  # noqa: E402
from app.routers import node_image, servers  # noqa: E402
from app.services import deploy_job_manager, deploy_service, node_image_delivery, ssh_target  # noqa: E402
from app.services.deploy_job_manager import DeployJob, DeployJobManager, PostDeployOptions  # noqa: E402
from app.services.ssh_access import store_server_access  # noqa: E402
from app.services.ssh_target import (  # noqa: E402
    HostKeyChangedError,
    SshAccessRules,
    SSHTarget,
    resolve_ssh_target,
    ssh_connect_kwargs,
)


class OpenConnectionTests(LiveSshCase):
    async def test_first_connection_remembers_presented_key(self):
        await self.open(self.target(server_id=7))

        self.remember.assert_awaited_once_with(7, public_text(self.host_key))

    async def test_remembered_key_lets_the_same_server_in(self):
        await self.open(self.target(server_id=7, host_key=public_text(self.host_key)))

        self.assertIn("password", self.seen)
        self.remember.assert_not_awaited()

    async def test_changed_key_stops_before_credentials_are_sent(self):
        stranger = public_text(asyncssh.generate_private_key("ssh-ed25519"))

        with self.assertRaises(HostKeyChangedError) as caught:
            await self.open(self.target(server_id=7, host_key=stranger))

        self.assertEqual(self.seen, ["connect"])
        self.assertIn("пароль и ключ не отправлены", str(caught.exception))
        self.assertIn("127.0.0.1", str(caught.exception))
        self.remember.assert_not_awaited()

    async def test_reconnect_of_the_same_task_is_checked_against_first_key(self):
        target = self.target()
        kwargs = ssh_connect_kwargs(target)
        await self.open(target, kwargs)

        kwargs["port"] = await self.start_sshd(asyncssh.generate_private_key("ssh-ed25519"))
        with self.assertRaises(HostKeyChangedError):
            await self.open(target, kwargs)

    async def test_server_without_record_has_nowhere_to_save(self):
        await self.open(self.target())

        self.remember.assert_not_awaited()


class _RecordingSession:
    def __init__(self, failure: Exception | None = None):
        self.statements: list = []
        self.failure = failure
        self.commit = mock.AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, statement):
        if self.failure:
            raise self.failure
        self.statements.append(statement)
        return mock.MagicMock(rowcount=1)


class RememberHostKeyTests(unittest.IsolatedAsyncioTestCase):
    async def remember(self, session: _RecordingSession) -> None:
        with mock.patch.object(ssh_target, "async_session_maker", return_value=session):
            await ssh_target._remember_host_key(7, "ssh-ed25519 AAAA")

    async def test_key_is_written_only_where_none_is_pinned_yet(self):
        session = _RecordingSession()
        await self.remember(session)

        sql = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("ssh_host_key='ssh-ed25519 AAAA'", sql)
        self.assertIn("servers.id = 7", sql)
        self.assertIn("servers.ssh_host_key IS NULL", sql)
        session.commit.assert_awaited_once()

    async def test_database_failure_does_not_break_the_connection(self):
        await self.remember(_RecordingSession(OperationalError("UPDATE", {}, Exception("db is down"))))


class ResolveTargetTests(unittest.TestCase):
    def test_target_carries_server_and_its_pinned_key(self):
        server = Server(id=5, name="n", url="https://10.0.0.5:9100", ssh_password="x", ssh_host_key="KEY")

        target = resolve_ssh_target(server, SshAccessRules())

        self.assertEqual((target.server_id, target.host_key), (5, "KEY"))


class InstallViaPanelTests(LiveSshCase):
    async def events(self, target: SSHTarget) -> list[dict]:
        return [e async for e in deploy_service.install_via_panel(target, lambda proxy: "install")]

    async def test_changed_key_is_one_clear_error(self):
        stranger = public_text(asyncssh.generate_private_key("ssh-ed25519"))

        events = await self.events(self.target(server_id=7, host_key=stranger))

        self.assertEqual(events, [{"type": "error", "message": str(HostKeyChangedError("127.0.0.1"))}])
        self.assertNotIn("auth", self.seen)

    async def test_first_install_pins_key_to_the_server(self):
        events = await self.events(self.target(server_id=7))

        self.assertEqual(events[-1], {"type": "done", "exit_code": 0})
        self.remember.assert_awaited_once_with(7, public_text(self.host_key))


class DeliveryConnectTests(LiveSshCase):
    async def test_changed_key_is_not_retried(self):
        target = self.target(server_id=7, host_key=public_text(asyncssh.generate_private_key("ssh-ed25519")))

        with mock.patch.object(node_image_delivery, "SSH_CONNECT_RETRY_DELAY", 0):
            events = [e async for e in node_image_delivery._connect(target, ssh_connect_kwargs(target))]

        self.assertEqual(events, [{"type": "error", "message": str(HostKeyChangedError("127.0.0.1"))}])
        self.assertEqual(self.seen, ["connect"])


class DeployNodeTests(LiveSshCase):
    async def test_done_event_carries_key_accepted_on_first_use(self):
        params = deploy_service.DeployParams(
            host="127.0.0.1", ssh_port=self.port, ssh_user="root", node_secret="secret", ssh_password="pw",
        )

        events = [e async for e in deploy_service.deploy_node(params)]

        self.assertEqual(events[-1], {"type": "done", "exit_code": 0, "host_key": public_text(self.host_key)})

    async def test_password_change_stops_on_changed_key(self):
        stranger = public_text(asyncssh.generate_private_key("ssh-ed25519"))
        kwargs = ssh_connect_kwargs(self.target(host_key=stranger))

        events = [e async for e in deploy_service._change_expired_password(kwargs, "pw", "new-pw", None)]

        self.assertEqual(
            events, [{"type": "pwchange", "ok": False, "message": str(HostKeyChangedError("127.0.0.1"))}],
        )
        self.assertNotIn("auth", self.seen)


class DeployPasswordChangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_with_new_password_keeps_key_of_first_connection(self):
        runs: list[dict] = []

        async def install_once(connect_kwargs, _params):
            runs.append(dict(connect_kwargs))
            if len(runs) == 1:
                connect_kwargs["known_hosts"] = "KEY OF FIRST CONNECTION"
                yield {"type": "_result", "exit_code": 1, "expired": True, "host_key": "KEY"}
                return
            yield {"type": "_result", "exit_code": 0, "expired": False, "host_key": "KEY"}

        async def change_password(*_args):
            yield {"type": "pwchange", "ok": True}

        params = deploy_service.DeployParams(
            host="203.0.113.10", ssh_port=22, ssh_user="root", node_secret="secret",
            ssh_password="expired", new_password="fresh-password",
        )
        with mock.patch.object(deploy_service, "_run_install_once", install_once), \
                mock.patch.object(deploy_service, "_change_expired_password", change_password):
            events = [e async for e in deploy_service.deploy_node(params)]

        self.assertIsNone(runs[0]["known_hosts"])
        self.assertEqual(runs[1]["known_hosts"], "KEY OF FIRST CONNECTION")
        self.assertEqual(runs[1]["password"], "fresh-password")
        self.assertEqual(events[-1], {"type": "done", "exit_code": 0, "host_key": "KEY"})


class DeployJobHostKeyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = DeployJobManager()
        self.created: dict = {}

        async def create_server(*args, **kwargs):
            self.created.update(kwargs)
            return 42

        self.manager._create_server = create_server
        for step in ("_wait_node_ready", "_post_install", "_bind_profiles",
                     "_apply_wildcard_ssl", "_bind_remnawave_nginx", "_on_rescue_reboot"):
            setattr(self.manager, step, mock.AsyncMock())
        self.job = DeployJob(id="job", name="node-1", host="203.0.113.10", server_url="https://203.0.113.10:9100")

    async def run_install(self, done_event: dict) -> None:
        async def install(_params):
            yield done_event

        with mock.patch.object(deploy_job_manager, "deploy_node", install):
            await self.manager._run(self.job, SimpleNamespace(), PostDeployOptions())

    async def test_key_seen_during_install_goes_to_created_server(self):
        await self.run_install({"type": "done", "exit_code": 0, "host_key": "ssh-ed25519 AAAA"})

        self.assertEqual(self.created["ssh_host_key"], "ssh-ed25519 AAAA")

    async def test_rescue_system_key_is_not_kept(self):
        await self.run_install({"type": "done", "exit_code": 0, "rescue": True, "host_key": "ssh-ed25519 AAAA"})

        self.manager._on_rescue_reboot.assert_awaited_once()
        self.assertIsNone(self.job.ssh_host_key)


def pinned_server(**fields) -> Server:
    return Server(id=1, name="node-1", url="https://10.0.0.1:9100", position=1, ssh_host_key="KEY", **fields)


class StoreCredsTests(unittest.TestCase):
    def store(self, server: Server, **access) -> None:
        request = node_image.SSHAccessRequest(**access)
        store_server_access(server, request.to_change(), ssh_host=request.ssh_host)

    def test_retyped_secret_resets_pinned_key(self):
        for secret in ({"ssh_password": "same"}, {"ssh_private_key": "KEY"}, {"ssh_password": ""}):
            server = pinned_server(ssh_port=22, ssh_user="root", ssh_password="same")
            self.store(server, ssh_port=22, ssh_user="root", **secret)
            self.assertIsNone(server.ssh_host_key, secret)

    def test_new_address_resets_pinned_key(self):
        for change in ({"ssh_host": "10.0.0.9"}, {"ssh_port": 2222}, {"ssh_user": "admin"}):
            server = pinned_server(ssh_host="10.0.0.1", ssh_port=22, ssh_user="root")
            self.store(server, **change)
            self.assertIsNone(server.ssh_host_key, change)

    def test_saving_the_same_address_without_secret_keeps_pinned_key(self):
        server = pinned_server(ssh_host="10.0.0.1", ssh_port=22, ssh_user="root", ssh_password="pw")

        self.store(server, ssh_host="10.0.0.1", ssh_port=22, ssh_user="root")

        self.assertEqual(server.ssh_host_key, "KEY")


class BulkSaveCredsTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_access_starts_delivery_without_old_key(self):
        server = pinned_server()
        manager = mock.MagicMock()
        request = node_image.BulkDeliverRequest(server_ids=[1], ssh_password="shared", save_creds=True)

        with mock.patch.object(node_image, "get_image_delivery_manager", return_value=manager), \
                mock.patch.object(node_image.update_channel, "current_image_tag", return_value="latest"):
            await node_image.deliver_image_bulk(request, FakeDb([server]))

        target = manager.start.call_args.args[2]
        self.assertIsNone(server.ssh_host_key)
        self.assertIsNone(target.host_key)
        self.assertEqual(target.password, "shared")


class ServerUrlChangeTests(unittest.IsolatedAsyncioTestCase):
    async def update_url(self, server: Server, url: str) -> None:
        blocklist = mock.MagicMock(sync_single_node_by_id=mock.AsyncMock())
        with mock.patch.object(servers, "get_blocklist_manager", return_value=blocklist):
            await servers.update_server(1, servers.ServerUpdate(url=url), FakeDb([server]))

    async def test_new_address_forgets_key_of_previous_machine(self):
        server = pinned_server()

        await self.update_url(server, "https://10.0.0.77:9100")

        self.assertIsNone(server.ssh_host_key)

    async def test_new_api_port_on_same_address_keeps_key(self):
        server = pinned_server()

        await self.update_url(server, "https://10.0.0.1:9443")

        self.assertEqual(server.ssh_host_key, "KEY")

    async def test_explicit_ssh_host_does_not_follow_url(self):
        server = pinned_server(ssh_host="192.0.2.5")

        await self.update_url(server, "https://10.0.0.77:9100")

        self.assertEqual(server.ssh_host_key, "KEY")


if __name__ == "__main__":
    unittest.main()
