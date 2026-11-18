"""Включение и выключение nginx Remnawave на ноде (services/remnawave_nginx_power).

Голый unittest, без PostgreSQL и без сети: сессия БД и клиент ноды подменены.

Выключенный в панели nginx должен оставаться выключенным: раскатки его не
поднимают, поднятый в обход панели останавливается снова, а отвязка сервера
от профиля гасит контейнер на ноде.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import json
import os
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402
from fastapi import BackgroundTasks, HTTPException  # noqa: E402

from app.routers import proxy as proxy_router  # noqa: E402
from app.routers import remnawave_nginx_profiles as profiles_router  # noqa: E402
from app.services import remnawave_nginx_power as power  # noqa: E402
from app.services import remnawave_nginx_sync  # noqa: E402
from app.services.node_capabilities import ACCESS_READ, Capability  # noqa: E402
from app.services.remnawave_nginx_power import PowerResult  # noqa: E402

PROFILE_ID = 7
AGENT_VERSION = power.MIN_NODE_VERSION_NGINX_POWER
INSTALL_PATH = "/opt/remnawave"


def linked_server(server_id: int = 1, *, disabled: bool = False, online: bool = True,
                  profile_id: int | None = PROFILE_ID) -> SimpleNamespace:
    last_seen = datetime.now(timezone.utc) - (timedelta(0) if online else timedelta(hours=1))
    return SimpleNamespace(
        id=server_id, name=f"node-{server_id}", url=f"https://node-{server_id}:9100",
        is_active=True, last_seen=last_seen, node_capabilities=None, node_version=AGENT_VERSION,
        active_remnawave_nginx_profile_id=profile_id,
        remnawave_nginx_disabled=disabled,
        remnawave_nginx_domain="node.example.com", remnawave_nginx_detected=True,
        remnawave_nginx_sync_status="synced", remnawave_nginx_config_hash="abc",
        remnawave_nginx_node_hash="def", remnawave_nginx_last_sync_at=None,
    )


def node_response(status_code: int = 200, body: object = None) -> SimpleNamespace:
    def read_json():
        if body is None:
            raise ValueError("no body")
        return body

    return SimpleNamespace(status_code=status_code, json=read_json)


class FakeSession:
    """Сессия БД: отдаёт заранее заданные серверы и запоминает запросы."""

    def __init__(self, servers: list[SimpleNamespace], statements: list | None = None) -> None:
        self._servers = servers
        self.statements = statements if statements is not None else []
        self.commit = mock.AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, _model, _server_id):
        return self._servers[0] if self._servers else None

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self._servers)))


class PatchingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)

    def replace(self, module, name: str, replacement):
        self.patches.enter_context(mock.patch.object(module, name, replacement))
        return replacement


class NodeRequestTest(PatchingTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.client = SimpleNamespace(post=mock.AsyncMock(), get=mock.AsyncMock())
        self.replace(power, "get_node_client", lambda _server: self.client)
        self.replace(power, "node_auth_headers", lambda _server: {})
        self.server = linked_server()

    async def test_stop_goes_to_the_stop_path_without_parameters(self):
        self.client.post.return_value = node_response(body={"success": True, "message": "остановлен"})

        result = await power.stop_nginx_on_node(self.server)

        self.assertEqual(result, PowerResult(True, "остановлен"))
        call = self.client.post.await_args
        self.assertEqual(call.args[0], "https://node-1:9100/api/remnawave/nginx/stop")
        self.assertIsNone(call.kwargs["params"])

    async def test_start_carries_the_install_path(self):
        self.client.post.return_value = node_response(body={"success": True, "message": "запущен"})

        result = await power.start_nginx_on_node(self.server, INSTALL_PATH)

        self.assertTrue(result.success)
        call = self.client.post.await_args
        self.assertEqual(call.args[0], "https://node-1:9100/api/remnawave/nginx/start")
        self.assertEqual(call.kwargs["params"], {"path": INSTALL_PATH})

    async def test_node_refusal_keeps_its_message(self):
        self.client.post.return_value = node_response(
            body={"success": False, "message": "Запуск не удался: bind() to 0.0.0.0:443 failed"},
        )
        result = await power.start_nginx_on_node(self.server, INSTALL_PATH)
        self.assertFalse(result.success)
        self.assertIn("bind() to 0.0.0.0:443 failed", result.message)

    async def test_old_agent_without_the_route_asks_for_an_update(self):
        self.client.post.return_value = node_response(404, {"detail": "Not Found"})
        result = await power.stop_nginx_on_node(self.server)
        self.assertEqual(result, PowerResult(False, power.NODE_TOO_OLD_MESSAGE))
        self.assertIn(power.MIN_NODE_VERSION_NGINX_POWER, result.message)

    async def test_node_error_detail_reaches_the_operator(self):
        self.client.post.return_value = node_response(403, {"detail": "раздел remnawave закрыт"})
        result = await power.stop_nginx_on_node(self.server)
        self.assertEqual(result, PowerResult(False, "раздел remnawave закрыт"))

    async def test_error_without_a_body_falls_back_to_the_status(self):
        self.client.post.return_value = node_response(500)
        result = await power.stop_nginx_on_node(self.server)
        self.assertEqual((result.success, result.message), (False, "HTTP 500"))

    async def test_timeout_and_broken_link_are_results_not_exceptions(self):
        for error in (httpx.ReadTimeout("slow"), httpx.ConnectError("refused")):
            with self.subTest(error=type(error).__name__):
                self.client.post.side_effect = error
                result = await power.stop_nginx_on_node(self.server)
                self.assertFalse(result.success)
                self.assertFalse(result.definite)
                self.assertTrue(result.message)

    async def test_refusal_with_an_answer_is_definite_and_a_gateway_error_is_not(self):
        # 4xx и «success: false» — нода ответила и отказала; 5xx — запрос оборван на полпути
        for status, body, definite in (
            (200, {"success": False, "message": "не удалось остановить"}, True),
            (404, {"detail": "Not Found"}, True),
            (403, {"detail": "раздел закрыт"}, True),
            (500, None, False),
            (504, {"detail": "gateway timeout"}, False),
        ):
            with self.subTest(status=status):
                self.client.post.return_value = node_response(status, body)
                result = await power.stop_nginx_on_node(self.server)
                self.assertFalse(result.success)
                self.assertIs(result.definite, definite)

    async def test_running_container_is_read_from_the_status_answer(self):
        self.client.get.return_value = node_response(body={"container": {"running": True}})
        self.assertTrue(await power._nginx_running(self.server, INSTALL_PATH))
        self.assertEqual(self.client.get.await_args.kwargs["params"], {"path": INSTALL_PATH})

    async def test_unreadable_status_counts_as_not_running(self):
        # «Не знаю» не должно приводить к остановке
        answers = (
            node_response(body={"container": {"running": False}}),
            node_response(body={}),
            node_response(body=[]),
            node_response(502, {"detail": "bad gateway"}),
            node_response(),
        )
        for answer in answers:
            with self.subTest(status=answer.status_code):
                self.client.get.return_value = answer
                self.assertFalse(await power._nginx_running(self.server, INSTALL_PATH))

        self.client.get.side_effect = httpx.ConnectError("refused")
        self.assertFalse(await power._nginx_running(self.server, INSTALL_PATH))


class SetEnabledTest(PatchingTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.db = SimpleNamespace(commit=mock.AsyncMock())
        self.replace(power, "get_remnawave_nginx_path", mock.AsyncMock(return_value=INSTALL_PATH))
        self.flag_seen_by_node: list[bool] = []
        self.commits_before_node: list[int] = []
        self.node_answer = PowerResult(True, "ok")

    def node(self, name: str) -> mock.AsyncMock:
        async def call(server, *_args):
            self.flag_seen_by_node.append(server.remnawave_nginx_disabled)
            self.commits_before_node.append(self.db.commit.await_count)
            return self.node_answer

        return self.replace(power, name, mock.AsyncMock(side_effect=call))

    async def test_switching_off_is_saved_before_the_node_is_asked(self):
        # Иначе досинхронизация успела бы поднять nginx между ответом ноды и коммитом
        stop = self.node("stop_nginx_on_node")
        server = linked_server(disabled=False)

        result = await power.set_nginx_enabled(self.db, server, enabled=False)

        self.assertTrue(result.success)
        self.assertTrue(server.remnawave_nginx_disabled)
        self.assertEqual((self.flag_seen_by_node, self.commits_before_node), ([True], [1]))
        stop.assert_awaited_once_with(server)

    async def test_switching_on_starts_nginx_at_the_configured_path(self):
        start = self.node("start_nginx_on_node")
        server = linked_server(disabled=True)

        result = await power.set_nginx_enabled(self.db, server, enabled=True)

        self.assertTrue(result.success)
        self.assertFalse(server.remnawave_nginx_disabled)
        self.assertEqual(self.flag_seen_by_node, [False])
        start.assert_awaited_once_with(server, INSTALL_PATH)

    async def test_refused_switch_off_leaves_nginx_enabled_in_the_panel(self):
        self.node("stop_nginx_on_node")
        self.node_answer = PowerResult(False, "Не удалось остановить контейнер: permission denied")
        server = linked_server(disabled=False)

        result = await power.set_nginx_enabled(self.db, server, enabled=False)

        self.assertFalse(result.success)
        self.assertFalse(server.remnawave_nginx_disabled)
        self.assertEqual(self.db.commit.await_count, 2)

    async def test_refused_start_leaves_nginx_disabled_in_the_panel(self):
        self.node("start_nginx_on_node")
        self.node_answer = PowerResult(False, "Запуск не удался, контейнер остановлен обратно")
        server = linked_server(disabled=True)

        result = await power.set_nginx_enabled(self.db, server, enabled=True)

        self.assertFalse(result.success)
        self.assertTrue(server.remnawave_nginx_disabled)

    async def test_switch_off_with_an_unknown_outcome_stays_switched_off(self):
        # Нода могла довести остановку до конца уже после таймаута: откат дал бы
        # «включён» в панели при остановленном контейнере. Остальное доведёт фоновая проверка
        self.node("stop_nginx_on_node")
        self.node_answer = PowerResult(False, "Нода не ответила вовремя", definite=False)
        server = linked_server(disabled=False)

        result = await power.set_nginx_enabled(self.db, server, enabled=False)

        self.assertFalse(result.success)
        self.assertTrue(server.remnawave_nginx_disabled)
        self.assertEqual(self.db.commit.await_count, 1)
        self.assertIn(power.KEPT_OFF_NOTE, result.message)

    async def test_switch_on_with_an_unknown_outcome_stays_switched_on(self):
        # Откат к «выключен» заставил бы фоновую проверку погасить nginx,
        # который нода всё-таки запустила
        self.node("start_nginx_on_node")
        self.node_answer = PowerResult(False, "Нода не ответила вовремя", definite=False)
        server = linked_server(disabled=True)

        result = await power.set_nginx_enabled(self.db, server, enabled=True)

        self.assertFalse(result.success)
        self.assertFalse(server.remnawave_nginx_disabled)
        self.assertIn(power.KEPT_ON_NOTE, result.message)


class StopAfterUnlinkTest(PatchingTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.stop = self.replace(
            power, "stop_nginx_on_node", mock.AsyncMock(return_value=PowerResult(True, "остановлен")),
        )

    async def test_online_node_is_stopped(self):
        self.assertTrue(await power.stop_nginx_after_unlink(linked_server(profile_id=None)))
        self.stop.assert_awaited_once()

    async def test_offline_node_is_skipped(self):
        server = linked_server(profile_id=None, online=False)
        self.assertIs(power.unlink_stop_skip(server), power.UnlinkStopSkip.OFFLINE)
        self.assertFalse(await power.stop_nginx_after_unlink(server))
        self.stop.assert_not_awaited()

    async def test_node_that_closed_the_section_is_skipped(self):
        server = linked_server(profile_id=None)
        server.node_capabilities = json.dumps({Capability.REMNAWAVE.value: ACCESS_READ})
        self.assertIs(power.unlink_stop_skip(server), power.UnlinkStopSkip.DENIED)
        self.assertFalse(await power.stop_nginx_after_unlink(server))
        self.stop.assert_not_awaited()

    async def test_old_agent_is_skipped_and_an_unknown_version_is_tried(self):
        server = linked_server(profile_id=None)
        old_agent = power.UnlinkStopSkip.OLD_AGENT
        for version, skip in (("10.36.1", old_agent), ("10.9.0", old_agent),
                              (AGENT_VERSION, None), ("10.100.0", None), (None, None)):
            with self.subTest(version=version):
                server.node_version = version
                self.assertIs(power.unlink_stop_skip(server), skip)

        server.node_version = "10.36.1"
        self.assertFalse(await power.stop_nginx_after_unlink(server))
        self.stop.assert_not_awaited()

    async def test_node_failure_is_not_raised(self):
        self.stop.return_value = PowerResult(False, "Нет связи с нодой")
        with self.assertLogs(power.logger, level="WARNING"):
            self.assertFalse(await power.stop_nginx_after_unlink(linked_server(profile_id=None)))


class ReappearedNginxTest(PatchingTest):
    """Фоновая проверка: выключенный в панели nginx, поднятый в обход неё."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.servers: list[SimpleNamespace] = []
        self.statements: list = []
        self.running: set[int] = set()
        self.replace(power, "async_session_maker", lambda: FakeSession(self.servers, self.statements))
        self.install_path = self.replace(
            power, "get_remnawave_nginx_path", mock.AsyncMock(return_value=INSTALL_PATH),
        )
        self.status = self.replace(power, "_nginx_running", mock.AsyncMock(side_effect=self.is_running))
        self.still_disabled = self.replace(power, "_still_disabled", mock.AsyncMock(return_value=True))
        self.stop = self.replace(
            power, "stop_nginx_on_node", mock.AsyncMock(return_value=PowerResult(True, "остановлен")),
        )

    async def is_running(self, server, _install_path) -> bool:
        return server.id in self.running

    def stopped_ids(self) -> list[int]:
        return sorted(call.args[0].id for call in self.stop.await_args_list)

    async def test_only_linked_servers_switched_off_in_the_panel_are_selected(self):
        await power.stop_reappeared_nginx()

        # По именам колонок, а не по тому, как диалект записывает true
        where = str(self.statements[0].whereclause)
        self.assertIn("active_remnawave_nginx_profile_id IS NOT NULL", where)
        for column in ("servers.remnawave_nginx_disabled", "servers.is_active"):
            self.assertIn(column, where)
            self.assertNotIn(f"{column} IS false", where)
            self.assertNotIn(f"NOT {column}", where)

    async def test_nginx_that_came_back_is_stopped_and_a_stopped_one_is_left_alone(self):
        self.servers += [linked_server(1, disabled=True), linked_server(2, disabled=True)]
        self.running = {1}

        self.assertEqual(await power.stop_reappeared_nginx(), 1)
        self.assertEqual(self.stopped_ids(), [1])

    async def test_offline_and_closed_nodes_are_not_even_asked(self):
        closed = linked_server(2, disabled=True)
        closed.node_capabilities = json.dumps({Capability.REMNAWAVE.value: ACCESS_READ})
        self.servers += [linked_server(1, disabled=True, online=False), closed]
        self.running = {1, 2}

        self.assertEqual(await power.stop_reappeared_nginx(), 0)
        self.status.assert_not_awaited()
        self.stop.assert_not_awaited()

    async def test_nginx_switched_back_on_during_the_check_is_not_stopped(self):
        # Список серверов снят до нажатия «Включить», а контейнер уже запущен оператором
        self.servers.append(linked_server(1, disabled=True))
        self.running = {1}
        self.still_disabled.return_value = False

        self.assertEqual(await power.stop_reappeared_nginx(), 0)
        self.stop.assert_not_awaited()

    async def test_failed_stop_is_logged_and_not_counted(self):
        self.servers.append(linked_server(1, disabled=True))
        self.running = {1}
        self.stop.return_value = PowerResult(False, "Нода не ответила вовремя")

        with self.assertLogs(power.logger, level="WARNING"):
            self.assertEqual(await power.stop_reappeared_nginx(), 0)

    async def test_nothing_switched_off_means_no_node_is_asked(self):
        self.assertEqual(await power.stop_reappeared_nginx(), 0)
        self.install_path.assert_not_awaited()
        self.status.assert_not_awaited()


