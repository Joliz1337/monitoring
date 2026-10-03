"""Прокси для загрузок: разведка по всем местам, где он живёт, и план изменений.

Мёртвый прокси, поставленный давно и мимо установщика, ломал обновления: curl
команды панели шёл через прокси из окружения агента, apt — из чужого apt.conf.
Разведка обязана найти его везде, а удаление — вычистить, не задев соседние
настройки файлов. Пароль прокси не должен уходить из ноды.
"""

import base64
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import download_proxy as dp  # noqa: E402
from app.services.download_proxy import ProxySource  # noqa: E402

DEAD = "http://user:secret@103.127.76.82:22931"

FILES = {
    dp.MONITORING_CONF: f"PROXY_ENABLED=1\nPROXY_URL={DEAD}\n",
    dp.APT_OWN: f'Acquire::http::Proxy "{DEAD}";\nAcquire::https::Proxy "{DEAD}";\n',
    "/etc/apt/apt.conf.d/95proxies": 'Acquire::http::Proxy "http://old:3128";\nAPT::Get::Assume-Yes "true";\n',
    dp.DOCKER_DROPIN_OWN: f'[Service]\nEnvironment="HTTP_PROXY={DEAD}"\nEnvironment="NO_PROXY=localhost"\n',
    "/etc/systemd/system/docker.service.d/override.conf":
        '[Service]\nEnvironment="HTTPS_PROXY=http://old:3128" "DOCKER_OPTS=--debug"\nLimitNOFILE=1048576\n',
    dp.DOCKER_CLIENT_CONFIG:
        '{"auths": {"ghcr.io": {"auth": "c2VjcmV0"}}, "proxies": {"default": {"httpProxy": "http://old:3128", "noProxy": "localhost"}}}',
    dp.GITCONFIG: f"[user]\n\tname = root\n[http]\n\tproxy = {DEAD}\n[https]\n\tproxy = {DEAD}\n",
    dp.CURLRC: 'proxy = "http://old:3128"\nuser-agent = "x"\n',
    dp.ENVIRONMENT: 'PATH="/usr/bin"\nhttps_proxy=http://old:3128\nexport NO_PROXY=localhost\n',
    dp.NODE_ENV: "NODE_SECRET=abc\nHTTPS_PROXY=http://old:3128\n",
}
AGENT_ENV = {"HTTPS_PROXY": "http://old:3128", "PATH": "/usr/bin"}


def entries(files=FILES, agent_env=AGENT_ENV):
    return dp.detect(files, agent_env)


class MaskTests(unittest.TestCase):
    def test_password_hidden(self):
        self.assertEqual(dp.mask_proxy_url(DEAD), "http://user:•••@103.127.76.82:22931")

    def test_without_credentials_untouched(self):
        self.assertEqual(dp.mask_proxy_url("http://10.0.0.1:3128"), "http://10.0.0.1:3128")

    def test_public_entry_never_carries_password(self):
        for entry in entries():
            self.assertNotIn("secret", str(entry.public()))


class ScanParsingTests(unittest.TestCase):
    def test_files_decoded(self):
        raw = "FILE\t/etc/environment\t" + base64.b64encode(b"https_proxy=http://a:1\n").decode() + "\nnoise\n"
        self.assertEqual(dp.parse_scan_output(raw), {"/etc/environment": "https_proxy=http://a:1\n"})


