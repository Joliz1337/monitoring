"""Разбор /etc/os-release: по ID и кодовому имени панель ищет сборку HAProxy."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.haproxy_info import parse_os_release  # noqa: E402

UBUNTU_NOBLE = """PRETTY_NAME="Ubuntu 24.04.3 LTS"
NAME="Ubuntu"
VERSION_ID="24.04"
VERSION_CODENAME=noble
ID=ubuntu
ID_LIKE=debian
"""


class ParseOsReleaseTests(unittest.TestCase):
    def test_ubuntu(self):
        fields = parse_os_release(UBUNTU_NOBLE)
        self.assertEqual(fields["ID"], "ubuntu")
        self.assertEqual(fields["VERSION_CODENAME"], "noble")
        self.assertEqual(fields["PRETTY_NAME"], "Ubuntu 24.04.3 LTS")

    def test_single_quotes_and_garbage_lines(self):
        fields = parse_os_release("ID='debian'\n\n# comment\nbroken line\nVERSION_CODENAME=trixie\n")
        self.assertEqual(fields, {"ID": "debian", "VERSION_CODENAME": "trixie"})

    def test_empty(self):
        self.assertEqual(parse_os_release(""), {})


if __name__ == "__main__":
    unittest.main()