class RolloutRespectsSwitchTest(PatchingTest):
    """Раскатка с автоподъёмом (привязка, ожившая нода) выключенный nginx не запускает."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.started: dict[int, bool] = {}

        async def apply(server, _template, _profile_id, _install_path, ensure_started=False):
            self.started[server.id] = ensure_started
            return remnawave_nginx_sync.SyncResult(server.id, server.name, True, "applied", status="success")

        self.replace(remnawave_nginx_sync, "_sync_single_server", apply)
        self.replace(
            remnawave_nginx_sync, "get_remnawave_nginx_path", mock.AsyncMock(return_value=INSTALL_PATH),
        )
        self.db = FakeSession([linked_server(1, disabled=False), linked_server(2, disabled=True)])
        self.profile = SimpleNamespace(id=PROFILE_ID, config_content="server {}")

    async def test_auto_start_skips_the_switched_off_server(self):
        await remnawave_nginx_sync.sync_profile_to_servers(self.profile, self.db, ensure_started=True)
        self.assertEqual(self.started, {1: True, 2: False})

    async def test_plain_rollout_starts_nobody(self):
        await remnawave_nginx_sync.sync_profile_to_servers(self.profile, self.db)
        self.assertEqual(self.started, {1: False, 2: False})

    async def test_linking_switches_nginx_back_on(self):
        server = linked_server(disabled=True, profile_id=None)
        remnawave_nginx_sync.apply_server_link(self.profile, server)
        self.assertFalse(server.remnawave_nginx_disabled)
        self.assertEqual(server.active_remnawave_nginx_profile_id, PROFILE_ID)


class RouterTest(PatchingTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bg = BackgroundTasks()

    async def unlink(self, server) -> dict:
        return await profiles_router.unlink_server(
            PROFILE_ID, 1, self.bg, db=FakeSession([server]), _=None,
        )

    async def test_unlink_schedules_the_stop_and_forgets_the_switch(self):
        server = linked_server(disabled=True)

        answer = await self.unlink(server)

        self.assertEqual(answer, {"success": True, "nginx_stop_scheduled": True, "nginx_stop_skipped": None})
        self.assertIsNone(server.active_remnawave_nginx_profile_id)
        # Флаг живёт только у привязанного сервера: иначе фоновая проверка
        # гасила бы nginx, которым панель уже не управляет
        self.assertFalse(server.remnawave_nginx_disabled)
        self.assertEqual(server.remnawave_nginx_domain, "node.example.com")
        self.assertEqual([task.func for task in self.bg.tasks], [profiles_router._bg_stop_nginx])

    async def test_unlink_says_why_nginx_was_not_stopped(self):
        offline = linked_server(online=False)
        old_agent = linked_server()
        old_agent.node_version = "10.36.1"
        closed = linked_server()
        closed.node_capabilities = json.dumps({Capability.REMNAWAVE.value: ACCESS_READ})

        for server, reason in ((offline, "offline"), (old_agent, "old_agent"), (closed, "denied")):
            with self.subTest(reason=reason):
                answer = await self.unlink(server)
                self.assertEqual(answer, {
                    "success": True, "nginx_stop_scheduled": False, "nginx_stop_skipped": reason,
                })
                self.assertIsNone(server.active_remnawave_nginx_profile_id)
        self.assertEqual(self.bg.tasks, [])

    async def test_unlink_through_a_stale_page_does_not_touch_another_profile(self):
        server = linked_server(profile_id=PROFILE_ID + 1)

        with self.assertRaises(HTTPException) as raised:
            await self.unlink(server)

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(server.active_remnawave_nginx_profile_id, PROFILE_ID + 1)
        self.assertEqual(self.bg.tasks, [])

    async def background_stop(self, server) -> mock.AsyncMock:
        stop = self.replace(profiles_router, "stop_nginx_after_unlink", mock.AsyncMock(return_value=True))
        self.replace(profiles_router, "async_session_maker", lambda: FakeSession([server]))
        await profiles_router._bg_stop_nginx(1)
        return stop

    async def test_background_stop_reaches_the_unlinked_node(self):
        server = linked_server(profile_id=None)
        stop = await self.background_stop(server)
        stop.assert_awaited_once_with(server)

    async def test_background_stop_spares_a_server_linked_again(self):
        # Привязка сама поднимает nginx: запоздавшая остановка погасила бы рабочий
        stop = await self.background_stop(linked_server(profile_id=PROFILE_ID + 1))
        stop.assert_not_awaited()

    async def set_power(self, server, enabled: bool) -> dict:
        servers = [server] if server else []
        return await profiles_router.set_server_power(
            PROFILE_ID, 1, profiles_router.ServerPowerRequest(enabled=enabled),
            db=FakeSession(servers), _=None,
        )

    async def test_power_answer_carries_the_node_message_and_the_resulting_state(self):
        async def switch(_db, server, enabled):
            server.remnawave_nginx_disabled = not enabled
            return PowerResult(True, "Контейнер remnawave-nginx остановлен")

        self.replace(profiles_router, "set_nginx_enabled", switch)

        answer = await self.set_power(linked_server(), enabled=False)

        self.assertEqual(answer, {
            "success": True, "message": "Контейнер remnawave-nginx остановлен", "nginx_enabled": False,
        })

    async def test_node_refusal_is_an_answer_not_an_http_error(self):
        self.replace(
            profiles_router, "set_nginx_enabled",
            mock.AsyncMock(return_value=PowerResult(False, power.NODE_TOO_OLD_MESSAGE)),
        )

        answer = await self.set_power(linked_server(), enabled=False)

        self.assertEqual(answer, {
            "success": False, "message": power.NODE_TOO_OLD_MESSAGE, "nginx_enabled": True,
        })

    async def test_power_is_refused_for_a_server_of_another_profile(self):
        switch = self.replace(profiles_router, "set_nginx_enabled", mock.AsyncMock())
        for server in (None, linked_server(profile_id=PROFILE_ID + 1), linked_server(profile_id=None)):
            with self.subTest(server=server):
                with self.assertRaises(HTTPException) as raised:
                    await self.set_power(server, enabled=False)
                self.assertEqual(raised.exception.status_code, 404)
        switch.assert_not_awaited()

    async def test_power_is_refused_when_the_node_closed_the_section(self):
        switch = self.replace(profiles_router, "set_nginx_enabled", mock.AsyncMock())
        server = linked_server()
        server.node_capabilities = json.dumps({Capability.REMNAWAVE.value: ACCESS_READ})

        with self.assertRaises(HTTPException) as raised:
            await self.set_power(server, enabled=False)

        self.assertEqual(raised.exception.status_code, 409)
        switch.assert_not_awaited()

    async def test_servers_status_reports_the_switch(self):
        self.replace(
            profiles_router, "_get_profile",
            mock.AsyncMock(return_value=SimpleNamespace(id=PROFILE_ID, config_content="server {}")),
        )
        for disabled in (False, True):
            with self.subTest(disabled=disabled):
                items = await profiles_router.get_servers_status(
                    PROFILE_ID, db=FakeSession([linked_server(disabled=disabled)]), _=None,
                )
                self.assertEqual(items[0]["nginx_enabled"], not disabled)


class RestartGuardTest(PatchingTest):
    def node_request(self, server) -> mock.AsyncMock:
        self.replace(proxy_router, "get_server_by_id", mock.AsyncMock(return_value=server))
        return self.replace(proxy_router, "proxy_request", mock.AsyncMock(return_value={"success": True}))

    async def test_restart_would_start_a_switched_off_nginx_so_it_is_refused(self):
        request = self.node_request(linked_server(disabled=True))

        with self.assertRaises(HTTPException) as raised:
            await proxy_router.restart_remnawave_nginx(1, db=None, _={})

        self.assertEqual(raised.exception.status_code, 409)
        request.assert_not_awaited()

    async def test_restart_of_a_working_nginx_goes_to_the_node(self):
        request = self.node_request(linked_server(disabled=False))

        await proxy_router.restart_remnawave_nginx(1, db=None, _={})

        self.assertEqual(request.await_args.args[1], "/api/remnawave/nginx/restart")


if __name__ == "__main__":
    unittest.main()
