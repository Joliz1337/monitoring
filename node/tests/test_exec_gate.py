"""Запись в свой раздел — ещё не право исполнять на хосте: проверки по телу запроса.

Runnable with plain stdlib:  python -m unittest discover -s node/tests

При ограничивающем NODE_CAPABILITIES раздел без `exec` не должен давать root
обходным путём: командой после раскладки сертификата, файлом мимо каталогов
сертификатов, Lua или секцией program в конфиге HAProxy, откатом ноды на
версию, которая о правах ещё не знает. Тела запроса слой прав не видит —
решают обработчики; нода без ограничений не меняется ни в чём.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import capabilities  # noqa: E402
from app.capabilities import DenyLog, parse_capabilities  # noqa: E402
from app.models.haproxy import ConfigApplyRequest  # noqa: E402
from app.models.ssl import WildcardDeployRequest, WildcardDeployResponse  # noqa: E402
from app.routers import haproxy as haproxy_router  # noqa: E402
from app.routers import ssl as ssl_router  # noqa: E402
from app.routers import system  # noqa: E402
from app.services.ssl_manager import SSLManager  # noqa: E402

DEFAULT_DEPLOY_PATH = "/etc/letsencrypt/live/example.com"
PLAIN_CONFIG = "global\n    maxconn 1000\nfrontend a\n    bind *:443\n"
LUA_CONFIG = PLAIN_CONFIG + "global\n    lua-load /etc/haproxy/hook.lua\n"
RUNNING_VERSION = "10.36.0"
APPLY_MARKER = "APPLY_UPDATE_STARTED"


def node_policy(raw: str):
    """Права ноды на время теста; свой журнал отказов — общий молчит пять минут после первого."""
    return mock.patch.multiple(
        capabilities, get_policy=lambda: parse_capabilities(raw), _handler_deny_log=DenyLog(),
    )


def denied_domain(response) -> Optional[str]:
    if getattr(response, "status_code", None) != 403:
        return None
    return json.loads(response.body)["domain"]


class SslDeployTests(unittest.TestCase):

    def deploy(self, raw_policy: str, **fields) -> tuple[object, int]:
        manager = SSLManager()
        manager.deploy_wildcard = mock.AsyncMock(
            return_value=WildcardDeployResponse(success=True, message="Certificate deployed successfully"),
        )
        request = WildcardDeployRequest(fullchain_pem="CERT", privkey_pem="KEY", **fields)
        with node_policy(raw_policy), mock.patch.object(ssl_router, "get_ssl_manager", return_value=manager):
            response = asyncio.run(ssl_router.deploy_wildcard(request))
        return response, manager.deploy_wildcard.await_count

    def test_certificate_into_the_panel_default_location_needs_only_ssl(self):
        response, deployed = self.deploy("ssl", deploy_path=DEFAULT_DEPLOY_PATH, reload_command="  ")

        self.assertTrue(response.success)
        self.assertEqual(deployed, 1)

    def test_system_certificate_directories_need_only_ssl(self):
        _, deployed = self.deploy(
            "ssl", custom_fullchain_path="/etc/ssl/certs/example.pem", custom_privkey_path="/etc/ssl/private/example.key",
        )

        self.assertEqual(deployed, 1)

    def test_reload_command_needs_exec(self):
        response, deployed = self.deploy("ssl", deploy_path=DEFAULT_DEPLOY_PATH, reload_command="systemctl reload nginx")

        self.assertEqual(denied_domain(response), "exec")
        self.assertEqual(deployed, 0)

    def test_file_outside_certificate_directories_needs_exec(self):
        inside = f"{DEFAULT_DEPLOY_PATH}/fullchain.pem"
        for outside in [
            "/etc/pve/local/pveproxy-ssl.key",
            "/etc/cron.d/job",
            # Хуки certbot: его таймер исполняет их от root
            "/etc/letsencrypt/renewal/example.com.conf",
            "/etc/letsencrypt/cli.ini",
            "/etc/letsencrypt/live/../renewal/example.com.conf",
            "/etc/ssl/openssl.cnf",
            "/etc/ssl/certs/../../cron.d/job",
            "/etc/letsencrypt/live",
            "//etc/ssl/private/example.key",
        ]:
            with self.subTest(outside=outside):
                response, deployed = self.deploy("ssl", custom_fullchain_path=inside, custom_privkey_path=outside)
                self.assertEqual(denied_domain(response), "exec")
                self.assertEqual(deployed, 0)

    def test_deploy_path_outside_certificate_directories_needs_exec(self):
        response, deployed = self.deploy("ssl", deploy_path="/etc/systemd/system", fullchain_filename="evil.service")

        self.assertEqual(denied_domain(response), "exec")
        self.assertEqual(deployed, 0)

    def test_exec_grant_opens_command_and_any_path(self):
        _, deployed = self.deploy(
            "ssl,exec", custom_fullchain_path="/etc/pve/local/pveproxy-ssl.pem",
            custom_privkey_path="/etc/pve/local/pveproxy-ssl.key", reload_command="systemctl restart pveproxy",
        )

        self.assertEqual(deployed, 1)

    def test_unrestricted_node_deploys_as_before(self):
        _, deployed = self.deploy(
            "", custom_fullchain_path="/etc/pve/local/pveproxy-ssl.pem",
            custom_privkey_path="/etc/pve/local/pveproxy-ssl.key", reload_command="systemctl restart pveproxy",
        )

        self.assertEqual(deployed, 1)

    def test_malformed_path_is_left_to_the_deploy_itself(self):
        # Такой запрос ничего не запишет: раскладка сама вернёт ошибку пути
        _, deployed = self.deploy("ssl", deploy_path="/etc/ssl/certs; reboot")

        self.assertEqual(deployed, 1)


class HaproxyConfigApplyTests(unittest.TestCase):

    def apply(self, raw_policy: str, config: str) -> tuple[object, int]:
        manager = mock.Mock()
        manager.apply_config.return_value = (True, "Config applied and reloaded", True)
        with node_policy(raw_policy), \
                mock.patch.object(haproxy_router, "get_haproxy_manager", return_value=manager), \
                mock.patch.object(haproxy_router, "get_loss_probe"):
            response = asyncio.run(haproxy_router.apply_config(ConfigApplyRequest(config_content=config)))
        return response, manager.apply_config.call_count

    def test_ordinary_config_needs_only_haproxy(self):
        response, applied = self.apply("haproxy", PLAIN_CONFIG)

        self.assertTrue(response.success)
        self.assertEqual(applied, 1)

    def test_config_that_runs_code_needs_exec(self):
        response, applied = self.apply("haproxy", LUA_CONFIG)

        self.assertEqual(denied_domain(response), "exec")
        self.assertEqual(applied, 0)

    def test_exec_grant_and_unrestricted_node_apply_any_config(self):
        for raw_policy in ["haproxy,exec", ""]:
            with self.subTest(raw_policy=raw_policy):
                _, applied = self.apply(raw_policy, LUA_CONFIG)
                self.assertEqual(applied, 1)


class DowngradeGuardTests(unittest.TestCase):

    def guard(self, raw_policy: str, running_version: str = RUNNING_VERSION) -> str:
        with mock.patch.object(system, "get_policy", return_value=parse_capabilities(raw_policy)), \
                mock.patch.object(system, "get_current_version", return_value=running_version):
            return system._downgrade_guard()

    def test_open_node_and_node_with_exec_update_to_any_version(self):
        for raw_policy in ["", "full", "monitoring,exec"]:
            with self.subTest(raw_policy=raw_policy):
                self.assertEqual(self.guard(raw_policy), "")

    def test_restricted_node_without_exec_checks_the_cloned_version(self):
        for raw_policy in ["monitoring", "readonly,system", "exec:ro"]:
            with self.subTest(raw_policy=raw_policy):
                guard = self.guard(raw_policy)
                self.assertIn(f'"{RUNNING_VERSION}"', guard)
                self.assertIn(f"{system.UPDATER_CLONE_DIR}/node/VERSION", guard)

    def test_node_that_does_not_know_its_version_stays_updatable(self):
        self.assertEqual(self.guard("monitoring", running_version="unknown"), "")


@unittest.skipIf(sys.platform == "win32", "фрагмент исполняет sh апдейтера, на Windows sh уходит в WSL-лаунчер")
@unittest.skipUnless(shutil.which("sh"), "sh not available")
class DowngradeGuardShellTests(unittest.TestCase):

    def run_guard(self, cloned_version: Optional[str]) -> tuple[subprocess.CompletedProcess, bool]:
        work = Path(tempfile.mkdtemp(prefix="downgrade-guard-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        clone = work / "clone"
        (clone / "node").mkdir(parents=True)
        if cloned_version is not None:
            (clone / "node" / "VERSION").write_bytes(cloned_version.encode())
        guard = system.render_downgrade_guard(str(clone), RUNNING_VERSION)
        script = f"set -e\n{guard}\necho {APPLY_MARKER}\n"
        result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30)
        return result, clone.exists()

    def test_same_or_newer_version_reaches_apply(self):
        for version in ["10.36.0", "10.36.0\n", "10.36.0\r\n", "10.36.1", "10.100.0", "11.0.0"]:
            with self.subTest(version=version):
                result, clone_kept = self.run_guard(version)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(APPLY_MARKER, result.stdout)
                self.assertTrue(clone_kept)

    def test_older_version_is_refused_before_any_of_its_code_runs(self):
        # 10.9.0 старше 10.36.0: сравнение по числам, а не по строкам
        for version in ["10.35.9", "10.9.0", "9.99.99", "0.0.1"]:
            with self.subTest(version=version):
                result, clone_kept = self.run_guard(version)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn(APPLY_MARKER, result.stdout)
                self.assertIn("[ERROR]", result.stdout)
                self.assertIn("'exec'", result.stdout)
                self.assertFalse(clone_kept)

    def test_tree_without_a_readable_version_is_refused(self):
        # Версии до появления node/VERSION — как раз те, что о правах не знают
        for version in [None, "", "main", "10.36", "10.36.0-rc1", "10.36.0; reboot"]:
            with self.subTest(version=version):
                result, _ = self.run_guard(version)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn(APPLY_MARKER, result.stdout)


class UpdaterScriptTests(unittest.TestCase):

    def setUp(self):
        self.script = system.render_updater_script("dev", "CLONE_STEP", True, "DOWNGRADE_GUARD")

    def test_host_staging_directory_is_private_and_unpredictable(self):
        # Общий /tmp/monitoring-staging локальный пользователь успевал подменить своим
        self.assertIn("mktemp -d /tmp/monitoring-staging.XXXXXX", self.script)
        self.assertIsNone(re.search(r"/tmp/monitoring-staging(?![.\w])", self.script))

    def test_apply_update_keeps_its_arguments(self):
        self.assertIn(
            'env MON_ALLOW_LOCAL_BUILD=1 bash "$HOST_STAGING/node/scripts/apply-update.sh" '
            '"$HOST_STAGING" /opt/monitoring-node "$CURRENT_VERSION" "dev"',
            self.script,
        )

    def test_staging_directory_is_removed_whatever_the_outcome(self):
        self.assertIn("""trap 'nsenter -t 1 -m -u -n -i -p -- rm -rf "$HOST_TMP" 2>/dev/null || true' EXIT""", self.script)

    def test_version_guard_stands_between_the_clone_and_the_first_use_of_its_files(self):
        clone, guard, staging = (self.script.index(part) for part in ("CLONE_STEP", "DOWNGRADE_GUARD", "Staging update files"))
        self.assertLess(clone, guard)
        self.assertLess(guard, staging)

    def test_local_build_can_be_forbidden(self):
        self.assertIn("MON_ALLOW_LOCAL_BUILD=0 ", system.render_updater_script("dev", "CLONE_STEP", False))

    @unittest.skipIf(sys.platform == "win32", "нужен sh апдейтера")
    @unittest.skipUnless(shutil.which("sh"), "sh not available")
    def test_script_is_valid_sh(self):
        guard = system.render_downgrade_guard(system.UPDATER_CLONE_DIR, RUNNING_VERSION)
        script = system.render_updater_script("dev", "true", True, guard)

        check = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True, timeout=30)

        self.assertEqual(check.returncode, 0, check.stderr)


if __name__ == "__main__":
    unittest.main()
