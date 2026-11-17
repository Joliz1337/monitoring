"""API «SSH-доступ»: задать, убрать, сводка по парку, папки, ключ хоста.

Доступ задаётся заранее и отдельно от обновления: сохранение правит только
выбранный уровень и ничего не запускает. Общий доступ и доступ папки не трогают
записи серверов — свой доступ сервера остаётся главнее. Наружу уходят только
признаки «задан»: ни пароль, ни ключ, ни пароль прокси.
"""

import json
import os
import sys
import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ssh_fakes import FakeDb  # noqa: E402

from app.auth import verify_auth  # noqa: E402
from app.database import get_db  # noqa: E402
from app.models import Server, SshAccessProfile  # noqa: E402
from app.routers import servers as servers_router  # noqa: E402
from app.routers import ssh_access as api  # noqa: E402
from app.services import ssh_access  # noqa: E402
from app.services.node_update_watcher import FailureReason, FallbackProblem, failure_message  # noqa: E402
from app.services.ssh_target import HostKeyChangedError  # noqa: E402

PROXY = "1.2.3.4:1080"


def node(sid: int, **fields) -> Server:
    return Server(**{
        "id": sid, "name": f"node-{sid}", "url": f"https://10.0.0.{sid}:9100", "is_active": True, **fields,
    })


def shared(**fields) -> SshAccessProfile:
    return SshAccessProfile(folder="", **fields)


def folder(name: str, **fields) -> SshAccessProfile:
    return SshAccessProfile(folder=name, **fields)


async def save(db: FakeDb, **request) -> dict:
    return await api.save(api.SaveRequest(**request), db)


async def clear(db: FakeDb, **request) -> dict:
    return await api.clear(api.ClearRequest(**request), db)


class SaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_access_is_created_and_servers_stay_untouched(self):
        server = node(1, ssh_host_key="KEY")
        db = FakeDb(servers=[server])

        result = await save(db, scope="global", ssh_password="pw")

        self.assertEqual(result, {"changed": 1})
        self.assertEqual([(row.folder, row.ssh_password) for row in db.profiles], [("", "pw")])
        self.assertEqual((server.ssh_password, server.ssh_host_key), (None, "KEY"))
        db.commit.assert_awaited_once()

    async def test_field_not_sent_is_left_alone(self):
        row = shared(ssh_password="old", ssh_port=2022)
        db = FakeDb(profiles=[row])

        await save(db, scope="global", ssh_proxy=PROXY)

        self.assertEqual((row.ssh_password, row.ssh_port), ("old", 2022))
        self.assertEqual((row.ssh_proxy, row.ssh_proxy_mode), (PROXY, "fallback"))
        self.assertEqual(len(db.profiles), 1)

    async def test_empty_change_is_refused(self):
        db = FakeDb()

        with self.assertRaises(HTTPException) as caught:
            await save(db, scope="global")

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(db.profiles, [])
        db.commit.assert_not_awaited()

    async def test_folder_without_servers_is_refused(self):
        db = FakeDb(servers=[node(1, folder="RU")])

        with self.assertRaises(HTTPException) as caught:
            await save(db, scope="folders", folders=["RU", "DE"], ssh_password="pw")

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("DE", caught.exception.detail)
        self.assertEqual(db.profiles, [])

    async def test_folder_name_is_matched_as_stored(self):
        db = FakeDb(servers=[node(1, folder="RU")])

        await save(db, scope="folders", folders=[" RU "], ssh_password="pw")
        with self.assertRaises(HTTPException):
            await save(db, scope="folders", folders=["ru"], ssh_password="pw")

        self.assertEqual([row.folder for row in db.profiles], ["RU"])

    async def test_several_folders_get_the_same_access(self):
        db = FakeDb(servers=[node(1, folder="RU"), node(2, folder="DE")])

        result = await save(db, scope="folders", folders=["RU", "DE", "RU"], ssh_port=2222)

        self.assertEqual(result, {"changed": 2})
        self.assertEqual(sorted((row.folder, row.ssh_port) for row in db.profiles), [("DE", 2222), ("RU", 2222)])

    async def test_off_mode_alone_refuses_inherited_proxy_for_the_folder(self):
        db = FakeDb(servers=[node(1, folder="RU")])

        await save(db, scope="folders", folders=["RU"], ssh_proxy_mode="off")

        self.assertEqual([(row.ssh_proxy, row.ssh_proxy_mode) for row in db.profiles], [(None, "off")])

    async def test_server_scope_writes_own_access_and_resets_host_key(self):
        server = node(1, ssh_host_key="KEY")
        db = FakeDb(servers=[server])

        result = await save(db, scope="servers", server_ids=[1], ssh_password="pw", ssh_port=2222)

        self.assertEqual(result, {"changed": 1})
        self.assertEqual((server.ssh_password, server.ssh_port, server.ssh_host_key), ("pw", 2222, None))
        self.assertEqual(db.profiles, [])

    async def test_unknown_server_ids_are_skipped(self):
        db = FakeDb(servers=[node(1)])

        result = await save(db, scope="servers", server_ids=[1, 99], ssh_password="pw")

        self.assertEqual(result, {"changed": 1})

    async def test_server_scope_touches_only_the_listed_servers(self):
        # FakeDb фильтров не применяет: что правка адресована только перечисленным серверам, видно по запросу
        db = FakeDb(servers=[node(1)])

        await save(db, scope="servers", server_ids=[1, 99], ssh_password="pw")
        await clear(db, scope="servers", server_ids=[7], parts=["port"])
        await api.forget_host_keys(api.HostKeysRequest(server_ids=[5]), db)

        queries = [str(query.compile(compile_kwargs={"literal_binds": True})) for query in db.queries]
        self.assertIn("servers.id IN (1, 99)", queries[0])
        self.assertIn("servers.id IN (7)", queries[1])
        self.assertIn("servers.id IN (5)", queries[2])

    async def test_password_reaches_the_level_exactly_as_typed(self):
        db = FakeDb()

        await save(db, scope="global", ssh_password="  p:a@ss  ")

        self.assertEqual(db.profiles[0].ssh_password, "  p:a@ss  ")

    def test_malformed_proxy_is_rejected(self):
        with self.assertRaises(ValidationError):
            api.SaveRequest(scope="global", ssh_proxy="not a proxy")

    def test_port_out_of_range_is_rejected(self):
        with self.assertRaises(ValidationError):
            api.SaveRequest(scope="global", ssh_port=70000)


