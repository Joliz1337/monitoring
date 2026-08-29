"""Тесты профилей клиента подписки и передачи HWID.

Голый unittest, без сети.

Панели с привязкой по устройству отдают клиенту без HWID не ключи, а
текст-инструкцию («у вас выключена передача hwid»): формально валидную
подписку, из которой нечего проверять. Отсюда набор заголовков ниже.

HWID детерминирован по адресу подписки. Случайный на каждый запрос
регистрировал бы в чужой панели новое устройство и съедал лимит владельца
ключа — у проверки не должно быть таких побочных эффектов.

Запуск из panel/backend:  python -m unittest discover -s tests -p "test_*.py"
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.xray_test.device_profiles import (  # noqa: E402
    DEFAULT_PROFILE_ID,
    PROFILES,
    build_headers,
    describe,
    get_profile,
    hwid_for,
)

URL = "https://sub.example.com/user/abc123"


class ProfileTest(unittest.TestCase):
    def test_default_profile_sends_hwid(self):
        self.assertTrue(get_profile(DEFAULT_PROFILE_ID).sends_hwid)

    def test_unknown_id_falls_back_to_default(self):
        self.assertEqual(get_profile("no-such-client").id, DEFAULT_PROFILE_ID)
        self.assertEqual(get_profile(None).id, DEFAULT_PROFILE_ID)

    def test_ids_are_unique(self):
        ids = [profile.id for profile in PROFILES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_describe_exposes_hwid_flag(self):
        described = {item["id"]: item for item in describe()}
        self.assertTrue(described["happ-ios"]["sends_hwid"])
        self.assertFalse(described["v2rayng"]["sends_hwid"])


class HwidTest(unittest.TestCase):
    def test_stable_for_same_url(self):
        self.assertEqual(hwid_for(URL), hwid_for(URL))

    def test_whitespace_ignored(self):
        self.assertEqual(hwid_for(URL), hwid_for(f"  {URL}\n"))

    def test_different_url_different_hwid(self):
        self.assertNotEqual(hwid_for(URL), hwid_for(URL + "2"))

    def test_looks_like_uuid(self):
        value = hwid_for(URL)
        self.assertEqual(len(value), 36)
        self.assertEqual(value.count("-"), 4)


class HeadersTest(unittest.TestCase):
    def test_happ_profile_sends_device_headers(self):
        headers = build_headers("happ-ios", URL)

        self.assertEqual(headers["User-Agent"], "Happ/3.13.0")
        self.assertEqual(headers["x-hwid"], hwid_for(URL))
        self.assertEqual(headers["x-device-os"], "iOS")
        self.assertIn("x-ver-os", headers)
        self.assertIn("x-device-model", headers)
        self.assertIn("x-device-locale", headers)

    def test_plain_profile_has_no_hwid(self):
        headers = build_headers("v2rayng", URL)

        self.assertNotIn("x-hwid", headers)
        self.assertNotIn("x-device-os", headers)
        self.assertEqual(headers["User-Agent"], "v2rayNG/1.9.24")

    def test_headers_stable_between_calls(self):
        self.assertEqual(build_headers("happ-android", URL), build_headers("happ-android", URL))

    def test_hwid_differs_per_subscription(self):
        first = build_headers("happ-ios", URL)["x-hwid"]
        second = build_headers("happ-ios", "https://other.example/sub")["x-hwid"]
        self.assertNotEqual(first, second)

    def test_same_hwid_across_device_profiles(self):
        """HWID привязан к подписке, а не к выбранному устройству."""
        self.assertEqual(
            build_headers("happ-ios", URL)["x-hwid"],
            build_headers("happ-windows", URL)["x-hwid"],
        )


if __name__ == "__main__":
    unittest.main()
