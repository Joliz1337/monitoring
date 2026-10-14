"""Раскатка нескольких wildcard-сертификатов на один сервер.

Сервер получает закреплённые за ним сертификаты, служба перечитывает их один раз —
после файлов последнего. Свои абсолютные пути вмещают только один сертификат:
второй затирал бы первый, и при каждом продлении они сменяли бы друг друга.
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.wildcard_ssl import (  # noqa: E402
    CUSTOM_PATHS_SINGLE_CERT_MESSAGE,
    NO_CERTIFICATES_MESSAGE,
    WildcardSSLManager,
)

RELOAD = "systemctl reload nginx"


def make_server(**overrides) -> SimpleNamespace:
    fields = {
        "id": 1,
        "name": "node-1",
        "wildcard_ssl_reload_cmd": RELOAD,
        "wildcard_ssl_custom_path_enabled": False,
        "wildcard_ssl_custom_fullchain_path": None,
        "wildcard_ssl_custom_privkey_path": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def make_custom_path_server() -> SimpleNamespace:
    return make_server(
        wildcard_ssl_custom_path_enabled=True,
        wildcard_ssl_custom_fullchain_path="/etc/pve/local/pveproxy-ssl.pem",
        wildcard_ssl_custom_privkey_path="/etc/pve/local/pveproxy-ssl.key",
    )


def make_cert(cert_id: int, base_domain: str) -> SimpleNamespace:
    return SimpleNamespace(id=cert_id, domain=f"*.{base_domain}", base_domain=base_domain)


FIRST = make_cert(1, "a.example")
SECOND = make_cert(2, "b.example")


class DeployToNodeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = WildcardSSLManager()
        self.pushed: list[tuple[int, str]] = []
        self.failing_cert_id: int | None = None

        async def push(server, cert, reload_command):
            self.pushed.append((cert.id, reload_command))
            if cert.id == self.failing_cert_id:
                return False, "Reload command failed", None
            return True, "Certificate deployed successfully", None

        self.manager._push_certificate = push

    async def test_reload_runs_once_after_last_certificate(self):
        result = await self.manager.deploy_to_node(make_server(), [FIRST, SECOND])
        self.assertTrue(result["success"])
        self.assertEqual(self.pushed, [(1, ""), (2, RELOAD)])
        self.assertIn("*.a.example", result["message"])
        self.assertIn("*.b.example", result["message"])

    async def test_certificate_filter_deploys_only_that_one_with_reload(self):
        result = await self.manager.deploy_to_node(make_server(), [FIRST, SECOND], cert_id=2)
        self.assertTrue(result["success"])
        self.assertEqual(self.pushed, [(2, RELOAD)])

    async def test_server_without_certificates_is_reported(self):
        result = await self.manager.deploy_to_node(make_server(), [])
        self.assertFalse(result["success"])
        self.assertEqual(result["message"], NO_CERTIFICATES_MESSAGE)
        self.assertEqual(result["server_name"], "node-1")
        self.assertEqual(self.pushed, [])

    async def test_custom_paths_refuse_several_certificates_even_when_filtered(self):
        # Фильтр по одному сертификату не спасает: продление второго затёрло бы первый
        result = await self.manager.deploy_to_node(make_custom_path_server(), [FIRST, SECOND], cert_id=1)
        self.assertFalse(result["success"])
        self.assertEqual(result["message"], CUSTOM_PATHS_SINGLE_CERT_MESSAGE)
        self.assertEqual(self.pushed, [])

    async def test_custom_paths_accept_single_certificate(self):
        result = await self.manager.deploy_to_node(make_custom_path_server(), [FIRST])
        self.assertTrue(result["success"])
        self.assertEqual(self.pushed, [(1, RELOAD)])

    async def test_custom_mode_without_both_paths_uses_folders(self):
        server = make_server(
            wildcard_ssl_custom_path_enabled=True,
            wildcard_ssl_custom_fullchain_path="/etc/pve/local/pveproxy-ssl.pem",
        )
        result = await self.manager.deploy_to_node(server, [FIRST, SECOND])
        self.assertTrue(result["success"])

    async def test_failure_stops_and_names_certificate(self):
        self.failing_cert_id = 1
        result = await self.manager.deploy_to_node(make_server(), [FIRST, SECOND])
        self.assertFalse(result["success"])
        self.assertTrue(result["message"].startswith("*.a.example: "))
        self.assertEqual(self.pushed, [(1, "")])


if __name__ == "__main__":
    unittest.main()
