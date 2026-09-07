"""Разведка и вырезание доступов хостера: разбор скана, детекция, нейтрализация.

Запуск из node/:  python -m unittest discover -s tests -p "test_*.py"
"""

import asyncio
import base64
import os
import sys
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import hoster_access as ha  # noqa: E402
from app.services.hoster_access import (  # noqa: E402
    HosterAccessManager,
    classify_ssh_key,
    detect,
    neutralize_apt_source,
    parse_scan_output,
)


def _b(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def build_dump(
    *,
    vendor="cmpt\nOpenStack Compute",
    packages=(),
    units=(),
    qemu_active="",
    qemu_port=False,
    cloudinit_disabled=False,
    keys=(),  # (file, fp_line, raw_line)
    users=(),  # (name, uid, home, shell)
    sudoers=(),  # (file, content)
    groups=(),  # raw getent group lines
    apt=(),  # (file, content)
    sshd=(),  # raw "key value" lines
    dropins=(),  # (file, content)
) -> str:
    lines = ["@@VENDOR", vendor, "@@PKGS", *packages, "@@UNITS", *units]
    lines += ["@@QEMU_ACTIVE", qemu_active, "@@QEMU_PORT"]
    if qemu_port:
        lines.append("org.qemu.guest_agent.0")
    lines += ["@@CLOUDINIT_DISABLED", "yes" if cloudinit_disabled else "no", "@@KEYS"]
    for f, fp, raw in keys:
        lines.append(f"KEY\t{f}\t{fp}\t{_b(raw)}")
    lines.append("@@USERS")
    for name, uid, home, shell in users:
        lines.append(f"{name}:{uid}:{home}:{shell}")
    lines.append("@@SUDOERS")
    for f, content in sudoers:
        lines.append(f"SUDO\t{f}\t{_b(content)}")
    lines.append("@@GROUPS")
    lines += list(groups)
    lines.append("@@APT")
    for f, content in apt:
        lines.append(f"APT\t{f}\t{_b(content)}")
    lines.append("@@SSHD")
    lines += list(sshd)
    lines.append("@@DROPINS")
    for f, content in dropins:
        lines.append(f"DROPIN\t{f}\t{_b(content)}")
    lines.append("@@END")
    return "\n".join(lines)


class ParseTests(unittest.TestCase):
    def test_sections_parsed(self):
        dump = build_dump(
            vendor="Selectel\nOpenStack Nova",
            packages=("cloud-init", "qemu-guest-agent", "telegraf"),
            units=("qemu-guest-agent.service", "telegraf.service"),
            qemu_active="active",
            qemu_port=True,
            users=(("root", 0, "/root", "/bin/bash"), ("ubuntu", 1000, "/home/ubuntu", "/bin/bash")),
            sshd=("permitrootlogin yes", "passwordauthentication yes"),
        )
        facts = parse_scan_output(dump)
        self.assertEqual(facts.vendor, "Selectel OpenStack Nova")
        self.assertIn("telegraf", facts.packages)
        self.assertTrue(facts.cloudinit_installed)
        self.assertEqual(facts.qemu_active, "active")
        self.assertEqual(facts.qemu_ports, ["org.qemu.guest_agent.0"])
        self.assertEqual(facts.sshd["permitrootlogin"], "yes")
        self.assertEqual(len(facts.users), 2)

    def test_key_base64_roundtrip(self):
        raw = 'ssh-rsa AAAAB3Nza Generated-by-Nova'
        dump = build_dump(keys=[("/root/.ssh/authorized_keys", "2048 SHA256:x Generated-by-Nova (RSA)", raw)])
        facts = parse_scan_output(dump)
        self.assertEqual(facts.keys[0][0], "/root/.ssh/authorized_keys")
        self.assertEqual(ha._b64_decode(facts.keys[0][2]), raw)


class ClassifyTests(unittest.TestCase):
    def test_hoster_markers(self):
        self.assertTrue(classify_ssh_key("2048 SHA256:x Generated-by-Nova (RSA)"))
        self.assertTrue(classify_ssh_key('2048 SHA256:y no-agent-forwarding,command="...Please login as..." (RSA)'))
        self.assertFalse(classify_ssh_key("4096 SHA256:z joliz@DESKTOP-FN3ITS8 (RSA)"))


class DetectTests(unittest.TestCase):
    def test_vk_profile(self):
        facts = parse_scan_output(build_dump(
            vendor="cmpt\nOpenStack Compute",
            packages=("cloud-init", "qemu-guest-agent", "telegraf"),
            units=("telegraf.service",),
            qemu_active="inactive",
            qemu_port=True,
            keys=[("/root/.ssh/authorized_keys", "2048 SHA256:a Generated-by-Nova (RSA)", "ssh-rsa AAA Generated-by-Nova")],
            users=(("root", 0, "/root", "/bin/bash"), ("ubuntu", 1000, "/home/ubuntu", "/bin/bash")),
            sshd=("permitrootlogin yes", "passwordauthentication yes"),
        ))
        cats = {i.category for i in detect(facts)}
        self.assertIn("qemu_guest_agent", cats)
        self.assertIn("cloud_init", cats)
        self.assertIn("monitoring_agent", cats)
        self.assertIn("foreign_ssh_key", cats)
        self.assertIn("foreign_user", cats)
        self.assertIn("sshd_weakening", cats)

    def test_google_agent_detected_by_unit_only(self):
        facts = parse_scan_output(build_dump(
            packages=(),  # dpkg не показывает пакет
            units=("google-guest-agent.service",),
        ))
        agents = [i for i in detect(facts) if i.category == "monitoring_agent"]
        self.assertEqual(len(agents), 1)
        self.assertTrue(agents[0].access_critical)
        self.assertIn("rm -f /etc/sudoers.d/google_sudoers", " ".join(agents[0].commands))

    def test_own_services_never_flagged(self):
        facts = parse_scan_output(build_dump(
            packages=("nginx", "haproxy", "docker-ce"),
            units=("nginx.service", "haproxy.service", "ddos-watchdog.service", "docker.service"),
        ))
        self.assertEqual([i for i in detect(facts) if i.category == "monitoring_agent"], [])

    def test_operator_key_not_default_selected(self):
        facts = parse_scan_output(build_dump(
            keys=[("/root/.ssh/authorized_keys", "4096 SHA256:z joliz@DESKTOP (RSA)", "ssh-rsa BBB joliz@DESKTOP")],
        ))
        key = next(i for i in detect(facts) if i.category == "foreign_ssh_key")
        self.assertFalse(key.default_selected)

    def test_zabbix_timeweb_single_finding_covers_pkg_and_unit(self):
        # Timeweb: пакет zabbix-agent-timeweb, но юнит zabbix-agent — одна находка,
        # purge и вычищает пакет, и гасит юнит.
        facts = parse_scan_output(build_dump(
            packages=("zabbix-agent-timeweb",),
            units=("zabbix-agent.service",),
        ))
        agents = [i for i in detect(facts) if i.category == "monitoring_agent"]
        self.assertEqual(len(agents), 1)
        cmds = " ".join(agents[0].commands)
        self.assertIn("purge -y zabbix-agent-timeweb", cmds)
        self.assertIn("disable --now zabbix-agent ", cmds)

    def test_timeweb_profile(self):
        facts = parse_scan_output(build_dump(
            vendor="QEMU\nUbuntu 18.04 PC (i440FX + PIIX, 1996)",
            packages=("cloud-init", "qemu-guest-agent", "zabbix-agent-timeweb"),
            units=("qemu-guest-agent.service", "zabbix-agent.service"),
            qemu_active="active",
            qemu_port=True,
            users=(("root", 0, "/root", "/bin/bash"),),
            apt=[
                ("/etc/apt/sources.list.d/ubuntu.sources", "Types: deb\nURIs: http://mirror.timeweb.ru/ubuntu/\nSuites: noble\nComponents: main\n"),
                ("/etc/apt/sources.list.d/zabbix.list", "deb [signed-by=/etc/apt/keyrings/timeweb-zabbix.gpg] http://zabbix.repo.timeweb.ru/ubuntu focal main\n"),
            ],
            sshd=("permitrootlogin yes", "passwordauthentication yes"),
        ))
        items = detect(facts)
        cats = [i.category for i in items]
        self.assertIn("qemu_guest_agent", cats)
        self.assertIn("cloud_init", cats)
        self.assertEqual(cats.count("monitoring_agent"), 1)  # zabbix одной находкой
        self.assertEqual(cats.count("hoster_apt_repo"), 2)   # оба репозитория Timeweb

    def test_cloud_init_disabled_not_flagged(self):
        facts = parse_scan_output(build_dump(packages=("cloud-init",), cloudinit_disabled=True))
        self.assertNotIn("cloud_init", {i.category for i in detect(facts)})

    def test_hardened_sshd_not_flagged(self):
        facts = parse_scan_output(build_dump(
            sshd=("permitrootlogin without-password", "passwordauthentication no"),
        ))
        self.assertNotIn("sshd_weakening", {i.category for i in detect(facts)})


class AptNeutralizeTests(unittest.TestCase):
    def test_trusted_yes_stripped_and_host_swapped(self):
        content = "deb [trusted=yes] http://mirror.timeweb.ru/ubuntu noble main\n"
        out, changed = neutralize_apt_source(content)
        self.assertTrue(changed)
        self.assertNotIn("trusted=yes", out)
        self.assertIn("archive.ubuntu.com", out)
        self.assertNotIn("mirror.timeweb.ru", out)

    def test_dedicated_third_party_repo_dropped(self):
        content = (
            "deb http://mirror.selectel.ru/ubuntu/ resolute main\n"
            "deb https://mirror.selectel.ru/3rd-party/cloud-init-deb/resolute/ ./\n"
        )
        out, changed = neutralize_apt_source(content)
        self.assertTrue(changed)
        self.assertIn("archive.ubuntu.com/ubuntu/", out)
        self.assertNotIn("3rd-party", out)

    def test_stock_source_untouched(self):
        content = "deb http://archive.ubuntu.com/ubuntu noble main\n"
        out, changed = neutralize_apt_source(content)
        self.assertFalse(changed)
        self.assertEqual(out, content)

    def test_comments_preserved(self):
        content = "# hoster mirror mirror.yandex.ru\ndeb http://mirror.yandex.ru/ubuntu noble main\n"
        out, _ = neutralize_apt_source(content)
        self.assertIn("# hoster mirror mirror.yandex.ru", out)

    def test_deb822_main_stanza_host_swapped(self):
        content = (
            "Types: deb\n"
            "URIs: http://mirror.selectel.ru/ubuntu/\n"
            "Suites: resolute resolute-updates\n"
            "Components: main universe\n"
            "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
        )
        out, changed = neutralize_apt_source(content)
        self.assertTrue(changed)
        self.assertIn("URIs: http://archive.ubuntu.com/ubuntu/", out)
        self.assertNotIn("mirror.selectel.ru", out)
        # станца не осиротела — Types/Suites/Components на месте
        self.assertIn("Types: deb", out)
        self.assertIn("Suites: resolute", out)

    def test_deb822_dedicated_stanza_dropped_but_stock_kept(self):
        content = (
            "Types: deb\n"
            "URIs: http://archive.ubuntu.com/ubuntu/\n"
            "Suites: resolute\n"
            "Components: main\n"
            "\n"
            "Types: deb\n"
            "URIs: https://mirror.selectel.ru/3rd-party/cloud-init-deb/resolute/\n"
            "Suites: ./\n"
        )
        out, changed = neutralize_apt_source(content)
        self.assertTrue(changed)
        self.assertNotIn("3rd-party", out)
        self.assertNotIn("mirror.selectel.ru", out)
        self.assertIn("archive.ubuntu.com/ubuntu/", out)

    def test_timeweb_zabbix_repo_line_dropped(self):
        # Отдельный репозиторий хостера (host не основное зеркало) — строка выкидывается.
        content = "deb [signed-by=/etc/apt/keyrings/timeweb-zabbix.gpg] http://zabbix.repo.timeweb.ru/ubuntu focal main\n"
        out, changed = neutralize_apt_source(content)
        self.assertTrue(changed)
        self.assertNotIn("zabbix.repo.timeweb.ru", out)

    def test_timeweb_deb822_main_mirror_swapped(self):
        content = "Types: deb\nURIs: http://mirror.timeweb.ru/ubuntu/\nSuites: noble\nComponents: main\n"
        out, changed = neutralize_apt_source(content)
        self.assertTrue(changed)
        self.assertIn("archive.ubuntu.com/ubuntu/", out)
        self.assertNotIn("timeweb", out)

    def test_deb822_trusted_field_removed(self):
        content = (
            "Types: deb\n"
            "URIs: http://mirror.timeweb.ru/ubuntu/\n"
            "Suites: noble\n"
            "Components: main\n"
            "Trusted: yes\n"
        )
        out, changed = neutralize_apt_source(content)
        self.assertTrue(changed)
        self.assertNotIn("Trusted: yes", out)
        self.assertIn("archive.ubuntu.com/ubuntu/", out)


@dataclass
class FakeResult:
    success: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str = ""


class FakeExecutor:
    def __init__(self, scan_dump: str):
        self.scan_dump = scan_dump
        self.shell_commands: list[str] = []

    async def execute(self, command, timeout=30, shell="sh"):
        if command == ha.SCAN_SCRIPT:
            return FakeResult(stdout=self.scan_dump)
        self.shell_commands.append(command)
        return FakeResult(success=True)


class PurgeTests(unittest.TestCase):
    def _manager(self, dump: str) -> tuple[HosterAccessManager, FakeExecutor]:
        mgr = HosterAccessManager()
        fake = FakeExecutor(dump)
        mgr._executor = fake
        return mgr, fake

    def test_unknown_id_reported_not_run(self):
        mgr, _ = self._manager(build_dump())
        resp = asyncio.run(mgr.purge(["does-not-exist"]))
        self.assertFalse(resp.results[0].ok)
        self.assertIn("не найдено", resp.results[0].message)

    def test_shell_finding_runs_commands(self):
        dump = build_dump(packages=("qemu-guest-agent",), qemu_port=True)
        mgr, fake = self._manager(dump)
        resp = asyncio.run(mgr.purge(["qemu_guest_agent"]))
        self.assertTrue(resp.results[0].ok)
        self.assertTrue(any("purge -y qemu-guest-agent" in c for c in fake.shell_commands))

    def test_ssh_key_rewrites_file(self):
        raw = "ssh-rsa AAA Generated-by-Nova"
        dump = build_dump(keys=[("/root/.ssh/authorized_keys", "2048 SHA256:a Generated-by-Nova (RSA)", raw)])
        mgr, _ = self._manager(dump)
        item = next(i for i in ha.detect(parse_scan_output(dump)) if i.category == "foreign_ssh_key")

        written: dict[str, str] = {}

        async def fake_read(path):
            return f"{raw}\nssh-ed25519 BBB keep@me\n"

        async def fake_write(path, content, mode=None):
            written[path] = content
            return True

        orig_read, orig_write = ha.read_host_file_exact, ha.write_host_file
        ha.read_host_file_exact, ha.write_host_file = fake_read, fake_write
        try:
            resp = asyncio.run(mgr.purge([item.id]))
        finally:
            ha.read_host_file_exact, ha.write_host_file = orig_read, orig_write

        self.assertTrue(resp.results[0].ok)
        self.assertNotIn("Generated-by-Nova", written["/root/.ssh/authorized_keys"])
        self.assertIn("keep@me", written["/root/.ssh/authorized_keys"])


if __name__ == "__main__":
    unittest.main()
