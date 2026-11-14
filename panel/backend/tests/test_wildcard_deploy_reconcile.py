"""Сверка раскатки wildcard-сертификатов (app/services/wildcard_ssl.py).

Голый unittest, без PostgreSQL и без сети: база, нода и Telegram подменены.

После автопродления сертификат раскатывался один раз: нода, которая в этот момент
была без связи или ответила ошибкой, оставалась со старым сертификатом до его
истечения. Теперь панель помнит отпечаток того, что лежит на каждом сервере, и
раз в час довозит сертификаты туда, где отпечаток разошёлся с нужным.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import wildcard_ssl  # noqa: E402
from app.services.wildcard_ssl import (  # noqa: E402
    NOTIFY_TEXT_CHARS,
    ReconcileState,
    WildcardSSLManager,
    deployment_fingerprint,
)

RELOAD = "systemctl reload nginx"
OFFLINE_THRESHOLD = 60
ALERTS = SimpleNamespace(language="ru", telegram_bot_token="123:token", telegram_chat_id="-100")


def make_server(server_id: int = 1, **overrides) -> SimpleNamespace:
    fields = {
        "id": server_id,
        "name": f"node-{server_id}",
        "url": f"https://node-{server_id}.example:9100",
        "last_seen": datetime.now(timezone.utc),
        "last_error": None,
        "wildcard_ssl_deploy_path": None,
        "wildcard_ssl_reload_cmd": RELOAD,
        "wildcard_ssl_fullchain_name": None,
        "wildcard_ssl_privkey_name": None,
        "wildcard_ssl_custom_path_enabled": False,
        "wildcard_ssl_custom_fullchain_path": None,
        "wildcard_ssl_custom_privkey_path": None,
        "wildcard_ssl_deployed_fingerprint": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def make_cert(cert_id: int = 1, base_domain: str = "a.example", pem: str = "CHAIN-1", days_left: int = 60):
    return SimpleNamespace(
        id=cert_id,
        domain=f"*.{base_domain}",
        base_domain=base_domain,
        fullchain_pem=pem,
        privkey_pem="KEY",
        expiry_date=datetime.now(timezone.utc) + timedelta(days=days_left),
    )


class FingerprintTests(unittest.TestCase):
    def test_same_state_gives_same_fingerprint(self):
        self.assertEqual(
            deployment_fingerprint(make_server(), [make_cert()]),
            deployment_fingerprint(make_server(), [make_cert()]),
        )
        self.assertEqual(len(deployment_fingerprint(make_server(), [make_cert()])), 64)

    def test_renewed_certificate_changes_fingerprint(self):
        self.assertNotEqual(
            deployment_fingerprint(make_server(), [make_cert(pem="CHAIN-1")]),
            deployment_fingerprint(make_server(), [make_cert(pem="CHAIN-2")]),
        )

    def test_every_deploy_setting_changes_fingerprint(self):
        cert = make_cert()
        base = deployment_fingerprint(make_server(), [cert])
        changed_settings = (
            {"wildcard_ssl_reload_cmd": "systemctl reload haproxy"},
            {"wildcard_ssl_deploy_path": "/etc/nginx/ssl"},
            {"wildcard_ssl_fullchain_name": "cert.pem"},
            {"wildcard_ssl_privkey_name": "cert.key"},
            {
                "wildcard_ssl_custom_path_enabled": True,
                "wildcard_ssl_custom_fullchain_path": "/etc/pve/local/pveproxy-ssl.pem",
                "wildcard_ssl_custom_privkey_path": "/etc/pve/local/pveproxy-ssl.key",
            },
        )
        for overrides in changed_settings:
            with self.subTest(overrides=overrides):
                self.assertNotEqual(base, deployment_fingerprint(make_server(**overrides), [cert]))

    def test_default_path_written_out_is_the_same_deploy(self):
        self.assertEqual(
            deployment_fingerprint(make_server(), [make_cert()]),
            deployment_fingerprint(make_server(wildcard_ssl_deploy_path="/etc/letsencrypt/live/"), [make_cert()]),
        )

    def test_certificate_set_matters_but_not_its_order(self):
        first, second = make_cert(1, "a.example"), make_cert(2, "b.example")
        server = make_server()
        self.assertEqual(
            deployment_fingerprint(server, [first, second]),
            deployment_fingerprint(server, [second, first]),
        )
        self.assertNotEqual(
            deployment_fingerprint(server, [first, second]),
            deployment_fingerprint(server, [first]),
        )


class NodeRequestTests(unittest.IsolatedAsyncioTestCase):
    """Запрос к ноде остался прежним: ноды старых версий принимают те же поля."""

    async def push(self, server: SimpleNamespace) -> dict:
        response = SimpleNamespace(status_code=200, json=lambda: {"success": True, "message": "ok"})
        client = SimpleNamespace(post=mock.AsyncMock(return_value=response))
        with mock.patch.object(wildcard_ssl, "get_node_client", return_value=client), \
                mock.patch.object(wildcard_ssl, "node_auth_headers", return_value={}):
            ok, _, _ = await WildcardSSLManager()._push_certificate(server, make_cert(), RELOAD)
        self.assertTrue(ok)
        self.assertEqual(client.post.await_args.args, (f"{server.url}/api/ssl/wildcard/deploy",))
        return client.post.await_args.kwargs["json"]

    async def test_folder_mode_with_defaults(self):
        self.assertEqual(await self.push(make_server()), {
            "fullchain_pem": "CHAIN-1",
            "privkey_pem": "KEY",
            "reload_command": RELOAD,
            "deploy_path": "/etc/letsencrypt/live/a.example",
        })

    async def test_folder_mode_with_own_path_and_file_names(self):
        server = make_server(
            wildcard_ssl_deploy_path="/etc/nginx/ssl/",
            wildcard_ssl_fullchain_name="cert.pem",
            wildcard_ssl_privkey_name="cert.key",
        )
        self.assertEqual(await self.push(server), {
            "fullchain_pem": "CHAIN-1",
            "privkey_pem": "KEY",
            "reload_command": RELOAD,
            "deploy_path": "/etc/nginx/ssl/a.example",
            "fullchain_filename": "cert.pem",
            "privkey_filename": "cert.key",
        })

    async def test_custom_paths(self):
        server = make_server(
            wildcard_ssl_custom_path_enabled=True,
            wildcard_ssl_custom_fullchain_path="/etc/pve/local/pveproxy-ssl.pem",
            wildcard_ssl_custom_privkey_path="/etc/pve/local/pveproxy-ssl.key",
        )
        self.assertEqual(await self.push(server), {
            "fullchain_pem": "CHAIN-1",
            "privkey_pem": "KEY",
            "reload_command": RELOAD,
            "custom_fullchain_path": "/etc/pve/local/pveproxy-ssl.pem",
            "custom_privkey_path": "/etc/pve/local/pveproxy-ssl.key",
        })


class ManagerTestCase(unittest.IsolatedAsyncioTestCase):
    """Менеджер с подменённой нодой и базой: отпечаток пишется прямо в объект сервера."""

    def setUp(self):
        self.manager = WildcardSSLManager()
        self.servers: dict[int, SimpleNamespace] = {}
        self.pushed: list[tuple[int, int]] = []
        self.node_errors: dict[int, str | Exception] = {}

        async def push(server, cert, reload_command):
            self.pushed.append((server.id, cert.id))
            error = self.node_errors.get(server.id)
            if isinstance(error, Exception):
                raise error
            if error:
                return False, error, None
            return True, "Certificate deployed successfully", None

        async def remember(server_id, fingerprint):
            self.servers[server_id].wildcard_ssl_deployed_fingerprint = fingerprint

        self.manager._push_certificate = push
        self.manager._remember_deployed = remember

    def add_server(self, server_id: int = 1, **overrides) -> SimpleNamespace:
        self.servers[server_id] = make_server(server_id, **overrides)
        return self.servers[server_id]


class RememberDeployedTests(ManagerTestCase):
    async def test_full_deploy_remembers_what_lies_on_the_server(self):
        server, certs = self.add_server(), [make_cert(1, "a.example"), make_cert(2, "b.example")]
        result = await self.manager.deploy_to_node(server, certs)
        self.assertTrue(result["success"])
        self.assertEqual(server.wildcard_ssl_deployed_fingerprint, deployment_fingerprint(server, certs))

    async def test_failed_deploy_remembers_nothing(self):
        server = self.add_server()
        self.node_errors[server.id] = "Reload command failed (rolled back): boom"
        result = await self.manager.deploy_to_node(server, [make_cert()])
        self.assertFalse(result["success"])
        self.assertIsNone(server.wildcard_ssl_deployed_fingerprint)

    async def test_one_certificate_of_several_does_not_confirm_the_rest(self):
        server, certs = self.add_server(), [make_cert(1, "a.example"), make_cert(2, "b.example")]
        result = await self.manager.deploy_to_node(server, certs, cert_id=2)
        self.assertTrue(result["success"])
        self.assertIsNone(server.wildcard_ssl_deployed_fingerprint)

    async def test_filter_by_the_only_certificate_is_a_full_deploy(self):
        server, certs = self.add_server(), [make_cert(1, "a.example")]
        await self.manager.deploy_to_node(server, certs, cert_id=1)
        self.assertEqual(server.wildcard_ssl_deployed_fingerprint, deployment_fingerprint(server, certs))


class ReconcileTests(ManagerTestCase):
    def setUp(self):
        super().setUp()
        self.certs: dict[int, list[SimpleNamespace]] = {}
        self.auto_renew = True
        self.stored_failures: dict[int, str] = {}
        self.notifications: list[str] = []

        async def load_state():
            if not self.auto_renew:
                return None
            return ReconcileState(OFFLINE_THRESHOLD, ALERTS, dict(self.stored_failures))

        async def save_failures(failed):
            self.stored_failures = dict(failed)

        async def targets():
            return [(server, self.certs[server.id]) for server in self.servers.values()]

        async def notify(settings, message):
            self.notifications.append(message)

        self.manager._load_reconcile_state = load_state
        self.manager._save_failed_deploys = save_failures
        self.manager.get_deploy_targets = targets
        self.manager._notify = notify

        patcher = mock.patch.object(wildcard_ssl, "logger")
        self.logger = patcher.start()
        self.addCleanup(patcher.stop)

    def add_server(self, server_id: int = 1, certs: list | None = None, **overrides) -> SimpleNamespace:
        self.certs[server_id] = certs if certs is not None else [make_cert()]
        return super().add_server(server_id, **overrides)

    async def reconcile(self, retry_failed: bool = False) -> list[tuple[int, int]]:
        """Проход сверки; возвращает раскатки именно этого прохода."""
        self.pushed.clear()
        await self.manager.reconcile_deployments(retry_failed=retry_failed)
        return list(self.pushed)

    async def test_server_enabled_before_fingerprints_gets_certificate_once(self):
        self.add_server()
        self.assertEqual(await self.reconcile(), [(1, 1)])
        self.assertEqual(await self.reconcile(), [])
        self.assertEqual(self.notifications, [])

    async def test_renewed_certificate_reaches_the_server(self):
        self.add_server()
        await self.reconcile()
        self.certs[1] = [make_cert(pem="CHAIN-2")]
        self.assertEqual(await self.reconcile(), [(1, 1)])
        self.assertEqual(await self.reconcile(), [])

    async def test_changed_settings_redeploy_the_server(self):
        server = self.add_server()
        await self.reconcile()
        server.wildcard_ssl_reload_cmd = "systemctl reload haproxy"
        self.assertEqual(await self.reconcile(), [(1, 1)])
        server.wildcard_ssl_deploy_path = "/etc/haproxy/certs"
        self.assertEqual(await self.reconcile(), [(1, 1)])
        self.assertEqual(await self.reconcile(), [])

    async def test_switched_off_auto_renew_deploys_nothing(self):
        self.add_server()
        self.auto_renew = False
        self.assertEqual(await self.reconcile(retry_failed=True), [])

    async def test_offline_server_waits_silently_and_is_served_when_back(self):
        server = self.add_server(last_seen=datetime.now(timezone.utc) - timedelta(hours=3))
        self.assertEqual(await self.reconcile(retry_failed=True), [])
        self.assertEqual(self.notifications, [])
        self.assertEqual(self.stored_failures, {})

        server.last_seen = datetime.now(timezone.utc)
        self.assertEqual(await self.reconcile(), [(1, 1)])

    async def test_never_seen_server_is_not_touched(self):
        self.add_server(last_seen=None)
        self.assertEqual(await self.reconcile(), [])

    async def test_expired_certificate_is_not_deployed(self):
        self.add_server(1, certs=[make_cert(1, "a.example"), make_cert(2, "b.example", days_left=-1)])
        self.add_server(2, certs=[make_cert(2, "b.example", days_left=-1)])
        self.assertEqual(await self.reconcile(), [(1, 1)])
        self.assertEqual(await self.reconcile(), [])

    async def test_failure_is_reported_once_with_the_list_of_nodes(self):
        self.add_server(1)
        self.add_server(2)
        self.add_server(3)
        self.node_errors = {1: "Reload command failed (rolled back): nginx <emerg>", 3: "HTTP 500"}

        await self.reconcile(retry_failed=True)

        self.assertEqual(len(self.notifications), 1)
        message = self.notifications[0]
        self.assertIn("на 2 из 3 нод", message)
        self.assertIn("• node-1 (*.a.example): Reload command failed (rolled back): nginx <emerg>", message)
        self.assertIn("• node-3 (*.a.example): HTTP 500", message)
        self.assertNotIn("node-2", message)
        self.assertEqual(set(self.stored_failures), {1, 3})
        self.assertIn("node-3", self.logger.error.call_args.args[0])

    async def test_crash_on_one_node_does_not_stop_the_others(self):
        self.add_server(1)
        self.add_server(2)
        self.node_errors = {1: RuntimeError("unexpected")}

        await self.reconcile(retry_failed=True)

        self.assertIsNotNone(self.servers[2].wildcard_ssl_deployed_fingerprint)
        self.assertIn("• node-1 (*.a.example): unexpected", self.notifications[0])
        self.assertEqual(set(self.stored_failures), {1})

    async def test_hourly_pass_leaves_reported_failure_alone(self):
        self.add_server()
        self.node_errors = {1: "Reload command failed"}
        await self.reconcile(retry_failed=True)

        self.assertEqual(await self.reconcile(), [])
        self.assertEqual(await self.reconcile(), [])
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(set(self.stored_failures), {1})

    async def test_daily_pass_retries_reported_failure_without_new_notification(self):
        self.add_server()
        self.node_errors = {1: "Reload command failed"}
        await self.reconcile(retry_failed=True)

        self.assertEqual(await self.reconcile(retry_failed=True), [(1, 1)])
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(set(self.stored_failures), {1})

    async def test_successful_retry_forgets_the_failure(self):
        server = self.add_server()
        self.node_errors = {1: "Reload command failed"}
        await self.reconcile(retry_failed=True)

        self.node_errors = {}
        self.assertEqual(await self.reconcile(retry_failed=True), [(1, 1)])
        self.assertEqual(self.stored_failures, {})
        self.assertIsNotNone(server.wildcard_ssl_deployed_fingerprint)

    async def test_new_certificate_version_is_retried_at_once_and_reported_again(self):
        self.add_server()
        self.node_errors = {1: "Reload command failed"}
        await self.reconcile(retry_failed=True)

        self.certs[1] = [make_cert(pem="CHAIN-2")]
        self.assertEqual(await self.reconcile(), [(1, 1)])
        self.assertEqual(len(self.notifications), 2)

    async def test_failure_survives_the_node_going_offline(self):
        server = self.add_server()
        self.node_errors = {1: "Reload command failed"}
        await self.reconcile(retry_failed=True)

        server.last_seen = datetime.now(timezone.utc) - timedelta(hours=3)
        self.assertEqual(await self.reconcile(retry_failed=True), [])
        self.assertEqual(set(self.stored_failures), {1})

        server.last_seen = datetime.now(timezone.utc)
        await self.reconcile(retry_failed=True)
        self.assertEqual(len(self.notifications), 1)

    async def test_manual_deploy_closes_the_reported_failure(self):
        server = self.add_server()
        self.node_errors = {1: "Reload command failed"}
        await self.reconcile(retry_failed=True)

        self.node_errors = {}
        await self.manager.deploy_to_node(server, self.certs[1])
        self.assertEqual(await self.reconcile(), [])
        self.assertEqual(self.stored_failures, {})

    async def test_failure_of_a_removed_server_is_forgotten(self):
        self.add_server()
        self.stored_failures = {99: "0" * 64}
        await self.reconcile()
        self.assertEqual(self.stored_failures, {})

    async def test_long_failure_list_fits_into_one_telegram_message(self):
        for server_id in range(1, 61):
            self.add_server(server_id)
            self.node_errors[server_id] = "Reload command failed (rolled back): " + "x" * 600

        await self.reconcile(retry_failed=True)

        self.assertEqual(len(self.notifications), 1)
        self.assertLess(len(self.notifications[0]), NOTIFY_TEXT_CHARS)
        self.assertIn("на 60 из 60 нод", self.notifications[0])
        self.assertRegex(self.notifications[0], r"… \+\d+")
        self.assertEqual(len(self.stored_failures), 60)


class DeployFailedMessageTests(unittest.TestCase):
    def test_message_is_written_in_the_language_of_notifications(self):
        failed = [{"server_name": "node-1", "domains": "*.a.example", "message": "boom"}]
        for language, summary in (("ru", "на 1 из 4 нод"), ("en", "on 1 of 4 nodes")):
            with self.subTest(language=language):
                text = WildcardSSLManager()._msg_deploy_failed(SimpleNamespace(language=language), failed, 4)
                self.assertIn(summary, text)
                self.assertIn("• node-1 (*.a.example): boom", text)

    def test_failure_without_a_reason_still_names_the_node(self):
        failed = [{"server_name": "node-1", "domains": "*.a.example", "message": ""}]
        self.assertIn("• node-1 (*.a.example): ?", WildcardSSLManager._failure_lines(failed))


class FakeSession:
    """Подмена async_session: запросы копит, на чтение отдаёт заданную строку."""

    def __init__(self, row: object = None, error: Exception | None = None) -> None:
        self.row = row
        self.error = error
        self.statements: list = []
        self.added: list = []
        self.committed = False

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def execute(self, statement) -> SimpleNamespace:
        if self.error:
            raise self.error
        self.statements.append(statement)
        return SimpleNamespace(scalar_one_or_none=lambda: self.row)

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.committed = True


class StorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = WildcardSSLManager()

    def use(self, session: FakeSession) -> None:
        patcher = mock.patch.object(wildcard_ssl, "async_session", lambda: session)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_fingerprint_is_written_to_the_server_row(self):
        session = FakeSession()
        self.use(session)
        await self.manager._remember_deployed(5, "ab" * 32)

        compiled = session.statements[0].compile(dialect=postgresql.dialect())
        self.assertIn("UPDATE servers SET", str(compiled))
        self.assertEqual(compiled.params["wildcard_ssl_deployed_fingerprint"], "ab" * 32)
        self.assertIn(5, compiled.params.values())
        self.assertTrue(session.committed)

    async def test_database_failure_does_not_turn_a_done_deploy_into_an_error(self):
        for error in (OperationalError("UPDATE servers", {}, Exception("db is down")), ConnectionRefusedError()):
            with self.subTest(error=type(error).__name__):
                self.use(FakeSession(error=error))
                with self.assertLogs(wildcard_ssl.logger, level="ERROR"):
                    await self.manager._remember_deployed(5, "ab" * 32)

    async def test_switched_off_auto_renew_gives_no_state(self):
        self.use(FakeSession())
        self.manager._get_setting = mock.AsyncMock(return_value="false")
        self.assertIsNone(await self.manager._load_reconcile_state())

    async def test_state_carries_reported_failures(self):
        self.use(FakeSession())
        settings = {
            wildcard_ssl.AUTO_RENEW_SETTING: "true",
            wildcard_ssl.FAILED_DEPLOYS_SETTING: json.dumps({"abc": [7, 9]}),
        }
        self.manager._get_setting = mock.AsyncMock(side_effect=lambda db, key: settings.get(key))

        state = await self.manager._load_reconcile_state()

        self.assertEqual(state.failed, {7: "abc", 9: "abc"})
        self.assertEqual(state.offline_threshold, 60)
        self.assertIsNone(state.alerts)

    async def test_failures_are_saved_into_a_new_settings_row(self):
        session = FakeSession()
        self.use(session)
        await self.manager._save_failed_deploys({7: "abc"})

        self.assertEqual(len(session.added), 1)
        self.assertEqual(session.added[0].key, wildcard_ssl.FAILED_DEPLOYS_SETTING)
        self.assertEqual(json.loads(session.added[0].value), {"abc": [7]})
        self.assertTrue(session.committed)

    async def test_failures_replace_the_existing_settings_row(self):
        row = SimpleNamespace(value=json.dumps({"abc": [7]}))
        session = FakeSession(row=row)
        self.use(session)
        await self.manager._save_failed_deploys({})

        self.assertEqual(row.value, "{}")
        self.assertEqual(session.added, [])
        self.assertTrue(session.committed)

    def test_failures_round_trip_through_json(self):
        failed = {1: "abc", 2: "abc", 3: "def"}
        dumped = WildcardSSLManager._dump_failed_deploys(failed)
        self.assertEqual(json.loads(dumped), {"abc": [1, 2], "def": [3]})
        self.assertEqual(WildcardSSLManager._parse_failed_deploys(dumped), failed)

    def test_missing_or_broken_value_is_empty(self):
        for raw in (None, "", "not json", "[1, 2]", '{"abc": 7}', '{"abc": ["x"]}'):
            with self.subTest(raw=raw):
                self.assertEqual(WildcardSSLManager._parse_failed_deploys(raw), {})


class PassTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = WildcardSSLManager()
        self.calls: list[str] = []

        async def renew():
            self.calls.append("renew")

        async def reconcile(retry_failed):
            self.calls.append(f"reconcile retry_failed={retry_failed}")

        self.manager._check_and_renew = renew
        self.manager.reconcile_deployments = reconcile

    async def test_daily_pass_renews_then_reconciles_with_retries(self):
        await self.manager._run_pass(renew=True)
        self.assertEqual(self.calls, ["renew", "reconcile retry_failed=True"])

    async def test_hourly_pass_only_reconciles(self):
        await self.manager._run_pass(renew=False)
        self.assertEqual(self.calls, ["reconcile retry_failed=False"])

    async def test_failed_renew_check_does_not_cancel_reconcile(self):
        self.manager._check_and_renew = mock.AsyncMock(side_effect=RuntimeError("certbot crashed"))
        with self.assertLogs(wildcard_ssl.logger, level="ERROR"):
            await self.manager._run_pass(renew=True)
        self.assertEqual(self.calls, ["reconcile retry_failed=True"])

    async def test_failed_reconcile_does_not_escape_the_loop(self):
        self.manager.reconcile_deployments = mock.AsyncMock(side_effect=RuntimeError("db is down"))
        with self.assertLogs(wildcard_ssl.logger, level="ERROR"):
            await self.manager._run_pass(renew=False)


class FakeRenewSession:
    """Подмена async_session для _check_and_renew: сертификаты и настройки алертов."""

    def __init__(self, certs: list) -> None:
        self.certs = certs

    async def __aenter__(self) -> "FakeRenewSession":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def execute(self, _statement) -> SimpleNamespace:
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: list(self.certs)),
            scalar_one_or_none=lambda: ALERTS,
        )


class AutoRenewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = WildcardSSLManager()
        settings = {"wildcard_auto_renew_enabled": "true", "wildcard_renew_days_before": "30"}
        self.manager._get_setting = mock.AsyncMock(side_effect=lambda db, key: settings.get(key))
        self.manager.renew_certificate = mock.AsyncMock(return_value=(True, "renewed"))
        self.manager.deploy_to_node = mock.AsyncMock()
        self.manager._notify = mock.AsyncMock()

    async def check(self, certs: list) -> None:
        with mock.patch.object(wildcard_ssl, "async_session", lambda: FakeRenewSession(certs)):
            await self.manager._check_and_renew()

    async def test_due_certificate_is_renewed_and_left_to_reconcile(self):
        await self.check([make_cert(1, days_left=10), make_cert(2, "b.example", days_left=80)])
        self.manager.renew_certificate.assert_awaited_once_with(1)
        self.manager.deploy_to_node.assert_not_awaited()
        self.manager._notify.assert_not_awaited()

    async def test_failed_renewal_is_reported(self):
        self.manager.renew_certificate.return_value = (False, "DNS problem: NXDOMAIN")
        with self.assertLogs(wildcard_ssl.logger, level="ERROR"):
            await self.check([make_cert(1, days_left=10)])
        self.manager._notify.assert_awaited_once()
        message = self.manager._notify.await_args.args[1]
        self.assertIn("*.a.example", message)
        self.assertIn("DNS problem: NXDOMAIN", message)


class NotifyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.telegram = SimpleNamespace(send_message=mock.AsyncMock(return_value=True))
        patcher = mock.patch(
            "app.services.telegram_bot.get_telegram_bot_service", return_value=self.telegram,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = WildcardSSLManager()

    async def test_message_is_queued_for_retry_when_telegram_is_unreachable(self):
        await self.manager._notify(ALERTS, "Не удалось продлить")
        self.telegram.send_message.assert_awaited_once()
        call = self.telegram.send_message.await_args
        self.assertEqual(call.args[:2], ("123:token", "-100"))
        self.assertIn("Не удалось продлить", call.args[2])
        self.assertEqual(call.kwargs, {"retry_later": True})

    async def test_node_output_cannot_break_telegram_markup(self):
        await self.manager._notify(ALERTS, 'nginx: unexpected "<" in a.conf & b.conf')
        text = self.telegram.send_message.await_args.args[2]
        self.assertIn("<b>Wildcard SSL</b>", text)
        self.assertIn('unexpected "&lt;" in a.conf &amp; b.conf', text)

    async def test_overlong_message_is_cut_to_telegram_limit(self):
        await self.manager._notify(ALERTS, "x" * 10_000)
        self.assertLess(len(self.telegram.send_message.await_args.args[2]), 4096)

    async def test_unconfigured_telegram_is_skipped(self):
        await self.manager._notify(None, "text")
        await self.manager._notify(SimpleNamespace(telegram_bot_token="", telegram_chat_id="-100"), "text")
        self.telegram.send_message.assert_not_awaited()

    async def test_telegram_failure_does_not_escape(self):
        self.telegram.send_message.side_effect = RuntimeError("telegram client crashed")
        with self.assertLogs(wildcard_ssl.logger, level="ERROR"):
            await self.manager._notify(ALERTS, "text")


if __name__ == "__main__":
    unittest.main()