class ClearTests(unittest.IsolatedAsyncioTestCase):
    async def test_clearing_the_last_part_removes_the_folder_row(self):
        db = FakeDb(profiles=[folder("RU", ssh_port=2222)])

        result = await clear(db, scope="folders", folders=["RU"], parts=["port"])

        self.assertEqual(result, {"changed": 1})
        self.assertEqual(db.profiles, [])

    async def test_clear_keeps_the_other_parts(self):
        row = shared(ssh_password="pw", ssh_proxy=PROXY, ssh_proxy_mode="always")
        db = FakeDb(profiles=[row])

        await clear(db, scope="global", parts=["proxy"])

        self.assertEqual((row.ssh_password, row.ssh_proxy, row.ssh_proxy_mode), ("pw", None, None))
        self.assertEqual(db.profiles, [row])

    async def test_clear_server_login_keeps_port_and_host_key(self):
        server = node(1, ssh_password="pw", ssh_port=2222, ssh_host_key="KEY")
        db = FakeDb(servers=[server])

        await clear(db, scope="servers", server_ids=[1], parts=["login"])

        self.assertEqual((server.ssh_password, server.ssh_port, server.ssh_host_key), (None, 2222, "KEY"))

    async def test_access_of_emptied_folder_can_be_removed(self):
        db = FakeDb(profiles=[folder("GONE", ssh_password="pw")])

        result = await clear(db, scope="folders", folders=["GONE"], parts=["login", "port", "proxy"])

        self.assertEqual((result, db.profiles), ({"changed": 1}, []))

    async def test_level_that_was_never_set_changes_nothing(self):
        db = FakeDb()

        result = await clear(db, scope="global", parts=["login"])

        self.assertEqual(result, {"changed": 0})


class OverviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_overview_never_returns_secrets(self):
        db = FakeDb(
            servers=[node(1, ssh_private_key="PRIVATE-OWN", ssh_passphrase="phrase-own", ssh_proxy=f"{PROXY}@u:proxy-own")],
            profiles=[
                shared(ssh_user="root", ssh_port=22, ssh_password="pw-shared", ssh_proxy=f"{PROXY}@u:proxy-pass"),
                folder("RU", ssh_private_key="PRIVATE-RU", ssh_passphrase="phrase-ru"),
            ],
        )

        overview = await api.overview(db)

        text = json.dumps(overview, ensure_ascii=False)
        for secret in ("pw-shared", "PRIVATE", "phrase", "proxy-pass", "proxy-own"):
            self.assertNotIn(secret, text)
        self.assertEqual(overview["global"], {
            "ssh_user": "root", "ssh_port": 22, "auth": "password", "proxy": PROXY, "proxy_mode": None,
        })
        self.assertEqual(overview["folders"][0]["auth"], "key")
        self.assertEqual((overview["servers"][0]["auth"], overview["servers"][0]["proxy"]), ("key", PROXY))

    async def test_overview_counts_where_each_server_takes_login(self):
        servers = [node(1, ssh_password="own"), node(2, folder="RU"), node(3)]
        db = FakeDb(servers=servers, profiles=[shared(ssh_password="g"), folder("RU", ssh_password="f")])

        overview = await api.overview(db)

        self.assertEqual(overview["summary"], {"total": 3, "server": 1, "folder": 1, "global": 1, "none": 0})
        self.assertEqual((overview["without_login"], overview["without_login_total"]), ([], 0))

    async def test_servers_without_login_are_named(self):
        db = FakeDb(servers=[node(1, ssh_password="own"), node(2, folder="RU")])

        overview = await api.overview(db)

        self.assertEqual(overview["summary"], {"total": 2, "server": 1, "folder": 0, "global": 0, "none": 1})
        self.assertEqual(overview["without_login"], [{"server_id": 2, "name": "node-2", "folder": "RU"}])

    async def test_inactive_servers_are_left_out_of_the_summary(self):
        db = FakeDb(servers=[node(1), node(2, is_active=False)])

        overview = await api.overview(db)

        self.assertEqual(overview["summary"]["total"], 1)
        self.assertEqual([row["server_id"] for row in overview["without_login"]], [1])

    async def test_server_with_only_a_port_is_not_listed(self):
        servers = [node(1, ssh_port=2222), node(2, ssh_proxy_mode="off"), node(3, ssh_password="pw", ssh_port=2200)]
        db = FakeDb(servers=servers)

        overview = await api.overview(db)

        self.assertEqual(
            [(row["server_id"], row["auth"], row["ssh_port"], row["proxy_mode"]) for row in overview["servers"]],
            [(2, None, None, "off"), (3, "password", 2200, None)],
        )

    async def test_emptied_folder_stays_with_zero_servers(self):
        db = FakeDb(servers=[node(1, folder="RU")], profiles=[folder("GONE", ssh_password="pw"), folder("RU", ssh_port=2222)])

        overview = await api.overview(db)

        self.assertEqual([(row["folder"], row["servers"]) for row in overview["folders"]], [("GONE", 0), ("RU", 1)])

    async def test_list_of_servers_without_login_is_capped(self):
        db = FakeDb(servers=[node(1), node(2), node(3)])

        with mock.patch.object(ssh_access, "WITHOUT_LOGIN_LIMIT", 2):
            overview = await api.overview(db)

        self.assertEqual((len(overview["without_login"]), overview["without_login_total"]), (2, 3))


