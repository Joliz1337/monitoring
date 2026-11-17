"""Уровни SSH-доступа: что меняет правка уровня и как из строк собираются правила.

Уровень — общий, папка или сам сервер. Правка трогает только переданные поля:
одной формой задаётся и всё сразу, и одна часть. Вход на уровне один: пароль
либо ключ, иначе при подключении ключ молча побеждал бы введённый пароль.
"""

import os
import sys
import unittest

import asyncssh

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ssh_fakes import FakeDb  # noqa: E402

from app.models import Server, SshAccessProfile  # noqa: E402
from app.services.ssh_access import (  # noqa: E402
    AccessChange,
    AccessPartName,
    apply_change,
    clear_parts,
    is_blank,
    load_rules,
    rules_from_profiles,
    store_server_access,
)
from app.services.ssh_target import ProxyMode  # noqa: E402


def shared(**fields) -> SshAccessProfile:
    return SshAccessProfile(folder="", **fields)


def proxy_of(level) -> tuple:
    return level.ssh_proxy, level.ssh_proxy_mode


class ApplyChangeTests(unittest.TestCase):
    def test_password_replaces_stored_key(self):
        level = shared(ssh_private_key="KEY", ssh_passphrase="pp")

        apply_change(level, AccessChange(password="pw"))

        self.assertEqual((level.ssh_password, level.ssh_private_key, level.ssh_passphrase), ("pw", None, None))

    def test_key_replaces_stored_password(self):
        level = shared(ssh_password="old")

        apply_change(level, AccessChange(private_key="KEY", passphrase="pp"))

        self.assertEqual((level.ssh_private_key, level.ssh_passphrase, level.ssh_password), ("KEY\n", "pp", None))

    def test_new_key_without_passphrase_drops_the_old_one(self):
        level = shared(ssh_private_key="OLD", ssh_passphrase="old-pp")

        apply_change(level, AccessChange(private_key="NEW"))

        self.assertEqual((level.ssh_private_key, level.ssh_passphrase), ("NEW\n", None))

    def test_blank_key_is_neither_a_login_nor_a_request_to_erase(self):
        # Случайный пробел или перевод строки в поле ключа не должен подменить пароль пустым ключом
        level = shared(ssh_password="pw")

        apply_change(level, AccessChange(private_key="  \n "))

        self.assertEqual((level.ssh_password, level.ssh_private_key), ("pw", None))

    def test_field_not_sent_is_left_alone(self):
        level = shared(ssh_password="pw", ssh_proxy="1.2.3.4:1080", ssh_proxy_mode="always")

        apply_change(level, AccessChange(port=2222))

        self.assertEqual(level.ssh_port, 2222)
        self.assertEqual(level.ssh_password, "pw")
        self.assertEqual(proxy_of(level), ("1.2.3.4:1080", "always"))

    def test_empty_string_and_zero_erase(self):
        level = shared(ssh_user="root", ssh_port=2222, ssh_password="pw")

        apply_change(level, AccessChange(user="", port=0, password=""))

        self.assertEqual((level.ssh_user, level.ssh_port, level.ssh_password), (None, None, None))

    def test_password_is_kept_verbatim(self):
        level = shared()

        apply_change(level, AccessChange(user="  root ", password="  p:a@ss  "))

        self.assertEqual((level.ssh_user, level.ssh_password), ("root", "  p:a@ss  "))

    def test_key_pasted_from_windows_is_importable(self):
        pem = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()
        level = shared()

        apply_change(level, AccessChange(private_key=pem.replace("\n", "\r\n").rstrip()))

        asyncssh.import_private_key(level.ssh_private_key)
        self.assertTrue(level.ssh_private_key.endswith("\n"))
        self.assertFalse(level.ssh_private_key.endswith("\n\n"))
        self.assertNotIn("\r", level.ssh_private_key)


class ProxyChangeTests(unittest.TestCase):
    def test_new_proxy_gets_fallback_mode(self):
        level = shared()

        apply_change(level, AccessChange(proxy=" 1.2.3.4:1080 "))

        self.assertEqual(proxy_of(level), ("1.2.3.4:1080", "fallback"))

    def test_new_address_keeps_the_mode_of_the_old_one(self):
        level = shared(ssh_proxy="1.2.3.4:1080", ssh_proxy_mode="always")

        apply_change(level, AccessChange(proxy="5.6.7.8:1080"))

        self.assertEqual(proxy_of(level), ("5.6.7.8:1080", "always"))

    def test_off_mode_drops_the_address(self):
        level = shared(ssh_proxy="1.2.3.4:1080", ssh_proxy_mode="always")

        apply_change(level, AccessChange(proxy_mode=ProxyMode.OFF))

        self.assertEqual(proxy_of(level), (None, "off"))

    def test_address_given_after_off_works_as_fallback(self):
        level = shared(ssh_proxy_mode="off")

        apply_change(level, AccessChange(proxy="1.2.3.4:1080"))

        self.assertEqual(proxy_of(level), ("1.2.3.4:1080", "fallback"))

    def test_empty_proxy_returns_to_inheritance(self):
        for level in (shared(ssh_proxy="1.2.3.4:1080", ssh_proxy_mode="always"), shared(ssh_proxy_mode="off")):
            apply_change(level, AccessChange(proxy=""))

            self.assertEqual(proxy_of(level), (None, None))

    def test_mode_alone_changes_only_existing_proxy(self):
        with_proxy = shared(ssh_proxy="1.2.3.4:1080", ssh_proxy_mode="fallback")
        without_proxy = shared()

        apply_change(with_proxy, AccessChange(proxy_mode=ProxyMode.ALWAYS))
        apply_change(without_proxy, AccessChange(proxy_mode=ProxyMode.ALWAYS))

        self.assertEqual(proxy_of(with_proxy), ("1.2.3.4:1080", "always"))
        self.assertEqual(proxy_of(without_proxy), (None, None))


