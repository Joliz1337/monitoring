"""Tests for the pure parts of app.services.ssh_config_manager.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Сборка sshd_config — единственное место в проекте, ошибка в котором отрезает
администратора от сервера: конфиг применяется, sshd перезапускается, и вернуть
доступ можно только через консоль хостера. Проверяются свойства, на которых
держится безопасность правки: закомментированное не оживает, Match-блоки не
трогаются, а недостающие директивы попадают ДО первого Match — внутри блока они
означали бы совсем другое.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.ssh_config_manager import (  # noqa: E402
    SSHD_DEFAULTS,
    SSHD_KEY_MAP,
    SSHConfigManager,
)


def make_manager() -> SSHConfigManager:
    """Менеджер без детекта окружения — конструктор ходит в systemd."""
    return SSHConfigManager.__new__(SSHConfigManager)


REVERSE_MAP = {v.lower(): k for k, v in SSHD_KEY_MAP.items()}


class ValueParsingTests(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()

    def test_yes_no_becomes_bool(self):
        self.assertIs(self.manager._parse_sshd_value("password_authentication", "yes"), True)
        self.assertIs(self.manager._parse_sshd_value("password_authentication", "NO"), False)
        # Всё, что не "yes", — запрет: у sshd нет третьего состояния
        self.assertIs(self.manager._parse_sshd_value("password_authentication", "maybe"), False)

    def test_numeric_keys_become_int(self):
        self.assertEqual(self.manager._parse_sshd_value("port", "2222"), 2222)
        self.assertEqual(self.manager._parse_sshd_value("max_auth_tries", "3"), 3)

    def test_broken_number_falls_back_to_default(self):
        # Мусор в конфиге не должен превращаться в строку там, где ждут int
        self.assertEqual(self.manager._parse_sshd_value("port", "abc"), SSHD_DEFAULTS["port"])

    def test_allow_users_splits_into_list(self):
        self.assertEqual(
            self.manager._parse_sshd_value("allow_users", "root  admin\tdeploy"),
            ["root", "admin", "deploy"],
        )

    def test_free_form_value_stays_string(self):
        self.assertEqual(
            self.manager._parse_sshd_value("permit_root_login", "prohibit-password"),
            "prohibit-password",
        )


class ValueFormattingTests(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()

    def test_round_trip_for_every_managed_key(self):
        samples = {
            "port": 2222,
            "permit_root_login": "prohibit-password",
            "password_authentication": False,
            "pubkey_authentication": True,
            "permit_empty_passwords": False,
            "max_auth_tries": 3,
            "login_grace_time": 30,
            "client_alive_interval": 300,
            "client_alive_count_max": 2,
            "max_sessions": 5,
            "max_startups": "10:30:60",
            "allow_users": ["root", "admin"],
            "x11_forwarding": False,
        }
        self.assertEqual(set(samples), set(SSHD_KEY_MAP))
        for key, value in samples.items():
            with self.subTest(key=key):
                formatted = self.manager._format_sshd_value(key, value)
                self.assertEqual(self.manager._parse_sshd_value(key, formatted), value)


class FileParsingTests(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()

    def parse(self, content: str) -> dict:
        return self.manager._parse_sshd_file(content, REVERSE_MAP)

    def test_first_match_wins(self):
        # sshd берёт первое вхождение — парсер обязан вести себя так же,
        # иначе панель показывала бы не то значение, по которому живёт сервер
        parsed = self.parse("Port 2222\nPort 22\n")
        self.assertEqual(parsed["port"], 2222)

    def test_comments_and_blank_lines_ignored(self):
        parsed = self.parse("# Port 9999\n\n   \nPort 2222\n")
        self.assertEqual(parsed["port"], 2222)

    def test_indented_directive_is_still_read(self):
        self.assertEqual(self.parse("    Port 2222\n")["port"], 2222)

    def test_directive_without_value_skipped(self):
        self.assertEqual(self.parse("Port\n"), {})

    def test_unknown_directives_ignored(self):
        parsed = self.parse("UsePAM yes\nPort 2222\n")
        self.assertEqual(parsed, {"port": 2222})

    def test_case_insensitive_directive_names(self):
        self.assertEqual(self.parse("port 2222\n")["port"], 2222)
        self.assertEqual(self.parse("PASSWORDAUTHENTICATION no\n")["password_authentication"], False)


class BuildContentTests(unittest.TestCase):
    """`_build_sshd_content` получает исходник параметром — ходить на хост не нужно."""

    def setUp(self):
        self.manager = make_manager()

    def build(self, original: str, config: dict) -> str:
        return self.manager._build_sshd_content(config, original=original)

    def test_active_directive_is_replaced_in_place(self):
        result = self.build("Port 22\nUsePAM yes\n", {"port": 2222})
        lines = result.splitlines()
        self.assertEqual(lines[0], "Port 2222")
        self.assertIn("UsePAM yes", lines)
        self.assertNotIn("Port 22", lines)

    def test_commented_directive_is_never_revived(self):
        # Раскомментирование включало бы опцию, которую администратор осознанно выключил
        result = self.build("#PasswordAuthentication no\n", {"port": 2222})
        self.assertIn("#PasswordAuthentication no", result.splitlines())
        self.assertNotIn("PasswordAuthentication no", result.splitlines())

    def test_missing_directive_is_appended(self):
        result = self.build("UsePAM yes\n", {"port": 2222})
        self.assertIn("Port 2222", result.splitlines())

    def test_missing_directive_lands_before_first_match_block(self):
        original = "UsePAM yes\nMatch User git\n    PasswordAuthentication no\n"
        result = self.build(original, {"port": 2222})
        lines = result.splitlines()
        self.assertLess(lines.index("Port 2222"), lines.index("Match User git"))

    def test_match_block_contents_are_copied_verbatim(self):
        original = (
            "Port 22\n"
            "Match User git\n"
            "    PasswordAuthentication no\n"
            "    X11Forwarding no\n"
        )
        result = self.build(original, {"port": 2222, "password_authentication": True})
        lines = result.splitlines()
        # Директива внутри Match относится только к этому пользователю —
        # подмена там сменила бы смысл правила
        self.assertIn("    PasswordAuthentication no", lines)
        self.assertIn("    X11Forwarding no", lines)
        self.assertEqual(lines[0], "Port 2222")

    def test_result_always_ends_with_newline(self):
        self.assertTrue(self.build("Port 22", {"port": 2222}).endswith("\n"))

    def test_empty_original_yields_full_directive_set(self):
        result = self.build("", {"port": 2222, "x11_forwarding": False})
        self.assertIn("Port 2222", result.splitlines())
        self.assertIn("X11Forwarding no", result.splitlines())

    def test_idempotent(self):
        config = {"port": 2222, "password_authentication": False}
        once = self.build("Port 22\nUsePAM yes\n", config)
        self.assertEqual(self.build(once, config), once)

    def test_round_trip_through_parser(self):
        config = {"port": 2222, "max_auth_tries": 3, "allow_users": ["root", "admin"]}
        built = self.build("Port 22\n", config)
        parsed = self.manager._parse_sshd_file(built, REVERSE_MAP)
        for key, value in config.items():
            self.assertEqual(parsed[key], value)


class Fail2banParsingTests(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()

    def test_section_is_read(self):
        content = "[DEFAULT]\nbantime = 600\n\n[sshd]\nenabled = true\nmaxretry = 5\nbantime = 1h\n"
        section = self.manager._parse_fail2ban_section(content, "sshd")
        self.assertEqual(section.get("enabled"), "true")
        self.assertEqual(section.get("maxretry"), "5")
        self.assertEqual(section.get("bantime"), "1h")

    def test_other_sections_do_not_leak(self):
        content = "[DEFAULT]\nmaxretry = 99\n\n[sshd]\nmaxretry = 5\n"
        self.assertEqual(self.manager._parse_fail2ban_section(content, "sshd").get("maxretry"), "5")

    def test_missing_section_is_empty(self):
        self.assertEqual(self.manager._parse_fail2ban_section("[DEFAULT]\nx = 1\n", "sshd"), {})

    def test_ban_time_units(self):
        self.assertEqual(self.manager._convert_ban_time("600"), 600)
        self.assertEqual(self.manager._convert_ban_time("30m"), 1800)
        self.assertEqual(self.manager._convert_ban_time("1h"), 3600)
        self.assertEqual(self.manager._convert_ban_time("1d"), 86400)


if __name__ == "__main__":
    unittest.main()