class DetectTests(unittest.TestCase):
    def test_every_place_is_found(self):
        found = {(e.source, e.location) for e in entries()}
        self.assertEqual(found, {
            (ProxySource.MONITORING, dp.MONITORING_CONF),
            (ProxySource.APT, dp.APT_OWN),
            (ProxySource.APT, "/etc/apt/apt.conf.d/95proxies"),
            (ProxySource.DOCKER_DAEMON, dp.DOCKER_DROPIN_OWN),
            (ProxySource.DOCKER_DAEMON, "/etc/systemd/system/docker.service.d/override.conf"),
            (ProxySource.DOCKER_CLIENT, dp.DOCKER_CLIENT_CONFIG),
            (ProxySource.GIT, dp.GITCONFIG),
            (ProxySource.CURL, dp.CURLRC),
            (ProxySource.ENVIRONMENT, dp.ENVIRONMENT),
            (ProxySource.NODE_ENV, dp.NODE_ENV),
            (ProxySource.AGENT_ENV, dp.AGENT_ENV_LOCATION),
        })

    def test_disabled_installer_proxy_is_not_a_proxy(self):
        files = {dp.MONITORING_CONF: "PROXY_ENABLED=0\nPROXY_URL=\n"}
        self.assertEqual(entries(files, {}), [])

    def test_apt_direct_and_per_host_rules_ignored(self):
        files = {"/etc/apt/apt.conf.d/10x": 'Acquire::http::Proxy::mirror.local "DIRECT";\nAcquire::https::Proxy "false";\n'}
        self.assertEqual(entries(files, {}), [])

    def test_git_proxy_outside_http_section_ignored(self):
        files = {dp.GITCONFIG: "[core]\n\tproxy = nope\n"}
        self.assertEqual(entries(files, {}), [])


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.changes = dp.plan_removal(FILES, entries())

    def test_own_files_deleted_foreign_files_cleaned(self):
        self.assertEqual(self.changes.deletes, {dp.APT_OWN, dp.DOCKER_DROPIN_OWN})
        self.assertEqual(self.changes.writes[dp.MONITORING_CONF], "PROXY_ENABLED=0\nPROXY_URL=\n")

    def test_neighbour_settings_survive(self):
        writes = self.changes.writes
        self.assertEqual(writes["/etc/apt/apt.conf.d/95proxies"], 'APT::Get::Assume-Yes "true";\n')
        self.assertEqual(writes["/etc/systemd/system/docker.service.d/override.conf"],
                         '[Service]\nEnvironment="DOCKER_OPTS=--debug"\nLimitNOFILE=1048576\n')
        self.assertIn('"auths"', writes[dp.DOCKER_CLIENT_CONFIG])
        self.assertNotIn("proxies", writes[dp.DOCKER_CLIENT_CONFIG])
        self.assertEqual(writes[dp.GITCONFIG], "[user]\n\tname = root\n")
        self.assertEqual(writes[dp.CURLRC], 'user-agent = "x"\n')
        self.assertEqual(writes[dp.ENVIRONMENT], 'PATH="/usr/bin"\n')
        self.assertEqual(writes[dp.NODE_ENV], "NODE_SECRET=abc\n")

    def test_docker_restarted_and_agent_recreated(self):
        self.assertTrue(self.changes.restart_docker)
        self.assertTrue(self.changes.recreate_agent)
        self.assertEqual(
            dp.restart_script(self.changes),
            f"systemctl restart docker && cd {dp.NODE_DIR} && docker compose up -d --force-recreate",
        )

    def test_agent_not_recreated_when_its_source_is_unknown(self):
        found = entries({}, AGENT_ENV)
        changes = dp.plan_removal({}, found)
        self.assertFalse(changes.recreate_agent)
        self.assertIsNone(dp.restart_script(changes))

    def test_curl_only_needs_no_restart(self):
        files = {dp.CURLRC: "proxy = http://old:3128\n"}
        changes = dp.plan_removal(files, entries(files, {}))
        self.assertFalse(changes.restart_docker)
        self.assertIsNone(dp.restart_script(changes))


class SetTests(unittest.TestCase):
    URL = "http://bob:p%40ss@10.0.0.5:3128"

    def test_installer_scheme_written_and_foreign_removed(self):
        changes = dp.plan_set(FILES, entries(), self.URL)
        writes = changes.writes
        self.assertIn(f"PROXY_URL='{self.URL}'", writes[dp.MONITORING_CONF])
        self.assertIn(f'Acquire::https::Proxy "{self.URL}";', writes[dp.APT_OWN])
        # systemd раскрыл бы %40 как спецификатор
        self.assertIn('Environment="HTTPS_PROXY=http://bob:p%%40ss@10.0.0.5:3128"', writes[dp.DOCKER_DROPIN_OWN])
        self.assertTrue(writes[dp.GITCONFIG].startswith("[user]\n\tname = root\n"))
        self.assertEqual(writes[dp.GITCONFIG].count(self.URL), 2)
        self.assertNotIn("old:3128", writes[dp.ENVIRONMENT])
        self.assertEqual(changes.deletes, set())
        self.assertTrue(changes.restart_docker)

    def test_changing_twice_does_not_pile_up_git_sections(self):
        first = dp.plan_set(FILES, entries(), self.URL).writes
        files = {**FILES, **first}
        second = dp.plan_set(files, entries(files, {}), "http://10.0.0.6:3128").writes[dp.GITCONFIG]
        self.assertEqual(second.count("[http]"), 1)
        self.assertNotIn(self.URL, second)

    def test_node_without_any_proxy(self):
        changes = dp.plan_set({}, [], "http://10.0.0.6:3128")
        self.assertEqual(set(changes.writes), {dp.MONITORING_CONF, dp.APT_OWN, dp.DOCKER_DROPIN_OWN, dp.GITCONFIG})
        self.assertFalse(changes.recreate_agent)


class UrlPatternTests(unittest.TestCase):
    def test_accepted(self):
        for url in ("http://10.0.0.1:3128", "https://proxy.example.com:443", "http://u:p%24ss@h:8080/"):
            self.assertRegex(url, dp.PROXY_URL_PATTERN)

    def test_shell_and_systemd_breakers_rejected(self):
        for url in ("http://u:pa$s@h:1", "http://h:1;rm -rf /", "http://u:'x'@h:1", 'http://"h":1', "socks5://h:1", "http://h :1"):
            self.assertIsNone(re.match(dp.PROXY_URL_PATTERN, url), url)


if __name__ == "__main__":
    unittest.main()