class ClearPartsTests(unittest.TestCase):
    def full_level(self) -> SshAccessProfile:
        return shared(
            ssh_user="root", ssh_port=2222, ssh_private_key="KEY\n", ssh_passphrase="pp",
            ssh_proxy="1.2.3.4:1080", ssh_proxy_mode="always",
        )

    def test_clear_login_keeps_port_and_proxy(self):
        level = self.full_level()

        clear_parts(level, {AccessPartName.LOGIN})

        self.assertEqual((level.ssh_user, level.ssh_private_key, level.ssh_passphrase), (None, None, None))
        self.assertEqual(level.ssh_port, 2222)
        self.assertEqual(proxy_of(level), ("1.2.3.4:1080", "always"))

    def test_clear_proxy_removes_address_and_mode(self):
        level = self.full_level()

        clear_parts(level, {AccessPartName.PROXY})

        self.assertEqual(proxy_of(level), (None, None))
        self.assertEqual(level.ssh_private_key, "KEY\n")

    def test_level_with_nothing_left_is_blank(self):
        level = shared(ssh_port=2222)
        self.assertFalse(is_blank(level))

        clear_parts(level, {AccessPartName.PORT})

        self.assertTrue(is_blank(level))

    def test_refusal_of_inherited_proxy_is_not_blank(self):
        self.assertFalse(is_blank(shared(ssh_proxy_mode="off")))


def pinned_server(**fields) -> Server:
    return Server(id=1, name="node-1", url="https://10.0.0.1:9100", position=1, ssh_host_key="KEY", **fields)


class StoreServerAccessTests(unittest.TestCase):
    def test_server_secret_resets_pinned_host_key(self):
        for change in (AccessChange(password="pw"), AccessChange(private_key="K"), AccessChange(password="")):
            server = pinned_server(ssh_password="pw")

            store_server_access(server, change)

            self.assertIsNone(server.ssh_host_key, change)

    def test_port_alone_resets_host_key_only_when_it_changed(self):
        same = pinned_server(ssh_port=22)
        moved = pinned_server(ssh_port=22)

        store_server_access(same, AccessChange(port=22))
        store_server_access(moved, AccessChange(port=2222))

        self.assertEqual(same.ssh_host_key, "KEY")
        self.assertIsNone(moved.ssh_host_key)

    def test_new_host_is_stored_and_resets_host_key(self):
        server = pinned_server(ssh_host="10.0.0.1")

        store_server_access(server, AccessChange(), ssh_host=" 10.0.0.9 ")

        self.assertEqual((server.ssh_host, server.ssh_host_key), ("10.0.0.9", None))

    def test_empty_host_falls_back_to_the_server_address(self):
        server = pinned_server(ssh_host="10.0.0.9")

        store_server_access(server, AccessChange(), ssh_host="")

        self.assertIsNone(server.ssh_host)

    def test_proxy_change_keeps_pinned_host_key(self):
        server = pinned_server()

        store_server_access(server, AccessChange(proxy="1.2.3.4:1080"))

        self.assertEqual((server.ssh_proxy, server.ssh_host_key), ("1.2.3.4:1080", "KEY"))


class RulesTests(unittest.IsolatedAsyncioTestCase):
    def test_rules_keep_shared_apart_from_folders(self):
        rules = rules_from_profiles([shared(ssh_password="g"), SshAccessProfile(folder="RU", ssh_port=2222)])

        self.assertEqual((rules.shared.password, rules.folders["RU"].port), ("g", 2222))
        self.assertNotIn("", rules.folders)

    def test_no_profiles_mean_no_shared_level(self):
        rules = rules_from_profiles([])

        self.assertIsNone(rules.shared)
        self.assertEqual(dict(rules.folders), {})

    async def test_load_rules_reads_the_table_once(self):
        db = FakeDb(profiles=[shared(ssh_password="g"), SshAccessProfile(folder="RU", ssh_private_key="K\n")])

        rules = await load_rules(db)

        self.assertEqual(len(db.queries), 1)
        self.assertEqual((rules.shared.password, rules.folders["RU"].private_key), ("g", "K\n"))


if __name__ == "__main__":
    unittest.main()
