"""Постустановочные опции автоустановки: Wildcard SSL и nginx-профиль Remnawave.

Привязка к профилю вынесена в apply_server_link и переиспользуется роутером и
деплой-джобой — тесты фиксируют контракт: домен обязателен только когда шаблону
есть что им заменить.
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError  # noqa: E402

from app.services.remnawave_nginx_config import DOMAIN_PLACEHOLDER  # noqa: E402
from app.services.remnawave_nginx_sync import (  # noqa: E402
    NginxLinkError,
    apply_server_link,
)
from app.routers.server_deploy import DeployRequest  # noqa: E402
from app.routers.wildcard_ssl import (  # noqa: E402
    ReloadCmdPreset,
    upsert_reload_preset,
)


def make_profile(config_content: str, profile_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(id=profile_id, config_content=config_content)


def make_server(domain: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        remnawave_nginx_domain=domain,
        active_remnawave_nginx_profile_id=None,
        remnawave_nginx_sync_status=None,
    )


class ApplyServerLinkTests(unittest.TestCase):
    def test_link_with_domain_sets_binding_and_pending(self):
        profile = make_profile(f"server_name {DOMAIN_PLACEHOLDER};")
        server = make_server()
        apply_server_link(profile, server, "node.example.com")
        self.assertEqual(server.remnawave_nginx_domain, "node.example.com")
        self.assertEqual(server.active_remnawave_nginx_profile_id, 7)
        self.assertEqual(server.remnawave_nginx_sync_status, "pending")

    def test_placeholder_without_any_domain_raises(self):
        profile = make_profile(f"server_name {DOMAIN_PLACEHOLDER};")
        with self.assertRaises(NginxLinkError):
            apply_server_link(profile, make_server(), None)

    def test_existing_server_domain_is_enough(self):
        profile = make_profile(f"server_name {DOMAIN_PLACEHOLDER};")
        server = make_server(domain="old.example.com")
        apply_server_link(profile, server, None)
        self.assertEqual(server.remnawave_nginx_domain, "old.example.com")
        self.assertEqual(server.remnawave_nginx_sync_status, "pending")

    def test_wildcard_profile_needs_no_domain(self):
        # Профиль с wildcard-доменом не содержит плейсхолдера — домен ноды не нужен
        profile = make_profile("server_name example.com *.example.com;")
        server = make_server()
        apply_server_link(profile, server, None)
        self.assertIsNone(server.remnawave_nginx_domain)
        self.assertEqual(server.active_remnawave_nginx_profile_id, 7)


class ReloadCmdPresetTests(unittest.TestCase):
    def test_upsert_appends_and_replaces_by_name(self):
        presets = upsert_reload_preset([], "nginx", "systemctl reload nginx")
        presets = upsert_reload_preset(presets, "remna", "docker exec remnawave-nginx nginx -s reload")
        presets = upsert_reload_preset(presets, "nginx", "nginx -s reload")
        self.assertEqual(len(presets), 2)
        self.assertEqual(presets[0], {"name": "nginx", "command": "nginx -s reload"})

    def test_preset_values_are_stripped(self):
        preset = ReloadCmdPreset(name="  remna  ", command="  nginx -s reload  ")
        self.assertEqual(preset.name, "remna")
        self.assertEqual(preset.command, "nginx -s reload")

    def test_blank_and_oversized_values_are_rejected(self):
        with self.assertRaises(ValidationError):
            ReloadCmdPreset(name="   ", command="x")
        with self.assertRaises(ValidationError):
            ReloadCmdPreset(name="ok", command="x" * 513)


class DeployRequestOptionsTests(unittest.TestCase):
    def request(self, **kwargs) -> DeployRequest:
        return DeployRequest(name="n1", host="1.2.3.4", **kwargs)

    def test_nginx_domain_is_normalized(self):
        req = self.request(remnawave_nginx_domain="  ExAmple.COM ")
        self.assertEqual(req.remnawave_nginx_domain, "example.com")

    def test_blank_nginx_domain_becomes_none(self):
        self.assertIsNone(self.request(remnawave_nginx_domain="   ").remnawave_nginx_domain)

    def test_invalid_nginx_domain_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.request(remnawave_nginx_domain="not a domain")

    def test_oversized_reload_cmd_is_rejected(self):
        # Зеркало MAX_RELOAD_COMMAND_LEN ноды: длиннее нода всё равно не примет
        with self.assertRaises(ValidationError):
            self.request(wildcard_ssl_reload_cmd="x" * 513)


if __name__ == "__main__":
    unittest.main()
