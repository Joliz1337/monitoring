"""Одноразовый персональный ключ в автоустановке.

Чекбокс мастера подменяет общий ключ парка персональным сертификатом сервера:
токен собирается на лету и нигде не сохраняется, а зарегистрированная нода
(`uses_shared_cert=False, dedicated_cert=True`) не должна попадать под миграцию
на общий cert — иначе один клик по баннеру уничтожил бы изоляцию.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cryptography import x509  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: E402

from app.routers.server_deploy import DeployRequest  # noqa: E402
from app.services.deploy_job_manager import PostDeployOptions  # noqa: E402
from app.services.migration import classify_server  # noqa: E402
from app.services.pki import (  # noqa: E402
    DEDICATED_DEPLOY_VALIDITY_DAYS,
    DEDICATED_NODE_CN_PREFIX,
    SHARED_NODE_CN,
    PKIKeygenData,
    build_dedicated_installer_token,
    cert_expires_at,
    generate_ca,
    generate_client_cert,
    generate_node_cert,
    unpack_node_secret,
)

CA_CERT, CA_KEY = generate_ca()
CLIENT_CERT, CLIENT_KEY = generate_client_cert(CA_CERT, CA_KEY)
SHARED_CERT, SHARED_KEY = generate_node_cert(CA_CERT, CA_KEY, SHARED_NODE_CN)
KEYGEN = PKIKeygenData(
    ca_cert=CA_CERT,
    ca_key=CA_KEY,
    client_cert=CLIENT_CERT,
    client_key=CLIENT_KEY,
    shared_node_cert=SHARED_CERT,
    shared_node_key=SHARED_KEY,
)


def load_cert(pem: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem.encode())


class BuildDedicatedInstallerTokenTests(unittest.TestCase):
    def test_token_carries_personal_cert_not_shared(self):
        token = build_dedicated_installer_token(KEYGEN, panel_ip="203.0.113.10")
        data = unpack_node_secret(token)
        self.assertEqual(data["v"], 1)
        self.assertEqual(data["ca"], CA_CERT)
        self.assertEqual(data["panel_ip"], "203.0.113.10")
        self.assertNotEqual(data["crt"], SHARED_CERT)
        self.assertNotEqual(data["key"], SHARED_KEY)

        cert = load_cert(data["crt"])
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        self.assertTrue(cn.startswith(DEDICATED_NODE_CN_PREFIX))
        eku = set(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
        self.assertIn(ExtendedKeyUsageOID.SERVER_AUTH, eku)
        self.assertNotIn(ExtendedKeyUsageOID.CLIENT_AUTH, eku)

    def test_each_token_issues_fresh_cert(self):
        first = unpack_node_secret(build_dedicated_installer_token(KEYGEN))
        second = unpack_node_secret(build_dedicated_installer_token(KEYGEN))
        self.assertNotEqual(first["crt"], second["crt"])

    def test_validity_matches_deploy_default(self):
        data = unpack_node_secret(build_dedicated_installer_token(KEYGEN))
        expected = datetime.now(timezone.utc) + timedelta(days=DEDICATED_DEPLOY_VALIDITY_DAYS)
        self.assertLess(abs(cert_expires_at(data["crt"]) - expected), timedelta(days=1))


class DeployFlagDefaultsTests(unittest.TestCase):
    def test_dedicated_cert_defaults_to_shared_key(self):
        req = DeployRequest(name="n1", host="1.2.3.4")
        self.assertFalse(req.dedicated_cert)
        self.assertFalse(PostDeployOptions().dedicated_cert)


class MigrationClassifierTests(unittest.TestCase):
    def make(self, uses_shared_cert: bool, pki_enabled: bool, dedicated_cert: bool):
        return SimpleNamespace(
            uses_shared_cert=uses_shared_cert,
            pki_enabled=pki_enabled,
            dedicated_cert=dedicated_cert,
        )

    def test_dedicated_node_is_not_migration_target(self):
        self.assertEqual(classify_server(self.make(False, True, True)), "dedicated")

    def test_other_classes_unchanged(self):
        self.assertEqual(classify_server(self.make(True, True, False)), "shared")
        self.assertEqual(classify_server(self.make(False, True, False)), "per_server")
        self.assertEqual(classify_server(self.make(False, False, False)), "legacy")


if __name__ == "__main__":
    unittest.main()