class FolderTests(unittest.IsolatedAsyncioTestCase):
    async def test_rename_moves_folder_access(self):
        row = folder("RU", ssh_password="pw")
        db = FakeDb(profiles=[row])

        await ssh_access.rename_folder(db, "RU", "RU-1")

        self.assertEqual((row.folder, db.profiles), ("RU-1", [row]))

    async def test_rename_into_folder_with_access_keeps_the_target(self):
        target = folder("DE", ssh_password="de")
        db = FakeDb(profiles=[folder("RU", ssh_password="ru"), target])

        await ssh_access.rename_folder(db, "RU", "DE")

        self.assertEqual(db.profiles, [target])
        self.assertEqual(target.ssh_password, "de")

    async def test_deleting_a_folder_drops_its_access(self):
        kept = shared(ssh_password="g")
        db = FakeDb(profiles=[kept, folder("RU", ssh_password="pw")])

        await ssh_access.forget_folder(db, "RU")

        self.assertEqual(db.profiles, [kept])

    async def test_shared_access_is_never_handled_as_a_folder(self):
        row = shared(ssh_password="g")
        db = FakeDb(profiles=[row])

        await ssh_access.rename_folder(db, "", "RU")
        await ssh_access.forget_folder(db, "")

        self.assertEqual((row.folder, db.profiles), ("", [row]))

    async def call_folder_endpoint(self, call):
        with mock.patch.object(servers_router, "get_blocklist_manager", return_value=mock.MagicMock()), \
                mock.patch.object(servers_router.ping_block, "rename_folder", mock.AsyncMock()), \
                mock.patch.object(servers_router.ping_block, "forget_folder", mock.AsyncMock()):
            await call

    async def test_renaming_a_folder_on_the_dashboard_carries_its_access(self):
        row = folder("RU", ssh_password="pw")
        db = FakeDb(servers=[node(1, folder="RU")], profiles=[row])

        await self.call_folder_endpoint(
            servers_router.rename_server_folder(servers_router.RenameServerFolder(old_name="RU", new_name=" RU-1 "), db)
        )

        self.assertEqual(row.folder, "RU-1")

    async def test_deleting_a_folder_on_the_dashboard_drops_its_access(self):
        db = FakeDb(servers=[node(1, folder="RU")], profiles=[folder("RU", ssh_password="pw")])

        await self.call_folder_endpoint(servers_router.delete_server_folder("RU", db))

        self.assertEqual(db.profiles, [])


class HostKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_forget_host_keys_counts_servers_that_had_a_key(self):
        pinned, fresh = node(1, ssh_host_key="KEY"), node(2)
        db = FakeDb(servers=[pinned, fresh])

        result = await api.forget_host_keys(api.HostKeysRequest(server_ids=[1, 2]), db)

        self.assertEqual(result, {"forgotten": 1})
        self.assertIsNone(pinned.ssh_host_key)
        db.commit.assert_awaited_once()

    def test_empty_list_of_servers_is_rejected(self):
        with self.assertRaises(ValidationError):
            api.HostKeysRequest(server_ids=[])


class HttpTests(unittest.TestCase):
    """Те же обработчики через настоящий разбор HTTP-запроса."""

    def setUp(self):
        self.db = FakeDb(servers=[node(1, folder="RU")])

        async def session():
            yield self.db

        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[get_db] = session
        app.dependency_overrides[verify_auth] = lambda: {}
        self.client = TestClient(app)

    def test_saved_access_is_visible_in_the_overview(self):
        saved = self.client.put("/ssh-access", json={
            "scope": "folders", "folders": ["RU"], "ssh_password": "pw",
            "ssh_proxy": f"{PROXY}@login:proxy-pass", "ssh_proxy_mode": "always",
        })
        overview = self.client.get("/ssh-access")

        self.assertEqual(saved.json(), {"changed": 1})
        self.assertEqual(overview.json()["folders"], [{
            "folder": "RU", "servers": 1, "ssh_user": None, "ssh_port": None,
            "auth": "password", "proxy": PROXY, "proxy_mode": "always",
        }])
        self.assertNotIn("proxy-pass", overview.text)

    def test_malformed_requests_are_client_errors(self):
        bad_proxy = self.client.put("/ssh-access", json={"scope": "global", "ssh_proxy": "oops"})
        no_parts = self.client.post("/ssh-access/clear", json={"scope": "global", "parts": []})
        unknown_scope = self.client.put("/ssh-access", json={"scope": "everyone", "ssh_port": 22})

        self.assertEqual((bad_proxy.status_code, no_parts.status_code, unknown_scope.status_code), (422, 422, 422))
        self.assertEqual(self.db.profiles, [])


class GuidanceTests(unittest.TestCase):
    def test_changed_host_key_points_to_the_ssh_access_window(self):
        text = str(HostKeyChangedError("10.0.0.5"))

        self.assertIn("«SSH-доступ»", text)
        self.assertIn("примите новый ключ хоста", text)

    def test_no_creds_notice_points_to_the_ssh_access_window(self):
        ru = failure_message("ru", "nl-1", FailureReason.IMAGE_UNAVAILABLE, None, FallbackProblem.NO_CREDS)
        en = failure_message("en", "nl-1", FailureReason.IMAGE_UNAVAILABLE, None, FallbackProblem.NO_CREDS)

        self.assertIn("«SSH-доступ»", ru)
        self.assertIn("SSH access", en)


if __name__ == "__main__":
    unittest.main()
