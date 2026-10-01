"""Адрес прокси для загрузок проверяется ещё в панели: на ноде он попадает в
sourced proxy.conf, apt.conf и Environment= юнита Docker, где кавычки и
метасимволы shell сломали бы файл или исполнились бы."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError  # noqa: E402

from app.routers.proxy import DownloadProxyRequest, DownloadProxyTestRequest  # noqa: E402


class DownloadProxyRequestTests(unittest.TestCase):
    def test_accepted(self):
        for url in ("http://10.0.0.1:3128", "https://u:p%40ss@proxy.example.com:443/"):
            self.assertEqual(DownloadProxyRequest(url=url).url, url)

    def test_rejected(self):
        for url in ("http://u:pa$s@h:1", "http://h:1;id", "http://u:'x'@h:1", "socks5://h:1", "10.0.0.1:3128", ""):
            with self.assertRaises(ValidationError, msg=url):
                DownloadProxyRequest(url=url)

    def test_test_without_url_checks_configured_ones(self):
        self.assertIsNone(DownloadProxyTestRequest().url)


if __name__ == "__main__":
    unittest.main()
