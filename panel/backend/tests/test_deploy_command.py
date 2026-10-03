"""Команда автоустановки ноды: кастомный порт API, язык панели и прокси туннеля уезжают на ноду.

Раньше monitoring_port из формы деплоя влиял только на URL сервера в панели,
а нода всё равно поднимала nginx на 9100 — подключение по кастомному порту
было мёртвым сразу после установки.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError  # noqa: E402

from app.routers.server_deploy import DeployRequest  # noqa: E402
from app.services.deploy_service import (  # noqa: E402
    DeployParams,
    InstallerLanguage,
    build_haproxy_upgrade_command,
    build_install_command,
)


def params(**kwargs) -> DeployParams:
    return DeployParams(host="1.2.3.4", ssh_port=22, ssh_user="root", node_secret="tok", **kwargs)


class DeployCommandTests(unittest.TestCase):
    def test_custom_port_is_exported_to_the_installer(self):
        command = build_install_command(params(node_api_port=12345))
        self.assertIn("NODE_API_PORT=12345", command)

    def test_default_port_is_not_exported(self):
        # Дефолт не тащим в env: .env ноды не засоряется, поведение старых установок не меняется
        command = build_install_command(params(node_api_port=9100))
        self.assertNotIn("NODE_API_PORT", command)

    def test_panel_language_is_exported_to_the_installer(self):
        command = build_install_command(params(lang=InstallerLanguage.RU))
        self.assertIn("MON_LANG=ru", command)

    def test_language_defaults_to_english(self):
        # Явный en важен: при переустановке перебивает русский, оставшийся от прошлой ноды
        command = build_install_command(params())
        self.assertIn("MON_LANG=en", command)


class TunnelCommandTests(unittest.TestCase):
    """Установка через панель: сервер за ТСПУ качает и сам install.sh, и всё
    остальное через прокси панели, который установщик снимает по завершении."""

    TUNNEL = "http://127.0.0.1:41234"

    def test_installer_is_downloaded_through_the_tunnel(self):
        command = build_install_command(params(via_panel=True), self.TUNNEL)
        self.assertTrue(command.startswith(f"curl -fsSL --proxy {self.TUNNEL} https://"))

    def test_installer_gets_temporary_proxy(self):
        command = build_install_command(params(via_panel=True), self.TUNNEL)
        self.assertIn(f"MON_PROXY_URL={self.TUNNEL}", command)
        self.assertIn("MON_PROXY_TEMPORARY=1", command)

    def test_tunnel_replaces_operator_proxy(self):
        command = build_install_command(params(proxy_url="http://10.0.0.1:3128"), self.TUNNEL)
        self.assertNotIn("10.0.0.1", command)

    def test_without_tunnel_command_is_unchanged(self):
        # Полуавтомат собирает команду без туннеля: флаг via_panel там ни на что не влияет
        command = build_install_command(params(via_panel=True, proxy_url="http://10.0.0.1:3128"))
        self.assertNotIn("--proxy", command)
        self.assertNotIn("MON_PROXY_TEMPORARY", command)
        self.assertIn("MON_PROXY_URL=http://10.0.0.1:3128", command)

    def test_haproxy_upgrade_through_the_tunnel(self):
        command = build_haproxy_upgrade_command(self.TUNNEL)
        self.assertTrue(command.startswith(f"curl -fsSL --proxy {self.TUNNEL} https://"))
        self.assertIn("MON_INSTALL_HAPROXY=1", command)
        self.assertIn(f"MON_PROXY_URL={self.TUNNEL}", command)
        self.assertIn("MON_PROXY_TEMPORARY=1", command)

    def test_haproxy_upgrade_through_agent_has_no_proxy(self):
        command = build_haproxy_upgrade_command()
        self.assertNotIn("--proxy", command)
        self.assertNotIn("MON_PROXY_URL", command)


class DeployRequestProxyTests(unittest.TestCase):
    def test_tunnel_and_operator_proxy_are_exclusive(self):
        with self.assertRaises(ValidationError):
            DeployRequest(name="n", host="1.2.3.4", via_panel=True, install_proxy=True, proxy_url="1.1.1.1:3128")

    def test_tunnel_alone_is_accepted(self):
        self.assertTrue(DeployRequest(name="n", host="1.2.3.4", via_panel=True).via_panel)


if __name__ == "__main__":
    unittest.main()
