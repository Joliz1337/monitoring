"""Tests for host-specific nginx limit substitution.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

Профиль в панели один на весь флот, поэтому числа, зависящие от размера хоста,
подставляет нода. Проверяем главное: подставляется только помеченное маркером,
подстановка идемпотентна, а ручные правки оператора не затираются.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.remnawave_nginx_manager import (  # noqa: E402
    AUTO_MARKER,
    FULL_CONFIG_MOUNT,
    compose_mount_target,
    compute_host_limits,
    patch_compose_mount,
    patch_host_limits,
)

FRAGMENT_COMPOSE = """services:
  remnawave-nginx:
    image: nginx:1.28
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - /dev/shm:/dev/shm:rw
"""

FULL_COMPOSE = FRAGMENT_COMPOSE.replace(
    "/etc/nginx/conf.d/default.conf", FULL_CONFIG_MOUNT
)

TEMPLATE = f"""worker_processes auto;
worker_rlimit_nofile 65536;  {AUTO_MARKER}

events {{
    worker_connections 8192;  {AUTO_MARKER}
    multi_accept on;
}}

http {{
    ssl_session_cache shared:SSL:20m;  {AUTO_MARKER}
    keepalive_timeout 75s;
}}
"""

LIMITS = {
    "worker_rlimit_nofile": 262144,
    "worker_connections": 65536,
    "ssl_session_cache": 40,
}


class PatchTests(unittest.TestCase):
    def test_substitutes_marked_lines(self):
        result = patch_host_limits(TEMPLATE, LIMITS)
        self.assertIn(f"worker_rlimit_nofile 262144;  {AUTO_MARKER}", result)
        self.assertIn(f"worker_connections 65536;  {AUTO_MARKER}", result)
        self.assertIn(f"ssl_session_cache shared:SSL:40m;  {AUTO_MARKER}", result)

    def test_idempotent(self):
        once = patch_host_limits(TEMPLATE, LIMITS)
        self.assertEqual(once, patch_host_limits(once, LIMITS))

    def test_untouched_without_marker(self):
        manual = TEMPLATE.replace(f"  {AUTO_MARKER}", "")
        self.assertEqual(patch_host_limits(manual, LIMITS), manual)

    def test_other_directives_intact(self):
        result = patch_host_limits(TEMPLATE, LIMITS)
        self.assertIn("worker_processes auto;", result)
        self.assertIn("keepalive_timeout 75s;", result)
        self.assertIn("multi_accept on;", result)


class ComposeMountTests(unittest.TestCase):
    def test_detects_fragment_mount(self):
        self.assertEqual(
            compose_mount_target(FRAGMENT_COMPOSE), "/etc/nginx/conf.d/default.conf"
        )
        self.assertEqual(compose_mount_target(FULL_COMPOSE), FULL_CONFIG_MOUNT)

    def test_patches_fragment_to_full(self):
        patched = patch_compose_mount(FRAGMENT_COMPOSE)
        self.assertIsNotNone(patched)
        self.assertIn(f"./nginx.conf:{FULL_CONFIG_MOUNT}:ro", patched)
        # Остальные тома и поля compose не задеты
        self.assertIn("- /dev/shm:/dev/shm:rw", patched)
        self.assertIn("image: nginx:1.28", patched)

    def test_full_mount_needs_no_patch(self):
        self.assertIsNone(patch_compose_mount(FULL_COMPOSE))

    def test_compose_without_mount(self):
        self.assertIsNone(patch_compose_mount("services:\n  remnawave-nginx:\n"))


class ComputeTests(unittest.TestCase):
    def test_worker_connections_follow_nofile(self):
        limits = compute_host_limits(compose_nofile=65536)
        # На хосте, где рендерер не оставил фактов, берём потолок из compose
        if limits["worker_rlimit_nofile"] == 65536:
            self.assertEqual(limits["worker_connections"], 16384)

    def test_values_are_sane(self):
        limits = compute_host_limits()
        self.assertGreaterEqual(limits["worker_rlimit_nofile"], 1024)
        self.assertGreaterEqual(limits["worker_connections"], 1024)
        self.assertLessEqual(limits["worker_connections"], 65536)
        self.assertGreaterEqual(limits["ssl_session_cache"], 10)
        self.assertLessEqual(limits["ssl_session_cache"], 100)


if __name__ == "__main__":
    unittest.main()
