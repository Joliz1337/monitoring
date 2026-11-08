"""Тесты сертификата персонального ключа установки.

Главное свойство — отсутствие clientAuth у серверных сертификатов ноды (общего и
персонального): ключ, оказавшийся у владельца чужого сервера, не должен годиться на то,
чтобы представиться панелью. clientAuth остаётся только у клиентского сертификата панели.
"""

import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography import x509  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID  # noqa: E402

from app.services.pki import (  # noqa: E402
    DEDICATED_NODE_CN_PREFIX,
    SHARED_NODE_CN,
    cert_expires_at,
    generate_ca,
    generate_client_cert,
    generate_dedicated_node_cert,
    generate_node_cert,
    load_or_create_keygen,
    pack_node_secret,
    unpack_node_secret,
)

CA_CERT, CA_KEY = generate_ca()


def load_eku(cert_pem: str) -> set:
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    ext = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    return set(ext.value)


class DedicatedCertTests(unittest.TestCase):
    def test_dedicated_cert_has_no_client_auth(self):
        _, cert_pem, _ = generate_dedicated_node_cert(CA_CERT, CA_KEY, 90)
        eku = load_eku(cert_pem)
        self.assertIn(ExtendedKeyUsageOID.SERVER_AUTH, eku)
        self.assertNotIn(ExtendedKeyUsageOID.CLIENT_AUTH, eku)

    def test_shared_cert_has_no_client_auth(self):
        cert_pem, _ = generate_node_cert(CA_CERT, CA_KEY, SHARED_NODE_CN)
        self.assertEqual(load_eku(cert_pem), {ExtendedKeyUsageOID.SERVER_AUTH})

    def test_panel_client_cert_keeps_client_auth(self):
        cert_pem, _ = generate_client_cert(CA_CERT, CA_KEY)
        self.assertEqual(load_eku(cert_pem), {ExtendedKeyUsageOID.CLIENT_AUTH})

    def test_dedicated_cert_signed_by_ca(self):
        _, cert_pem, _ = generate_dedicated_node_cert(CA_CERT, CA_KEY, 90)
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        ca = x509.load_pem_x509_certificate(CA_CERT.encode())
        cert.verify_directly_issued_by(ca)

    def test_common_name_is_unique_per_issue(self):
        first, _, _ = generate_dedicated_node_cert(CA_CERT, CA_KEY, 90)
        second, _, _ = generate_dedicated_node_cert(CA_CERT, CA_KEY, 90)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith(DEDICATED_NODE_CN_PREFIX))

    def test_validity_days_control_expiry(self):
        _, short_pem, _ = generate_dedicated_node_cert(CA_CERT, CA_KEY, 30)
        _, long_pem, _ = generate_dedicated_node_cert(CA_CERT, CA_KEY, 365)
        delta = cert_expires_at(long_pem) - cert_expires_at(short_pem)
        self.assertEqual(delta.days, 335)

    def test_token_matches_node_secret_format(self):
        """deploy.sh ноды принимает только v=1 с непустыми ca/crt/key."""
        _, cert_pem, key_pem = generate_dedicated_node_cert(CA_CERT, CA_KEY, 90)
        token = pack_node_secret(CA_CERT, cert_pem, key_pem, panel_ip="203.0.113.10")

        data = unpack_node_secret(token)
        self.assertEqual(data["v"], 1)
        self.assertEqual(data["ca"], CA_CERT)
        self.assertEqual(data["crt"], cert_pem)
        self.assertEqual(data["key"], key_pem)
        self.assertEqual(data["panel_ip"], "203.0.113.10")
        self.assertNotIn("=", token)


if __name__ == "__main__":
    unittest.main()


class _EmptyKeygenSession:
    """Сессия БД без записи PKI: load_or_create_keygen идёт по ветке первого запуска."""

    def __init__(self):
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, _statement):
        result = unittest.mock.Mock()
        result.scalar_one_or_none.return_value = None
        return result

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        return None


class IssuedKeygenTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_run_issues_server_only_shared_cert_and_client_auth_panel_cert(self):
        session = _EmptyKeygenSession()

        keygen = await load_or_create_keygen(lambda: session)

        self.assertEqual(load_eku(keygen.shared_node_cert), {ExtendedKeyUsageOID.SERVER_AUTH})
        self.assertEqual(load_eku(keygen.client_cert), {ExtendedKeyUsageOID.CLIENT_AUTH})
        panel_cert = x509.load_pem_x509_certificate(keygen.client_cert.encode())
        self.assertEqual(panel_cert.subject.rfc4514_string(), "CN=panel-client")
