"""Tests for certificate mount handling in the Remnawave nginx manager.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Пути сертификатов в конфиге — это пути внутри контейнера. Проверяем главное:
пути извлекаются корректно, непокрытые томами каталоги вычисляются и
дописываются в compose ровно в сервис nginx, а маппинг контейнер→хост
учитывает существующие тома (включая относительный ./ssl).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.remnawave_nginx_manager import (  # noqa: E402
    CERT_HINT,
    LETSENCRYPT_ROOT,
    cert_mount_dir,
    config_cert_files,
    explain_validation_error,
    host_path_for_cert,
    is_covered_by_mounts,
    missing_cert_mounts,
    patch_compose_volumes,
)

CONFIG = """server {
    listen 443 ssl;
    server_name node.example.com;

    ssl_certificate     /etc/letsencrypt/live/node.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/node.example.com/privkey.pem;
}
"""

CUSTOM_CONFIG = """server {
    ssl_certificate     /opt/certs/site/fullchain.pem;
    ssl_certificate_key /opt/certs/site/privkey.pem;
}
server {
    ssl_certificate     /etc/nginx/ssl/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/privkey.pem;
}
"""

COMPOSE = """services:
  remnawave-nginx:
    image: nginx:1.28
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./ssl:/etc/nginx/ssl:ro
      - /dev/shm:/dev/shm:rw
  remnanode:
    image: remnawave/node:latest
    volumes:
      - /dev/shm:/dev/shm:rw
"""

COMPOSE_WITH_LE = COMPOSE.replace(
    "      - ./ssl:/etc/nginx/ssl:ro\n",
    "      - ./ssl:/etc/nginx/ssl:ro\n      - /etc/letsencrypt:/etc/letsencrypt:ro\n",
)


class ConfigCertFilesTests(unittest.TestCase):
    def test_extracts_cert_and_key_without_duplicates(self):
        files = config_cert_files(CONFIG + CONFIG)
        self.assertEqual(files, [
            "/etc/letsencrypt/live/node.example.com/fullchain.pem",
            "/etc/letsencrypt/live/node.example.com/privkey.pem",
        ])

    def test_ignores_unsafe_paths(self):
        content = 'ssl_certificate $ssl_cert;\nssl_certificate_key "relative/key.pem";\n'
        self.assertEqual(config_cert_files(content), [])


class MountDirTests(unittest.TestCase):
    def test_letsencrypt_paths_mount_whole_root(self):
        # live/* — симлинки в ../../archive, узкий маунт их порвал бы
        self.assertEqual(
            cert_mount_dir("/etc/letsencrypt/live/x.com/fullchain.pem"), LETSENCRYPT_ROOT
        )

    def test_custom_path_mounts_parent_dir(self):
        self.assertEqual(cert_mount_dir("/opt/certs/site/fullchain.pem"), "/opt/certs/site")


class MissingMountsTests(unittest.TestCase):
    def test_letsencrypt_missing_in_default_compose(self):
        dirs = missing_cert_mounts(COMPOSE, config_cert_files(CONFIG))
        self.assertEqual(dirs, [LETSENCRYPT_ROOT])

    def test_covered_when_letsencrypt_mounted(self):
        self.assertEqual(missing_cert_mounts(COMPOSE_WITH_LE, config_cert_files(CONFIG)), [])

    def test_nginx_ssl_covered_by_relative_mount(self):
        dirs = missing_cert_mounts(COMPOSE, config_cert_files(CUSTOM_CONFIG))
        self.assertEqual(dirs, ["/opt/certs/site"])

    def test_prefix_match_is_per_component(self):
        # /etc/letsencrypt-extra не должен считаться покрытым маунтом /etc/letsencrypt
        self.assertFalse(is_covered_by_mounts(
            "/etc/letsencrypt-extra/cert.pem", [LETSENCRYPT_ROOT]
        ))
        self.assertTrue(is_covered_by_mounts(
            "/etc/letsencrypt/live/x/fullchain.pem", [LETSENCRYPT_ROOT]
        ))


class PatchVolumesTests(unittest.TestCase):
    def test_inserts_after_nginx_conf_mount(self):
        patched = patch_compose_volumes(COMPOSE, [LETSENCRYPT_ROOT])
        self.assertIsNotNone(patched)
        lines = patched.splitlines()
        conf_idx = next(i for i, l in enumerate(lines) if "./nginx.conf:" in l)
        self.assertEqual(
            lines[conf_idx + 1].strip(), f"- {LETSENCRYPT_ROOT}:{LETSENCRYPT_ROOT}:ro"
        )
        # Отступ совпадает со строкой монтирования nginx.conf
        self.assertEqual(
            lines[conf_idx + 1][: len(lines[conf_idx]) - len(lines[conf_idx].lstrip())],
            lines[conf_idx][: len(lines[conf_idx]) - len(lines[conf_idx].lstrip())],
        )
        # Сервис remnanode не задет
        self.assertEqual(patched.count("remnawave/node:latest"), 1)

    def test_nothing_to_add_returns_none(self):
        self.assertIsNone(patch_compose_volumes(COMPOSE, []))

    def test_compose_without_nginx_conf_mount_returns_none(self):
        self.assertIsNone(patch_compose_volumes("services:\n  x:\n", [LETSENCRYPT_ROOT]))

    def test_patch_then_recheck_is_idempotent(self):
        cert_files = config_cert_files(CONFIG)
        patched = patch_compose_volumes(COMPOSE, missing_cert_mounts(COMPOSE, cert_files))
        self.assertEqual(missing_cert_mounts(patched, cert_files), [])


class HostPathTests(unittest.TestCase):
    def test_maps_relative_mount_to_install_path(self):
        self.assertEqual(
            host_path_for_cert("/etc/nginx/ssl/fullchain.pem", COMPOSE, "/opt/remnawave"),
            "/opt/remnawave/ssl/fullchain.pem",
        )

    def test_maps_absolute_mount_source(self):
        self.assertEqual(
            host_path_for_cert(
                "/etc/letsencrypt/live/x/privkey.pem", COMPOSE_WITH_LE, "/opt/remnawave"
            ),
            "/etc/letsencrypt/live/x/privkey.pem",
        )

    def test_unmounted_path_maps_to_itself(self):
        self.assertEqual(
            host_path_for_cert("/opt/certs/site/fullchain.pem", COMPOSE, "/opt/remnawave"),
            "/opt/certs/site/fullchain.pem",
        )


class ExplainErrorTests(unittest.TestCase):
    def test_appends_hint_for_missing_certificate(self):
        output = 'nginx: [emerg] cannot load certificate "/etc/letsencrypt/live/x/fullchain.pem"'
        self.assertIn(CERT_HINT, explain_validation_error(output))

    def test_other_errors_untouched(self):
        output = 'nginx: [emerg] unknown directive "foo"'
        self.assertEqual(explain_validation_error(output), output)


if __name__ == "__main__":
    unittest.main()
