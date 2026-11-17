"""Какой SSH-доступ достаётся серверу: свой, папки или общий.

Доступ собирается из трёх частей, каждая берётся с ближайшего уровня, где
задана: вход (пользователь с паролем либо ключом — целиком), порт и SSH-прокси.
Так работают «пароль общий, а у этого сервера порт 2222» и «пароль общий, а у
папки свой прокси». Этим доступом пользуются все места, где панель ходит по SSH
на уже добавленный сервер, — иначе нода, не сумевшая обновиться сама, осталась
бы без запасного пути, хотя пароль у панели есть.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ssh_fakes import FakeDb  # noqa: E402

from app.models import NodeUpdateAttempt, Server, SshAccessProfile  # noqa: E402
from app.routers import node_image, remnawave_install, system  # noqa: E402
from app.services import haproxy_upgrade, node_update_watcher  # noqa: E402
from app.services.node_update_watcher import Decision, FailureReason, FallbackProblem, Verdict  # noqa: E402
from app.services.ssh_target import (  # noqa: E402
    AccessLevel,
    LevelAccess,
    SshAccessRules,
    SSHTargetError,
    login_level,
    resolve_ssh_target,
    ssh_routes,
)

PROXY = "1.2.3.4:1080"


def server(**fields) -> Server:
    return Server(**{"id": 5, "name": "node-5", "url": "https://10.0.0.5:9100", **fields})


def rules(shared: LevelAccess | None = None, **folders: LevelAccess) -> SshAccessRules:
    return SshAccessRules(shared=shared, folders=folders)


class LoginTests(unittest.TestCase):
    RULES = rules(LevelAccess(password="shared"), RU=LevelAccess(password="folder"))

    def login_of(self, node: Server) -> tuple:
        target = resolve_ssh_target(node, self.RULES)
        return target.password, target.login_level

    def test_own_login_beats_folder_and_shared(self):
        self.assertEqual(self.login_of(server(folder="RU", ssh_password="own")), ("own", AccessLevel.SERVER))

    def test_folder_login_for_server_without_own(self):
        self.assertEqual(self.login_of(server(folder="RU")), ("folder", AccessLevel.FOLDER))

    def test_shared_login_for_server_outside_folders_with_access(self):
        self.assertEqual(self.login_of(server(folder="DE")), ("shared", AccessLevel.GLOBAL))
        self.assertEqual(self.login_of(server()), ("shared", AccessLevel.GLOBAL))

    def test_login_is_taken_whole_from_one_level(self):
        by_levels = rules(
            LevelAccess(password="shared"), RU=LevelAccess(user="root", private_key="KEY\n", passphrase="pp"),
        )

        target = resolve_ssh_target(server(folder="RU"), by_levels)

        self.assertEqual((target.private_key, target.passphrase, target.password), ("KEY\n", "pp", None))

    def test_typed_password_replaces_inherited_key(self):
        by_key = rules(LevelAccess(private_key="KEY\n", passphrase="pp"))

        target = resolve_ssh_target(server(), by_key, ssh_password="typed")

        self.assertEqual((target.password, target.private_key, target.passphrase), ("typed", None, None))
        self.assertIsNone(target.login_level)

    def test_typed_password_is_used_exactly_as_typed(self):
        self.assertEqual(resolve_ssh_target(server(), rules(), ssh_password="  p w  ").password, "  p w  ")

    def test_typed_key_replaces_own_password(self):
        target = resolve_ssh_target(server(ssh_password="own"), rules(), ssh_private_key="TYPED")

        self.assertEqual((target.private_key, target.password), ("TYPED", None))

    def test_request_without_secret_keeps_saved_login(self):
        target = resolve_ssh_target(server(ssh_password="own"), rules(), ssh_port=2200, ssh_user="root")

        self.assertEqual((target.password, target.port, target.login_level), ("own", 2200, AccessLevel.SERVER))

    def test_no_login_on_any_level_is_no_creds(self):
        with self.assertRaises(SSHTargetError) as caught:
            resolve_ssh_target(server(folder="RU", ssh_port=22), rules(RU=LevelAccess(port=2222)))

        self.assertEqual(caught.exception.reason, "no_creds")

    def test_shared_non_root_user_is_refused(self):
        with self.assertRaises(SSHTargetError) as caught:
            resolve_ssh_target(server(), rules(LevelAccess(user="ubuntu", password="x")))

        self.assertEqual(caught.exception.reason, "not_root")

    def test_server_without_address_is_no_host(self):
        with self.assertRaises(SSHTargetError) as caught:
            resolve_ssh_target(server(url=""), self.RULES)

        self.assertEqual(caught.exception.reason, "no_host")

    def test_target_keeps_server_address_and_pinned_key(self):
        target = resolve_ssh_target(server(ssh_host="10.9.9.9", ssh_host_key="KEY"), self.RULES)

        self.assertEqual((target.host, target.server_id, target.host_key), ("10.9.9.9", 5, "KEY"))


class PortTests(unittest.TestCase):
    RULES = rules(LevelAccess(password="shared", port=2022), RU=LevelAccess(port=2222))

    def port_of(self, node: Server, **request) -> int:
        return resolve_ssh_target(node, self.RULES, **request).port

    def test_recorded_server_port_beats_folder_port(self):
        self.assertEqual(self.port_of(server(folder="RU", ssh_port=22)), 22)

    def test_port_falls_to_folder_then_shared_then_22(self):
        self.assertEqual(self.port_of(server(folder="RU")), 2222)
        self.assertEqual(self.port_of(server(folder="DE")), 2022)
        self.assertEqual(resolve_ssh_target(server(), rules(LevelAccess(password="shared"))).port, 22)

    def test_request_port_beats_everything(self):
        self.assertEqual(self.port_of(server(folder="RU", ssh_port=22), ssh_port=2200), 2200)


class ProxyRouteTests(unittest.TestCase):
    def routes_of(self, node: Server, by_levels: SshAccessRules) -> tuple:
        return resolve_ssh_target(node, by_levels).routes

    def test_parts_are_inherited_independently(self):
        by_levels = rules(LevelAccess(password="shared"), RU=LevelAccess(proxy=PROXY))

        target = resolve_ssh_target(server(folder="RU", ssh_port=2222), by_levels)

        self.assertEqual((target.port, target.password, target.routes), (2222, "shared", (None, PROXY)))

    def test_fallback_proxy_goes_after_the_server_own_proxy(self):
        by_levels = rules(LevelAccess(password="shared", proxy=PROXY, proxy_mode="fallback"))

        self.assertEqual(self.routes_of(server(proxy_url="9.9.9.9:1080"), by_levels), ("9.9.9.9:1080", PROXY))

    def test_always_mode_leaves_only_the_proxy(self):
        by_levels = rules(LevelAccess(password="shared", proxy=PROXY, proxy_mode="always"))

        self.assertEqual(self.routes_of(server(proxy_url="9.9.9.9:1080"), by_levels), (PROXY,))

    def test_off_on_folder_cancels_shared_proxy(self):
        by_levels = rules(
            LevelAccess(password="shared", proxy=PROXY, proxy_mode="always"), RU=LevelAccess(proxy_mode="off"),
        )

        self.assertEqual(self.routes_of(server(folder="RU"), by_levels), (None,))
        self.assertEqual(self.routes_of(server(folder="DE"), by_levels), (PROXY,))

    def test_own_proxy_of_server_beats_folder_proxy(self):
        by_levels = rules(LevelAccess(password="shared"), RU=LevelAccess(proxy=PROXY, proxy_mode="always"))
        node = server(folder="RU", ssh_proxy="5.6.7.8:1080", ssh_proxy_mode="always")

        self.assertEqual(self.routes_of(node, by_levels), ("5.6.7.8:1080",))

    def test_server_without_any_ssh_proxy_goes_its_usual_way(self):
        by_levels = rules(LevelAccess(password="shared"))

        self.assertEqual(self.routes_of(server(), by_levels), (None,))
        self.assertEqual(self.routes_of(server(proxy_url="9.9.9.9:1080"), by_levels), ("9.9.9.9:1080",))

    def test_routes_by_mode(self):
        self.assertEqual(ssh_routes(None, PROXY, None), (None, PROXY))
        self.assertEqual(ssh_routes(None, PROXY, "fallback"), (None, PROXY))
        self.assertEqual(ssh_routes(None, PROXY, "always"), (PROXY,))
        self.assertEqual(ssh_routes("9.9.9.9:1080", None, "off"), ("9.9.9.9:1080",))
        self.assertEqual(ssh_routes(PROXY, PROXY, "fallback"), (PROXY,))


class LoginLevelTests(unittest.TestCase):
    RULES = rules(LevelAccess(password="g"), RU=LevelAccess(private_key="K\n"), DE=LevelAccess(port=2222))

    def test_level_is_known_without_reading_secrets_of_the_server(self):
        self.assertEqual(login_level(self.RULES, "RU", False), AccessLevel.FOLDER)
        self.assertEqual(login_level(self.RULES, "RU", True), AccessLevel.SERVER)
        self.assertEqual(login_level(self.RULES, None, False), AccessLevel.GLOBAL)

    def test_folder_with_only_a_port_passes_login_to_shared(self):
        self.assertEqual(login_level(self.RULES, "DE", False), AccessLevel.GLOBAL)

    def test_no_login_anywhere(self):
        self.assertIsNone(login_level(rules(DE=LevelAccess(port=2222)), "DE", False))


def shared_password(password: str = "shared", **fields) -> SshAccessProfile:
    return SshAccessProfile(folder="", ssh_password=password, **fields)


class WatcherFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Нода не обновилась через агента — панель идёт по SSH с доступом по уровням."""

    async def fall_back(self, node: Server, profiles: list) -> tuple[mock.MagicMock, mock.AsyncMock]:
        manager = mock.MagicMock()
        manager.start.return_value = "job-1"
        notify = mock.AsyncMock()
        session = mock.MagicMock()
        session.__aenter__ = mock.AsyncMock(return_value=FakeDb(profiles=profiles))
        session.__aexit__ = mock.AsyncMock(return_value=False)
        attempt = NodeUpdateAttempt(server_id=node.id, attempt_id="a1", target_ref="main", stage="agent")
        decision = Decision(Verdict.FAILED, FailureReason.IMAGE_UNAVAILABLE)

        with mock.patch.object(node_update_watcher, "async_session", return_value=session), \
                mock.patch.object(node_update_watcher, "get_image_delivery_manager", return_value=manager), \
                mock.patch.object(node_update_watcher, "_mark_ssh", mock.AsyncMock()), \
                mock.patch.object(node_update_watcher, "_close", mock.AsyncMock(return_value=True)), \
                mock.patch.object(node_update_watcher, "_notify", notify), \
                mock.patch.object(node_update_watcher.update_channel, "current_image_tag", return_value="latest"):
            await node_update_watcher.NodeUpdateWatcher()._fall_back(attempt, node, decision)
        return manager, notify

    async def test_fallback_uses_shared_access_when_server_has_none(self):
        manager, notify = await self.fall_back(server(), [shared_password()])

        target = manager.start.call_args.args[2]
        self.assertEqual((target.host, target.password, target.login_level), ("10.0.0.5", "shared", AccessLevel.GLOBAL))
        notify.assert_not_awaited()

    async def test_fallback_without_access_on_any_level_reports_no_creds(self):
        manager, notify = await self.fall_back(server(), [])

        manager.start.assert_not_called()
        self.assertEqual(notify.await_args.args[3], FallbackProblem.NO_CREDS)


