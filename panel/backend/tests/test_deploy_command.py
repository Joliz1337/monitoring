"""Команда автоустановки ноды: кастомный порт API и язык панели уезжают на ноду.

Раньше monitoring_port из формы деплоя влиял только на URL сервера в панели,
а нода всё равно поднимала nginx на 9100 — подключение по кастомному порту
было мёртвым сразу после установки.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.deploy_service import (  # noqa: E402
    DeployParams,
    InstallerLanguage,
    _build_inner_command,
)


def params(**kwargs) -> DeployParams:
    return DeployParams(host="1.2.3.4", ssh_port=22, ssh_user="root", node_secret="tok", **kwargs)


class DeployCommandTests(unittest.TestCase):
    def test_custom_port_is_exported_to_the_installer(self):
        command = _build_inner_command(params(node_api_port=12345))
        self.assertIn("NODE_API_PORT=12345", command)

    def test_default_port_is_not_exported(self):
        # Дефолт не тащим в env: .env ноды не засоряется, поведение старых установок не меняется
        command = _build_inner_command(params(node_api_port=9100))
        self.assertNotIn("NODE_API_PORT", command)

    def test_panel_language_is_exported_to_the_installer(self):
        command = _build_inner_command(params(lang=InstallerLanguage.RU))
        self.assertIn("MON_LANG=ru", command)

    def test_language_defaults_to_english(self):
        # Явный en важен: при переустановке перебивает русский, оставшийся от прошлой ноды
        command = _build_inner_command(params())
        self.assertIn("MON_LANG=en", command)


if __name__ == "__main__":
    unittest.main()
