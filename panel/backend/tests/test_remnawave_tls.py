"""Проверка сертификата Remnawave API.

С каждым запросом к Remnawave уходят токен API и cookie. Раньше сертификат не
проверялся вовсе — их получил бы любой, кто встал между панелью и Remnawave.
Теперь проверка включена по умолчанию, владелец может её снять, а для адресов,
у которых публичного сертификата быть не может, она не делается никогда.
"""

import datetime
import ipaddress
import os
import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers import remnawave as router  # noqa: E402
from app.services import torrent_blocker, xray_stats_collector  # noqa: E402
from app.services.remnawave_api import (  # noqa: E402
    TLS_UNTRUSTED,
    TLS_UNTRUSTED_MESSAGE,
    RemnawaveAPI,
    RemnawaveAPIError,
    host_can_have_public_cert,
)


class HostsWithoutPublicCertTests(unittest.TestCase):
    def test_domain_name_is_verified(self):
        for url in ("https://panel.example.com", "https://panel.example.com:8443/", "https://remna.internal"):
            self.assertTrue(host_can_have_public_cert(url), url)

    def test_addresses_and_local_names_are_not(self):
        for url in (
            "https://203.0.113.7:3000",
            "https://[2001:db8::1]:3000",
            "https://[::ffff:203.0.113.7]",
            "https://localhost:3000",
            "https://localhost.",
            "http://remnawave:3000",
            "",
        ):
            self.assertFalse(host_can_have_public_cert(url), url)


class VerificationSettingTests(unittest.TestCase):
    def context(self, url: str, **kwargs) -> ssl.SSLContext:
        return RemnawaveAPI(url, "token", **kwargs)._ssl_context()

    def test_certificate_is_verified_by_default(self):
        context = self.context("https://panel.example.com")

        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_owner_can_turn_verification_off(self):
        context = self.context("https://panel.example.com", verify_tls=False)

        self.assertEqual(context.verify_mode, ssl.CERT_NONE)

    def test_address_is_never_verified(self):
        context = self.context("https://203.0.113.7:3000", verify_tls=True)

        self.assertEqual(context.verify_mode, ssl.CERT_NONE)

    def test_unset_setting_does_not_disable_verification(self):
        context = self.context("https://panel.example.com", verify_tls=None)

        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)


def write_self_signed_cert(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "remnawave.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    return cert_path, key_path


class SelfSignedApiTests(unittest.IsolatedAsyncioTestCase):
    """Живой HTTPS-сервер с самоподписанным сертификатом вместо Remnawave."""

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cert_path, key_path = write_self_signed_cert(Path(tmp.name))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)

        self.requests_with_token = 0
        app = web.Application()
        app.router.add_get("/api/system/stats", self.answer)
        app.router.add_get("/api/nodes", self.answer)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_context)
        await site.start()
        self.url = f"https://127.0.0.1:{runner.addresses[0][1]}"

    async def answer(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Api-Key") == "token":
            self.requests_with_token += 1
        return web.json_response({"response": []})

    def client(self, verify: bool) -> RemnawaveAPI:
        api = RemnawaveAPI(self.url, "token")
        # Адрес-IP сам по себе не проверяется; сертификат выписан на него же,
        # так что с проверкой отказ даёт именно недоверенный издатель
        api.verify_tls = verify
        self.addAsyncCleanup(api.close)
        return api

    async def test_unverified_host_is_reachable_with_self_signed_cert(self):
        result = await self.client(verify=False).check_connection()

        self.assertTrue(result["auth_valid"])
        self.assertEqual(self.requests_with_token, 1)

    async def test_untrusted_cert_is_named_and_token_is_not_sent(self):
        result = await self.client(verify=True).check_connection()

        self.assertFalse(result["api_reachable"])
        self.assertEqual(result["error"], TLS_UNTRUSTED_MESSAGE)
        self.assertEqual(result["error_code"], TLS_UNTRUSTED)
        self.assertEqual(self.requests_with_token, 0)

    async def test_request_fails_at_once_with_the_same_explanation(self):
        api = self.client(verify=True)

        with mock.patch("app.services.remnawave_api.asyncio.sleep") as retry_pause, \
                self.assertRaises(RemnawaveAPIError) as caught:
            await api.get_all_nodes()

        self.assertEqual(caught.exception.message, TLS_UNTRUSTED_MESSAGE)
        self.assertIn("turned off in the Remnawave settings", caught.exception.message)
        retry_pause.assert_not_called()


class _SettingsDb:
    def __init__(self, row):
        self.row = row
        self.commit = mock.AsyncMock()
        self.added: list = []

    def add(self, row) -> None:
        self.added.append(row)

    async def execute(self, _query):
        result = mock.MagicMock()
        result.scalar_one_or_none.return_value = self.row
        return result


def settings_row(**fields) -> SimpleNamespace:
    return SimpleNamespace(api_url="https://panel.example.com", api_token="token", cookie_secret=None, **fields)


class SettingsEndpointsTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkbox_is_saved(self):
        row = settings_row(verify_tls=True)

        await router.update_settings(router.UpdateSettingsRequest(verify_tls=False), _SettingsDb(row))

        self.assertFalse(row.verify_tls)

    async def test_request_without_field_keeps_setting(self):
        row = settings_row(verify_tls=False)

        await router.update_settings(router.UpdateSettingsRequest(collection_interval=120), _SettingsDb(row))

        self.assertFalse(row.verify_tls)

    async def test_settings_without_row_report_verification_on(self):
        self.assertTrue((await router.get_settings(_SettingsDb(None)))["verify_tls"])

    async def test_connection_test_uses_setting_and_returns_error_code(self):
        api = mock.MagicMock(close=mock.AsyncMock())
        api.check_connection = mock.AsyncMock(return_value={
            "api_reachable": False, "auth_valid": False,
            "error": TLS_UNTRUSTED_MESSAGE, "error_code": TLS_UNTRUSTED,
        })

        with mock.patch.object(router, "get_remnawave_api", return_value=api) as factory:
            result = await router.test_connection(_SettingsDb(settings_row(verify_tls=False)))

        factory.assert_called_once_with("https://panel.example.com", "token", None, False)
        self.assertEqual(result["error_code"], TLS_UNTRUSTED)
        self.assertFalse(result["success"])


class BackgroundClientsTests(unittest.IsolatedAsyncioTestCase):
    """Сбор данных и блокировщик торрентов ходят в тот же API — настройка одна на всех."""

    async def test_collector_passes_setting_to_client(self):
        api = mock.MagicMock(close=mock.AsyncMock(), get_user_traffic_bytes=mock.AsyncMock(return_value=5))
        collector = xray_stats_collector.XrayStatsCollector()

        with mock.patch.object(xray_stats_collector, "get_remnawave_api", return_value=api) as factory:
            await collector._user_daily_traffic(settings_row(verify_tls=False), 42, "u-1")

        factory.assert_called_once_with("https://panel.example.com", "token", None, False)

    async def test_torrent_blocker_passes_setting_to_client(self):
        session = mock.MagicMock()
        session.__aenter__ = mock.AsyncMock(return_value=_SettingsDb(settings_row(verify_tls=False)))
        session.__aexit__ = mock.AsyncMock(return_value=False)

        with mock.patch.object(torrent_blocker, "async_session", return_value=session):
            api = await torrent_blocker.TorrentBlockerService()._get_remnawave_api()

        self.assertFalse(api.verify_tls)


if __name__ == "__main__":
    unittest.main()