class CallersTests(unittest.IsolatedAsyncioTestCase):
    def test_haproxy_retry_over_ssh_gets_inherited_access(self):
        target, problem = haproxy_upgrade._saved_ssh_target(server(), rules(LevelAccess(password="shared")))

        self.assertEqual((target.password, problem), ("shared", None))

    def test_haproxy_upgrade_hands_inherited_access_to_the_ssh_retry(self):
        by_levels = rules(LevelAccess(password="shared"))

        with mock.patch.object(haproxy_upgrade, "_agent_then_ssh", mock.MagicMock()) as retry, \
                mock.patch.object(haproxy_upgrade, "_queued", lambda events: events), \
                mock.patch.object(haproxy_upgrade._manager, "start", return_value="job-1"):
            haproxy_upgrade.start_upgrade(server(), by_levels)

        self.assertEqual(retry.call_args.args[1].password, "shared")

    def test_haproxy_retry_without_access_names_the_problem(self):
        target, problem = haproxy_upgrade._saved_ssh_target(server(), rules())

        self.assertIsNone(target)
        self.assertTrue(problem)

    def test_remnawave_install_takes_inherited_access_for_empty_form(self):
        by_levels = rules(LevelAccess(password="shared"))

        target = remnawave_install._ssh_target_or_400(server(), by_levels, node_image.SSHAccessRequest())

        self.assertEqual(target.password, "shared")

    def test_remnawave_install_without_any_access_is_a_client_error(self):
        with self.assertRaises(HTTPException) as caught:
            remnawave_install._ssh_target_or_400(server(), rules(), node_image.SSHAccessRequest())

        self.assertEqual(caught.exception.status_code, 400)

    async def test_image_delivery_names_the_level(self):
        db = FakeDb(servers=[server(folder="RU")], profiles=[SshAccessProfile(folder="RU", ssh_password="f", ssh_port=2222)])

        settings = await node_image.get_image_delivery(5, db)

        self.assertEqual((settings["ssh_access_level"], settings["ssh_access_folder"]), ("folder", "RU"))
        self.assertEqual((settings["ssh_port"], settings["ssh_user"]), (2222, "root"))
        self.assertEqual((settings["has_ssh_password"], settings["has_ssh_private_key"]), (False, False))

    async def test_image_delivery_of_server_with_own_login(self):
        db = FakeDb(servers=[server(ssh_password="own", ssh_port=2200)], profiles=[shared_password(ssh_port=2022)])

        settings = await node_image.get_image_delivery(5, db)

        self.assertEqual((settings["ssh_access_level"], settings["ssh_access_folder"]), ("server", None))
        self.assertEqual((settings["ssh_port"], settings["has_ssh_password"]), (2200, True))

    async def test_image_delivery_of_server_without_access(self):
        settings = await node_image.get_image_delivery(5, FakeDb(servers=[server()]))

        self.assertEqual((settings["ssh_access_level"], settings["ssh_access_folder"]), (None, None))
        self.assertEqual((settings["ssh_host"], settings["ssh_port"], settings["ssh_user"]), ("10.0.0.5", 22, "root"))

    async def test_version_base_marks_inherited_access(self):
        nodes = [
            server(id=1, name="own", ssh_private_key="K\n"),
            server(id=2, name="in-folder", folder="RU"),
            server(id=3, name="bare"),
        ]
        db = FakeDb(servers=nodes, profiles=[SshAccessProfile(folder="RU", ssh_password="f")])
        latest = SimpleNamespace(panel="1.0.0", node="1.0.0", optimizations="1.0.0")

        with mock.patch.object(system, "get_current_version", return_value="1.0.0"), \
                mock.patch.object(system.release_versions, "latest_versions", mock.AsyncMock(return_value=latest)), \
                mock.patch.object(system.node_rollout, "pending_count", mock.AsyncMock(return_value=0)):
            base = await system.get_version_base(db)

        # Страница перечитывает список раз в 12 секунд: записи серверов с секретами грузиться не должны
        servers_query = str(db.queries[0].compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("servers.ssh_password IS NOT NULL", servers_query)
        self.assertNotIn("servers.api_key", servers_query)

        self.assertEqual(
            [(node["id"], node["folder"], node["has_ssh_creds"]) for node in base["nodes"]],
            [(1, None, True), (2, "RU", True), (3, None, False)],
        )


if __name__ == "__main__":
    unittest.main()
